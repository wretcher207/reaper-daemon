-- Actions, scripts and the tempo map for the Reaper Daemon bridge (2026-10-20,
-- GitHub issue #26).
--
-- list_actions / run_action open REAPER's own action list to an agent, so a
-- workflow the bridge has no verb for (podcast editing, a SWS action, a user's
-- own ReaScript bound to an ID) still runs through the same queue, the same
-- undo block and the same reply format. run_script goes further and executes
-- Lua the agent hands over, which is the one capability undo cannot bound: a
-- script can write files, change preferences, or call anything in the API. It
-- therefore sits behind its own gate, allow_script_execution, and that gate
-- deliberately does NOT fall back to allow_risk_level_3: granting disk writes
-- in 2026-09 was not consent to arbitrary code.
--
-- This file is loaded by reaper_agent_bridge.lua on every bridge load, the
-- way automation.lua is: found through package.searchpath, never from
-- require's cache, so reload_bridge picks up an edited copy. It lives outside
-- the bridge file to keep that file under the 256 KiB the Claude plugin
-- directory inspects in full, and because the bridge's main chunk sits at
-- Lua's 200-local limit. The bridge passes in the helpers it shares.
-- =============================================================================
local shared = ...
local config = shared.config
local gate = shared.gate
local root = shared.root
local join = shared.join
local exists = shared.exists
local read_file = shared.read_file
local resolve_position = shared.resolve_position
local time_from_bar = shared.time_from_bar
local bar_from_time = shared.bar_from_time

local ACTION_SECTIONS = {
  main = 0, main_alt = 100,
  midi_editor = 32060, midi_event_list = 32061, midi_inline = 32062,
  media_explorer = 32063,
}
local ACTION_SECTION_NAMES = {}
for name, id in pairs(ACTION_SECTIONS) do ACTION_SECTION_NAMES[id] = name end

local function action_section(value)
  if value == nil or value == "" then return 0, "main" end
  if type(value) == "number" then
    local name = ACTION_SECTION_NAMES[value]
    if not name then error("BAD_SECTION: unknown action section id " .. tostring(value)) end
    return value, name
  end
  local key = tostring(value):lower():gsub("[%s%-]", "_")
  local id = ACTION_SECTIONS[key]
  if id == nil then
    error("BAD_SECTION: section must be one of main, main_alt, midi_editor, "
      .. "midi_event_list, midi_inline, media_explorer")
  end
  return id, key
end

-- The name REAPER shows in its action list, or nil when the API cannot say
-- (an unknown ID, or a REAPER too old to expose the lookup).
local function action_name(command_id, section_id)
  if not reaper.kbd_getTextFromCmd then return nil end
  local ok, name = pcall(reaper.kbd_getTextFromCmd, command_id, section_id)
  if ok and type(name) == "string" and name ~= "" then return name end
  return nil
end

-- true / false for a toggle action, nil for a plain one.
local function action_toggle(command_id, section_id)
  if not reaper.GetToggleCommandStateEx then return nil end
  local state = reaper.GetToggleCommandStateEx(section_id, command_id)
  if state == nil or state < 0 then return nil end
  return state == 1
end

-- The _SWS_... / _RS... string for a command, or nil for a native action.
-- ReverseNamedCommandLookup returns it without the underscore; the underscore
-- is what NamedCommandLookup (and so run_action's named_id) expects back.
local function action_named_id(command_id)
  if not reaper.ReverseNamedCommandLookup then return nil end
  local named = reaper.ReverseNamedCommandLookup(command_id)
  if type(named) ~= "string" or named == "" then return nil end
  return "_" .. named
end

local function action_matches(words, name, named_id)
  if #words == 0 then return true end
  local hay = (name or ""):lower() .. " " .. (named_id or ""):lower()
  for _, word in ipairs(words) do
    if not hay:find(word, 1, true) then return false end
  end
  return true
end

local function command_list_actions(command)
  local p = command.payload or {}
  if not reaper.kbd_enumerateActions then
    error("UNSUPPORTED_API: listing actions needs REAPER 6.71 or newer (kbd_enumerateActions)")
  end
  local section_id, section_name = action_section(p.section)
  local words = {}
  for word in tostring(p.query or ""):lower():gmatch("%S+") do words[#words + 1] = word end
  local limit = math.max(1, math.min(200, math.floor(tonumber(p.limit) or 50)))
  local offset = math.max(0, math.floor(tonumber(p.offset) or 0))
  local matches, total, scanned, i = {}, 0, 0, 0
  while true do
    local cmd, name = reaper.kbd_enumerateActions(section_id, i)
    if not cmd or cmd == 0 then break end
    scanned = scanned + 1
    local named = action_named_id(cmd)
    if action_matches(words, name, named) then
      total = total + 1
      if total > offset and #matches < limit then
        matches[#matches + 1] = {
          command_id = cmd, name = name, named_id = named,
          toggle_state = action_toggle(cmd, section_id),
        }
      end
    end
    i = i + 1
  end
  return { section = section_name, section_id = section_id, query = p.query,
           total_matches = total, offset = offset, limit = limit,
           returned = #matches, scanned = scanned, actions = matches }
end

-- Actions that write to disk, mapped to the gate that already covers the
-- bridge's own verb for the same act. Everything else in the list is undoable
-- or harmless to the project; this table is deliberately the short, known set.
local ACTION_GATES = {
  [40026] = "allow_project_save",  -- File: Save project
  [40022] = "allow_project_save",  -- File: Save project as...
  [41895] = "allow_project_save",  -- File: Save new version of project
  [40015] = "allow_audio_writes",  -- File: Render project to disk...
  [41824] = "allow_audio_writes",  -- File: Render project, using the most recent render settings
  [42230] = "allow_audio_writes",  -- ...same, auto-close render dialog
}
local ACTION_REFUSED = {
  [40004] = "File: Quit REAPER would take the bridge down with it",
}

local function resolve_action(p)
  local section_id, section_name = action_section(p.section)
  local cmd
  if p.named_id ~= nil then
    local named = tostring(p.named_id)
    if named:sub(1, 1) ~= "_" then named = "_" .. named end
    cmd = reaper.NamedCommandLookup(named)
    if not cmd or cmd == 0 then error("ACTION_NOT_FOUND: no action is registered as " .. named) end
  elseif p.command_id ~= nil then
    cmd = math.floor(tonumber(p.command_id) or 0)
    if cmd <= 0 then error("BAD_PAYLOAD: command_id must be a positive integer") end
  else
    error("BAD_PAYLOAD: provide command_id (a number) or named_id (an _SWS_... / _RS... string)")
  end
  return cmd, section_id, section_name
end

local function command_run_action(command)
  local p = command.payload or {}
  local cmd, section_id, section_name = resolve_action(p)
  if ACTION_REFUSED[cmd] then error("ACTION_REFUSED: " .. ACTION_REFUSED[cmd]) end
  local gate_key = ACTION_GATES[cmd]
  if gate_key and not gate.allows(gate_key) then
    error("ACTION_BLOCKED: action " .. cmd .. " writes to disk and is gated; " .. gate.hint(gate_key))
  end
  local name = action_name(cmd, section_id)
  if name == nil and reaper.kbd_getTextFromCmd and p.named_id == nil then
    error("ACTION_NOT_FOUND: no action has command id " .. cmd .. " in the " .. section_name .. " section")
  end
  local toggle_before = action_toggle(cmd, section_id)
  if section_id == 0 or section_id == 100 then
    reaper.Main_OnCommand(cmd, 0)
  elseif section_id == 32060 or section_id == 32061 or section_id == 32062 then
    if not (reaper.MIDIEditor_GetActive and reaper.MIDIEditor_GetActive()) then
      error("NO_MIDI_EDITOR: open a MIDI editor before running a " .. section_name .. " action")
    end
    reaper.MIDIEditor_LastFocused_OnCommand(cmd, section_id == 32061)
  else
    error("UNSUPPORTED_SECTION: " .. section_name .. " actions cannot be run through the API")
  end
  return { command_id = cmd, named_id = p.named_id or action_named_id(cmd), name = name,
           section = section_name, ran = true,
           toggle_before = toggle_before, toggle_after = action_toggle(cmd, section_id) }
end

local SCRIPT_GATE = "allow_script_execution"
local SCRIPT_OUTPUT_CAP = 65536
local SCRIPT_DEFAULT_TIMEOUT = 30
local SCRIPT_MAX_TIMEOUT = 600

-- Strict: the specific key only, no allow_risk_level_3 fallback.
local function scripts_allowed()
  return config[SCRIPT_GATE] == true
end

local function script_gate_hint()
  return "run_script is off; set " .. SCRIPT_GATE .. " to true in bridge_config.json "
    .. "(python3 setup/install.py --allow-script-execution), then send reload_bridge. "
    .. "This gate has no allow_risk_level_3 fallback."
end

-- A relative path is tried under the daemon root first, then REAPER's own
-- Scripts folder, so "my_edit.lua" finds a ReaScript the user already runs.
local function script_source(p)
  if p.code ~= nil and p.path ~= nil then error("BAD_PAYLOAD: provide code or path, not both") end
  if p.code ~= nil then
    if type(p.code) ~= "string" or p.code == "" then error("BAD_PAYLOAD: code must be a non-empty string") end
    return p.code, "=agent_script", nil
  end
  if type(p.path) ~= "string" or p.path == "" then
    error("BAD_PAYLOAD: provide code (Lua source) or path (a .lua file)")
  end
  local path = p.path
  if path:find("[\r\n%z]") then error("BAD_PATH: path contains control characters") end
  if path:sub(-4):lower() ~= ".lua" then error("BAD_PATH: only .lua scripts run here; got " .. path) end
  local absolute = path:match("^%a:[/\\]") or path:match("^[/\\]")
  if not absolute then
    local candidates = { join(root, path) }
    if reaper.GetResourcePath then
      candidates[#candidates + 1] = join(reaper.GetResourcePath(), "Scripts", path)
    end
    local found = nil
    for _, candidate in ipairs(candidates) do
      if exists(candidate) then found = candidate; break end
    end
    if not found then
      error("SCRIPT_NOT_FOUND: " .. path .. " (looked under " .. table.concat(candidates, " and ") .. ")")
    end
    path = found
  elseif not exists(path) then
    error("SCRIPT_NOT_FOUND: " .. path)
  end
  local text = read_file(path)
  if not text then error("SCRIPT_UNREADABLE: " .. path) end
  return text, "@" .. path, path
end

-- Fresh globals for the script (reads fall through to the bridge's), behind a
-- `reaper` proxy that refuses deferred callbacks: a defer loop would ride the
-- bridge's own script chain, and one error inside it would stop the bridge.
-- ShowConsoleMsg is captured into the reply instead of opening the console.
local function script_env(capture)
  local blocked = { defer = true, atexit = true, runloop = true }
  local proxy = setmetatable({}, {
    __index = function(_, key)
      if blocked[key] then
        return function()
          error("SCRIPT_DEFER_UNSUPPORTED: reaper." .. key
            .. " is not available inside run_script; the script must finish in one pass", 0)
        end
      end
      if key == "ShowConsoleMsg" then return capture end
      return reaper[key]
    end,
    __newindex = function(_, key)
      error("SCRIPT_ENV: a script may not assign reaper." .. tostring(key), 0)
    end,
  })
  return setmetatable({ reaper = proxy }, { __index = _G })
end

-- Reply-safe copy of a script's return value: JSON types pass, tables are
-- copied (bounded depth), anything else becomes its tostring.
local function script_value(value, depth)
  depth = depth or 0
  local kind = type(value)
  if kind == "nil" or kind == "boolean" or kind == "string" then return value end
  if kind == "number" then
    if value ~= value or value == math.huge or value == -math.huge then return tostring(value) end
    return value
  end
  if kind == "table" and depth < 8 then
    local out = {}
    if #value > 0 then
      for i = 1, #value do out[i] = script_value(value[i], depth + 1) end
    else
      for k, v in pairs(value) do out[tostring(k)] = script_value(v, depth + 1) end
    end
    return out
  end
  return tostring(value)
end

local function command_run_script(command)
  local p = command.payload or {}
  if not scripts_allowed() then error("SCRIPT_BLOCKED: " .. script_gate_hint()) end
  local source, chunk_name, path = script_source(p)
  local timeout = tonumber(p.timeout_seconds) or SCRIPT_DEFAULT_TIMEOUT
  timeout = math.max(0.1, math.min(SCRIPT_MAX_TIMEOUT, timeout))

  local output, output_len, output_truncated = {}, 0, false
  local function capture(message)
    message = tostring(message)
    if output_len >= SCRIPT_OUTPUT_CAP then output_truncated = true; return end
    local room = SCRIPT_OUTPUT_CAP - output_len
    if #message > room then message = message:sub(1, room); output_truncated = true end
    output[#output + 1] = message
    output_len = output_len + #message
  end
  local env = script_env(capture)
  env.args = p.args
  local chunk, load_error = load(source, chunk_name, "t", env)
  if not chunk then error("SCRIPT_SYNTAX: " .. tostring(load_error)) end

  local started = reaper.time_precise()
  local can_hook = type(debug) == "table" and debug.sethook ~= nil
  if can_hook then
    debug.sethook(function()
      if reaper.time_precise() - started > timeout then
        debug.sethook()
        error("SCRIPT_TIMEOUT: the script ran longer than " .. timeout
          .. " s and was stopped; whatever it changed so far is one undo step", 0)
      end
    end, "", 10000)
  end
  local results = table.pack(xpcall(chunk, function(err)
    return { message = tostring(err), traceback = debug and debug.traceback and debug.traceback(tostring(err), 2) or nil }
  end, p.args))
  if can_hook then debug.sethook() end
  local elapsed_ms = (reaper.time_precise() - started) * 1000

  if not results[1] then
    local failure = results[2]
    local message = type(failure) == "table" and failure.message or tostring(failure)
    local code = message:match("^(SCRIPT_[A-Z_]+):")
    local detail = code and message or ("SCRIPT_ERROR: " .. message)
    if type(failure) == "table" and failure.traceback and not code then
      detail = detail .. "\n" .. failure.traceback
    end
    error(detail, 0)
  end
  return {
    ran = true, path = path, chunk = chunk_name,
    return_value = script_value(results[2]), return_count = results.n - 1,
    output = table.concat(output), output_truncated = output_truncated,
    elapsed_ms = math.floor(elapsed_ms * 100 + 0.5) / 100, timeout_seconds = timeout,
  }
end

-- Tempo map ------------------------------------------------------------------
-- set_tempo only moves the project's base tempo; these three read and write
-- positioned tempo and time-signature markers, which is what a mid-song tempo
-- change or a bar of 7/8 actually needs.

local function tempo_marker_row(index)
  local ok, time, measure, beat, bpm, num, denom, linear = reaper.GetTempoTimeSigMarker(0, index)
  if not ok then return nil end
  -- REAPER reports a tempo-only marker's meter as -1 (live) or 0 (API doc);
  -- either way it has none, so the reply leaves numerator/denominator out.
  local metered = (num or 0) > 0 and (denom or 0) > 0
  return { index = index, time = time, bar = bar_from_time(time), measure = measure, beat = beat,
           bpm = bpm, numerator = metered and num or nil, denominator = metered and denom or nil,
           linear = linear == true, has_time_signature = metered }
end

local function command_get_tempo_markers(command)
  local count = reaper.CountTempoTimeSigMarkers(0)
  local markers = {}
  for i = 0, count - 1 do markers[#markers + 1] = tempo_marker_row(i) end
  local num, denom, bpm = reaper.TimeMap_GetTimeSigAtTime(0, 0)
  return { count = count, base_tempo = reaper.Master_GetTempo(),
           at_start = { bpm = bpm, numerator = num, denominator = denom }, markers = markers }
end

-- The tempo in force at `time`, in the units SetTempoTimeSigMarker stores.
local function tempo_in_force(time)
  local idx = reaper.FindTempoTimeSigMarker and reaper.FindTempoTimeSigMarker(0, time) or -1
  if idx >= 0 then
    local row = tempo_marker_row(idx)
    if row then return row.bpm end
  end
  return reaper.Master_GetTempo()
end

local function check_time_signature(num, denom)
  if num < 1 or num > 255 then error("BAD_TIME_SIGNATURE: numerator must be 1-255") end
  local power = 1
  while power < denom do power = power * 2 end
  if denom < 1 or denom > 64 or power ~= denom then
    error("BAD_TIME_SIGNATURE: denominator must be 1, 2, 4, 8, 16, 32 or 64")
  end
end

local function command_set_tempo_marker(command)
  local p = command.payload or {}
  local count = reaper.CountTempoTimeSigMarkers(0)
  local index = -1
  if p.index ~= nil then
    index = math.floor(tonumber(p.index) or -1)
    if index < 0 or index >= count then
      error("NO_TEMPO_MARKER: index " .. tostring(p.index) .. " (project has " .. count .. ")")
    end
  end
  local existing = index >= 0 and tempo_marker_row(index) or nil

  local bpm = nil
  if p.bpm ~= nil then
    bpm = tonumber(p.bpm)
    if not bpm or bpm <= 0 then error("BAD_TEMPO: bpm must be positive") end
  end
  local num, denom = nil, nil
  if (p.numerator ~= nil) ~= (p.denominator ~= nil) then
    error("BAD_PAYLOAD: numerator and denominator go together")
  end
  if p.numerator ~= nil then
    num = math.floor(tonumber(p.numerator) or 0)
    denom = math.floor(tonumber(p.denominator) or 0)
    check_time_signature(num, denom)
  end
  if p.linear ~= nil and type(p.linear) ~= "boolean" then
    error("BAD_PAYLOAD: linear must be true or false")
  end
  if not existing and bpm == nil and num == nil then
    error("BAD_PAYLOAD: provide bpm, a time signature (numerator + denominator), or both")
  end

  local timepos, measurepos, beatpos, expected_time = -1, -1, -1, nil
  local position = p.position
  if position ~= nil then
    if position.type == "bar" then
      local bar = math.floor(tonumber(position.bar) or 0)
      if bar < 1 then error("BAD_POSITION: bar must be 1 or higher") end
      measurepos, beatpos = bar - 1, 0
      expected_time = time_from_bar(bar)
    else
      timepos = resolve_position(position)
      expected_time = timepos
      if num ~= nil then
        error("TIME_SIGNATURE_NEEDS_BAR: a time-signature change must sit on a bar; "
          .. "use position {type: bar, bar: N}")
      end
    end
  elseif existing then
    timepos, expected_time = existing.time, existing.time
  else
    error("BAD_PAYLOAD: position is required for a new tempo marker")
  end

  if bpm == nil then bpm = existing and existing.bpm or tempo_in_force(expected_time) end
  if num == nil then
    if existing and existing.has_time_signature then num, denom = existing.numerator, existing.denominator
    else num, denom = 0, 0 end
  end
  local linear = p.linear
  if linear == nil then linear = existing and existing.linear or false end

  local ok = reaper.SetTempoTimeSigMarker(0, index, timepos, measurepos, beatpos, bpm, num, denom, linear)
  if not ok then error("TEMPO_MARKER_FAILED: REAPER refused the tempo marker") end
  if reaper.UpdateTimeline then reaper.UpdateTimeline() end

  local written_time = measurepos >= 0 and time_from_bar(measurepos + 1) or expected_time
  local found = reaper.FindTempoTimeSigMarker(0, written_time + 0.0005)
  local row = found >= 0 and tempo_marker_row(found) or nil
  if not row or math.abs(row.time - written_time) > 0.001 then
    error("TEMPO_MARKER_VERIFY_FAILED: no marker read back at " .. tostring(written_time) .. " s; undo available")
  end
  return { marker = row, created = index < 0, count = reaper.CountTempoTimeSigMarkers(0) }
end

local function command_delete_tempo_marker(command)
  local p = command.payload or {}
  local count = reaper.CountTempoTimeSigMarkers(0)
  local index
  if p.index ~= nil then
    index = math.floor(tonumber(p.index) or -1)
  elseif p.position ~= nil then
    local time = resolve_position(p.position)
    index = reaper.FindTempoTimeSigMarker(0, time + 0.0005)
    local row = index >= 0 and tempo_marker_row(index) or nil
    if not row or math.abs(row.time - time) > 0.001 then
      error("NO_TEMPO_MARKER: no tempo marker at that position")
    end
  else
    error("BAD_PAYLOAD: provide index or position")
  end
  if index < 0 or index >= count then
    error("NO_TEMPO_MARKER: index " .. tostring(index) .. " (project has " .. count .. ")")
  end
  local row = tempo_marker_row(index)
  if not reaper.DeleteTempoTimeSigMarker(0, index) then
    error("TEMPO_MARKER_FAILED: REAPER refused to delete tempo marker " .. index)
  end
  if reaper.UpdateTimeline then reaper.UpdateTimeline() end
  return { deleted = row, count = reaper.CountTempoTimeSigMarkers(0) }
end

return {
  list_actions = command_list_actions,
  run_action = command_run_action,
  run_script = command_run_script,
  get_tempo_markers = command_get_tempo_markers,
  set_tempo_marker = command_set_tempo_marker,
  delete_tempo_marker = command_delete_tempo_marker,
  script_value = script_value,
  action_section = action_section,
}
