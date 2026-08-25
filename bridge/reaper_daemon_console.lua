-- reaper_daemon_console.lua -- Daemon Console, the panel
--
-- A chat box docked inside REAPER. He types, the sidecar's headless Claude
-- session answers, and the answer arrives without an alt-tab, without retyping
-- what track is selected, and without leaving the transport.
--
-- One exception to "it only moves files": the audition strip. Locate, A/B and
-- Undo run as ordinary REAPER API calls in this process, because the panel IS
-- inside REAPER. No bridge round trip, no render, no paid turn.
--
-- Otherwise this panel owns NO logic about what Claude should do. It moves four
-- things:
--   prompt out    console/prompts/<stamp>.json   (with the focus envelope)
--   control out   console/control/<stamp>.json   (interrupt, ack, restart)
--   transcript in console/events/<session>.jsonl (tailed by byte offset)
--   status in     console/state.json             (replaced by the sidecar)
-- plus one heartbeat out, console/panel.json, which is the sidecar's dead-man
-- switch. If this panel dies, that file stops moving and the paid session is
-- killed rather than left running.
--
-- Deliberately NOT a checklist. The first harness that lived at this path was
-- a grid of self-tests and the verdict was "this is too complicated". The
-- surface here is: what is going on, what did it say, what do I want to say.
--
-- Requires ReaImGui. Written against 0.10.x / Dear ImGui 1.92, where font size
-- moved to PushFont and CreateFont lost its size parameter.

local SCRIPT_NAME = "Daemon Console"
local EXT_SECTION = "reaper_daemon_console"

-- ---------------------------------------------------------------------------
-- Paths
-- ---------------------------------------------------------------------------

local sep = package.config:sub(1, 1)
local function join(...) return table.concat({ ... }, sep) end

local SCRIPT_DIR, BRIDGE_ROOT
do
  local info = debug.getinfo(1, "S").source:sub(2)
  SCRIPT_DIR = info:match("^(.*)[/\\][^/\\]+$") or "."
  BRIDGE_ROOT = SCRIPT_DIR:match("^(.*)[/\\][^/\\]+$") or SCRIPT_DIR
end
package.path = join(SCRIPT_DIR, "?.lua") .. ";" .. package.path

local json = require("json")

local P = {
  console  = join(BRIDGE_ROOT, "console"),
  state    = join(BRIDGE_ROOT, "console", "state.json"),
  panel    = join(BRIDGE_ROOT, "console", "panel.json"),
  prompts  = join(BRIDGE_ROOT, "console", "prompts"),
  control  = join(BRIDGE_ROOT, "console", "control"),
  events   = join(BRIDGE_ROOT, "console", "events"),
  log      = join(BRIDGE_ROOT, "logs", "console_panel.log"),
  sidecar  = join(BRIDGE_ROOT, "console_sidecar.py"),
}

-- ---------------------------------------------------------------------------
-- Pure helpers (exported for the self-test; no REAPER, no ImGui)
-- ---------------------------------------------------------------------------

local M = {}

local function write_file(path, text)
  local file, err = io.open(path, "wb")
  if not file then return nil, err end
  file:write(text)
  file:close()
  return true
end

-- Same semantics as reaper_agent_bridge.lua:191, Windows fallback included:
-- os.rename there cannot replace an existing destination, so every overwrite
-- fails the first attempt and needs remove + retry. Replace-style files only.
-- The .jsonl streams are append-only and must never be renamed over.
function M.atomic_write_json(path, value)
  local tmp = path .. ".tmp"
  local ok, err = write_file(tmp, json.encode(value))
  if not ok then return nil, err end
  ok, err = os.rename(tmp, path)
  if not ok then
    os.remove(path)
    ok, err = os.rename(tmp, path)
  end
  if not ok then
    os.remove(tmp)
    return nil, err
  end
  return true
end

function M.read_json_file(path)
  local file = io.open(path, "rb")
  if not file then return nil end
  local text = file:read("a")
  file:close()
  if not text or text == "" then return nil end
  local ok, value = pcall(json.decode, text)
  if not ok then return nil end
  return value
end

-- Byte-offset tailer with a carry buffer. Never seeks to end: it opens at the
-- stored offset, advances by exactly the bytes consumed, and keeps everything
-- after the last newline so a line caught mid-append is completed next poll
-- instead of being decoded as garbage. Proven at 62/62 lines over 484 polls in
-- the spike. A file that shrank was rotated, so reset rather than read noise.
function M.tail_read(state, path, on_line)
  local file = io.open(path, "rb")
  if not file then return 0 end
  local size = file:seek("end")
  if size < state.offset then
    state.offset, state.carry = 0, ""
  end
  file:seek("set", state.offset)
  local chunk = file:read(1024 * 256)
  file:close()
  if not chunk or #chunk == 0 then return 0 end
  state.offset = state.offset + #chunk
  state.carry = state.carry .. chunk
  local count = 0
  while true do
    local nl = state.carry:find("\n", 1, true)
    if not nl then break end
    local line = state.carry:sub(1, nl - 1)
    state.carry = state.carry:sub(nl + 1)
    if #line > 0 then
      count = count + 1
      on_line(line)
    end
  end
  return count
end

-- Ids are timestamp-prefixed so a directory listing sorts chronologically.
-- The bridge learned that one the hard way (reaper_agent_bridge.lua:3184).
local id_counter = 0
function M.next_id(stamp)
  id_counter = id_counter + 1
  return string.format("%s-%04x%02x", stamp, math.random(0, 0xffff) , id_counter % 256)
end

-- At most `limit` CHARACTERS, not bytes. Model output is full of non-ASCII and
-- a byte slice lands mid-character often enough to matter; ImGui renders the
-- orphaned halves as tofu, which looks like corruption in the transcript.
function M.short(text, limit)
  text = tostring(text or ""):gsub("%s+", " ")
  local chars = utf8.len(text)
  if not chars then
    -- Already not valid UTF-8 (a torn line, or the U+FFFD the CLI sometimes
    -- emits). Fall back to bytes rather than refusing to display it at all.
    if #text <= limit then return text end
    return text:sub(1, limit - 1) .. "\u{2026}"
  end
  if chars <= limit then return text end
  local cut = utf8.offset(text, limit)
  return text:sub(1, (cut or limit) - 1) .. "\u{2026}"
end

function M.money(value)
  local n = tonumber(value)
  if not n then return "$0.00" end
  if n > 0 and n < 0.01 then return "<$0.01" end
  return string.format("$%.2f", n)
end

function M.duration(ms)
  local n = tonumber(ms)
  if not n then return "" end
  if n < 1000 then return string.format("%dms", math.floor(n)) end
  return string.format("%.1fs", n / 1000)
end

-- The transcript reducer.
--
-- Assistant text arrives twice: streamed as `text_delta` while the model is
-- talking, then once more as a complete `text` block. Replacing the streamed
-- block with the final text (rather than appending it) keeps the transcript
-- correct even if a delta was dropped, and is why a finished answer never
-- appears doubled.
--
-- `blocks` is mutated in place and returned. Anything unrecognized is ignored:
-- a newer sidecar emitting a kind this panel predates must not break the view.
function M.apply_event(blocks, event, cap)
  cap = cap or 300
  if type(event) ~= "table" then return blocks end
  local kind = event.t

  local function last_open(role)
    local block = blocks[#blocks]
    if block and block.role == role and block.open then return block end
    return nil
  end

  local function push(block)
    block.ts = event.ts
    blocks[#blocks + 1] = block
    -- Drop from the front, never the back: the newest turn is the one he is
    -- reading. ListClipper cannot be used with wrapped variable-height text,
    -- so the cap is what keeps a long session's frame time flat.
    while #blocks > cap do table.remove(blocks, 1) end
    return block
  end

  if kind == "user" then
    push({ role = "user", text = event.text or "" })

  elseif kind == "text_delta" then
    local block = last_open("assistant")
    if block then
      block.text = block.text .. (event.text or "")
    else
      push({ role = "assistant", text = event.text or "", open = true })
    end

  elseif kind == "text" then
    local block = last_open("assistant")
    if block then
      block.text = event.text or block.text
      block.open = false
    else
      push({ role = "assistant", text = event.text or "" })
    end

  elseif kind == "thinking" then
    local block = last_open("thinking")
    if block and event.delta then
      block.text = block.text .. (event.text or "")
    elseif event.delta then
      push({ role = "thinking", text = event.text or "", open = true })
    elseif block then
      block.text = event.text or block.text
      block.open = false
    end

  elseif kind == "tool" then
    -- A tool call closes whatever the model was saying: the next text block is
    -- a new paragraph, not a continuation of the sentence before the call.
    local open = last_open("assistant")
    if open then open.open = false end
    push({ role = "tool", id = event.id, name = event.name or "?",
           input = event.input or "", text = "", verdict = "running" })

  elseif kind == "tool_result" then
    for i = #blocks, 1, -1 do
      if blocks[i].role == "tool" and blocks[i].id == event.id then
        blocks[i].verdict = event.verdict or (event.ok and "ok" or "error")
        blocks[i].ok = event.ok
        blocks[i].text = event.text or ""
        blocks[i].measurement = event.measurement
        break
      end
    end

  elseif kind == "turn_end" then
    local open = last_open("assistant")
    if open then open.open = false end
    push({ role = "meta",
           text = string.format("turn %s  %s  %s",
             event.subtype or "done",
             M.duration(event.duration_ms),
             M.money(event.turn_cost_usd)) })

  elseif kind == "error" then
    push({ role = "error",
           text = string.format("%s: %s", event.code or "ERROR",
                                event.details or "") })

  elseif kind == "session_end" then
    push({ role = "meta", text = "session ended (" .. (event.reason or "?") .. ")" })
  end

  return blocks
end

-- Liveness without a clock. state.json is republished every poll_interval
-- (console_sidecar.py:1885), so a changing `updated_at` means alive. Parsing
-- the ISO stamp and comparing to os.time() would drag in a timezone
-- conversion for no gain; watching the string change does not.
-- Returns age and the number of changes SEEN BY THIS PANEL. The change count
-- matters at open: a state.json left behind by a sidecar that died days ago is
-- perfectly readable, and trusting it on sight would show stale cost and bridge
-- numbers as though they were live. The first sight is only a baseline.
function M.freshness(tracker, updated_at, now)
  if not tracker.seeded then
    tracker.seeded = true
    tracker.last_seen = updated_at
    tracker.changed_at = now
    tracker.changes = 0
  elseif updated_at ~= tracker.last_seen then
    tracker.last_seen = updated_at
    tracker.changed_at = now
    tracker.changes = tracker.changes + 1
  end
  return now - tracker.changed_at, tracker.changes
end

-- ---------------------------------------------------------------------------
-- The audition loop
-- ---------------------------------------------------------------------------
--
-- The sidecar publishes `last_change` in state.json (console_sidecar.py's
-- describe_change): what the agent changed, on which track, in which bars, and
-- whether an FX exists to toggle in and out. This half turns that into three
-- buttons and five verdicts so a "too much" costs one click instead of a
-- retyped sentence.

-- The bar range's provenance is shown, never hidden. `time_selection` means the
-- tool did not name bars and this is where HE was looking when he asked, which
-- is a good guess and a different claim from the tool reporting what it wrote.
function M.change_bars_label(bars)
  if type(bars) ~= "table" or not bars.start then return nil end
  local span
  if bars["end"] and bars["end"] ~= bars.start then
    span = string.format("bars %d-%d", bars.start, bars["end"])
  else
    span = string.format("bar %d", bars.start)
  end
  if bars.source == "time_selection" then span = span .. " (your selection)" end
  return span
end

function M.change_line(change)
  if type(change) ~= "table" then return nil end
  local parts = { M.short(change.summary or change.tool or "?", 90) }
  local bars = M.change_bars_label(change.bars)
  if bars then parts[#parts + 1] = bars end
  if change.verdict == "unverified" then
    -- The mutation was sent and nothing measured it. Say so on the strip, not
    -- only in the tool block he has to expand.
    parts[#parts + 1] = "unverified"
  end
  return table.concat(parts, "  |  ")
end

-- The five verdicts. Each one restates the target so the model never has to ask
-- which change was meant, and each one is a complete instruction on its own:
-- the panel cannot assume the model still has the change in its context after a
-- compaction. `keep` is deliberately absent — keeping costs nothing and must
-- not spend a turn.
M.VERDICTS = {
  { key = "too_much",   label = "too much",
    template = "Too much on the last change (%s). Same target, smaller move." },
  { key = "not_enough", label = "not enough",
    template = "Not enough on the last change (%s). Same target, push it further." },
  { key = "wrong",      label = "wrong direction",
    template = "Wrong direction on the last change (%s). Undo it, then try the " ..
               "opposite move on the same target." },
  { key = "another",    label = "try another",
    template = "That did not work (%s). Undo it, then try a different approach " ..
               "to the same goal on the same target." },
}

function M.verdict_prompt(change, key)
  if type(change) ~= "table" then return nil end
  for _, verdict in ipairs(M.VERDICTS) do
    if verdict.key == key then
      local target = change.summary or change.tool or "the last change"
      local bars = M.change_bars_label(change.bars)
      if bars then target = target .. ", " .. bars end
      return string.format(verdict.template, target)
    end
  end
  return nil
end

-- A change is auditionable by ear only when there is an FX to toggle. Returns
-- nil plus the reason otherwise, and the reason is shown: a button that quietly
-- does nothing is the Send bug all over again.
function M.ab_reason(change)
  if type(change) ~= "table" then return nil, "no change yet" end
  if change.ab == "fx" then return true, nil end
  return nil, change.ab_blocked or "nothing to toggle on this change"
end

-- Pure guard for an evidence-backed parameter restore. `restore` means the
-- value is still exactly the agent's result; `already` is idempotent; `changed`
-- refuses to overwrite a later human or agent edit.
function M.restore_decision(current, before, after, epsilon)
  current, before, after = tonumber(current), tonumber(before), tonumber(after)
  if not current or not before or not after then return "incomplete" end
  epsilon = tonumber(epsilon) or 0.000001
  if math.abs(current - before) <= epsilon then return "already" end
  if math.abs(current - after) <= epsilon then return "restore" end
  return "changed"
end

if _G.REAPER_CONSOLE_SELFTEST then return M end

-- ---------------------------------------------------------------------------
-- REAPER + ImGui
-- ---------------------------------------------------------------------------

if not reaper.ImGui_GetBuiltinPath then
  reaper.MB("Daemon Console needs ReaImGui.\n\nInstall it in ReaPack: " ..
            "Extensions > ReaPack > Browse packages > ReaImGui.",
            SCRIPT_NAME, 0)
  return
end

package.path = reaper.ImGui_GetBuiltinPath() .. "/?.lua;" .. package.path
local ImGui = require("imgui")("0.10")
local focus = require("reaper_focus")

-- Resolve every enum once, loudly. A missing constant surfaces here with its
-- own name rather than as a nil arithmetic error inside a frame callback.
local function need(name)
  local value = ImGui[name]
  if value == nil then
    error(string.format("ReaImGui is missing %s; this panel needs 0.10 or newer", name))
  end
  return value
end

local FLAGS = {
  window_none      = need("WindowFlags_None"),
  child_borders    = need("ChildFlags_Borders"),
  cond_first       = need("Cond_FirstUseEver"),
  col_text         = need("Col_Text"),
  -- Optional: present in 0.10 here, but neither is load-bearing. Escape has a
  -- documented fallback (Escape then space) and Copy simply hides itself.
  cfg_nav_escape   = ImGui.ConfigVar_NavEscapeClearFocusItem,
  can_copy         = ImGui.SetClipboardText ~= nil,
}

local COLOR = {
  user      = 0x9fd4ffff,
  assistant = 0xe8e8e8ff,
  thinking  = 0x7a7a7aff,
  tool      = 0xc9a86aff,
  ok        = 0x86c98aff,
  bad       = 0xe08a7aff,
  meta      = 0x6f6f6fff,
  dim       = 0x8a8a8aff,
}

-- ---------------------------------------------------------------------------
-- Panel state
-- ---------------------------------------------------------------------------

local S = {
  ctx = nil,
  blocks = {},
  tail = { offset = 0, carry = "" },
  events_file = nil,
  state = nil,
  fresh = {},
  sidecar_age = 0,
  sidecar_changes = 0,
  input = "",
  focus_env = nil,
  focus_line = "",
  status_note = nil,
  note_until = 0,
  last_poll = 0,
  last_beat = 0,
  last_focus = 0,
  frame = 0,
  force_bottom = true,
  spawn_attempted = false,
  edits = 0,
  seen_tools = {},
  ab_enabled = nil,
  ab_change_id = nil,
  change_dismissed = nil,
  dock_applied = false,
  start_time = 0,
}

local function log(line)
  local file = io.open(P.log, "a")
  if not file then return end
  file:write(string.format("%s %s\n", os.date("!%Y-%m-%dT%H:%M:%SZ"), line))
  file:close()
end

local function note(text, seconds)
  S.status_note = text
  S.note_until = reaper.time_precise() + (seconds or 6)
  log("note: " .. tostring(text))
end

local function stamp()
  return os.date("!%Y%m%dT%H%M%SZ")
end

local function write_control(action, extra)
  local payload = extra or {}
  payload.action = action
  payload.ts = os.date("!%Y-%m-%dT%H:%M:%SZ")
  local path = join(P.control, M.next_id(stamp()) .. ".json")
  local ok, err = M.atomic_write_json(path, payload)
  if not ok then note("control write failed: " .. tostring(err)) end
  return ok
end

-- ---------------------------------------------------------------------------
-- Sidecar autostart
-- ---------------------------------------------------------------------------
--
-- ExecProcess with timeoutmsec -2 is a detached no-wait launch. The verdict is
-- never ExecProcess's return value (it reports on the `start` shim, which exits
-- immediately whether or not the real process came up); it is state.json
-- beginning to move.

local function python_exe()
  local configured = reaper.GetExtState(EXT_SECTION, "python_path")
  if configured and configured ~= "" then return configured end
  if sep == "\\" then
    for _, candidate in ipairs({
      "C:\\Python314\\pythonw.exe", "C:\\Python313\\pythonw.exe",
      "C:\\Python312\\pythonw.exe",
    }) do
      local probe = io.open(candidate, "rb")
      if probe then probe:close(); return candidate end
    end
    return "pythonw.exe"
  end
  return "python3"
end

local function spawn_sidecar()
  local exe = python_exe()
  local cmd
  if sep == "\\" then
    cmd = string.format('cmd.exe /C start /B "" "%s" "%s"', exe, P.sidecar)
  else
    cmd = string.format('"%s" "%s" &', exe, P.sidecar)
  end
  log("spawn: " .. cmd)
  reaper.ExecProcess(cmd, -2)
  note("Starting the sidecar\u{2026}", 10)
end

-- ---------------------------------------------------------------------------
-- Polling
-- ---------------------------------------------------------------------------

local function switch_session(events_file)
  S.events_file = events_file
  S.tail = { offset = 0, carry = "" }
  S.blocks = {}
  S.seen_tools = {}
  log("session file: " .. tostring(events_file))
end

local function poll()
  local now = reaper.time_precise()
  if now - S.last_poll < 0.2 then return end
  S.last_poll = now

  local state = M.read_json_file(P.state)
  if state then
    S.state = state
    S.sidecar_age, S.sidecar_changes = M.freshness(S.fresh, state.updated_at, now)
    local events_file = state.events_file
    if events_file and events_file ~= "" then
      local absolute = join(BRIDGE_ROOT, (events_file:gsub("/", sep)))
      if absolute ~= S.events_file then switch_session(absolute) end
    end
  else
    S.sidecar_age = math.huge
  end

  if S.events_file then
    M.tail_read(S.tail, S.events_file, function(line)
      local ok, event = pcall(json.decode, line)
      if not ok or type(event) ~= "table" then return end
      -- Count agent edits so an interleaved Ctrl+Z never surprises him: the
      -- undo stack is shared with his own edits, and "one Ctrl+Z" is true per
      -- bridge command, not per session.
      if event.t == "tool_result" and event.ok and event.id
         and not S.seen_tools[event.id] then
        S.seen_tools[event.id] = true
        S.edits = S.edits + 1
      end
      M.apply_event(S.blocks, event, 300)
    end)
  end
end

local function beat()
  local now = reaper.time_precise()
  if now - S.last_beat < 1.0 then return end
  S.last_beat = now
  M.atomic_write_json(P.panel, {
    alive_at = os.date("!%Y-%m-%dT%H:%M:%SZ"),
    uptime_seconds = now - S.start_time,
    frame = S.frame,
    blocks = #S.blocks,
    dock_id = tonumber(reaper.GetExtState(EXT_SECTION, "dock_id")) or 0,
  })
end

local function refresh_focus()
  local now = reaper.time_precise()
  if now - S.last_focus < 0.25 then return end
  S.last_focus = now
  S.focus_env = focus.envelope(0)
  S.focus_line = focus.summary(S.focus_env)
end

-- ---------------------------------------------------------------------------
-- Sending
-- ---------------------------------------------------------------------------

local function send(text)
  text = (text or ""):gsub("^%s+", ""):gsub("%s+$", "")
  if text == "" then
    -- Never fail silently again. An empty send used to return with no note and
    -- no log line, which is indistinguishable from a dead button.
    note("Nothing to send.")
    return false
  end

  local state = S.state
  if not state or S.sidecar_changes == 0 or S.sidecar_age > 10 then
    note("The sidecar is not running. Press Start.")
    return false
  end

  -- Project guard. The sidecar's MCP child talks to whatever project REAPER
  -- has open; if he switched projects since it started, a queued edit lands in
  -- the wrong one. Refuse rather than guess.
  local env = S.focus_env or focus.envelope(0)
  if state.project_name and env.project_name
     and state.project_name ~= env.project_name then
    note(string.format("Project changed (%s -> %s). Restart the session.",
                       state.project_name, env.project_name), 12)
    return false
  end

  if state.warn_pending then
    note("Last turn was expensive. Acknowledge the cost first.")
    return false
  end

  local path = join(P.prompts, M.next_id(stamp()) .. ".json")
  local ok, err = M.atomic_write_json(path, {
    text = text, source = "panel", focus = env,
    ts = os.date("!%Y-%m-%dT%H:%M:%SZ"),
  })
  if not ok then
    note("Could not write the prompt: " .. tostring(err))
    return false
  end
  -- Jump to the newest message: he just spoke, so he is not reading history.
  S.force_bottom = true
  return true
end

-- ---------------------------------------------------------------------------
-- Audition: resolving the change against live REAPER
-- ---------------------------------------------------------------------------
--
-- Every one of these runs in-process. The panel IS inside REAPER, so Locate,
-- A/B and Restore/Undo need no bridge round trip, no render and no paid turn: they are
-- ordinary API calls on the UI thread and they land in the same frame.

local function track_by_name(name, exact)
  if not name or name == "" then return nil end
  local wanted = name:lower()
  local found
  for i = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, i)
    local _, current = reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "", false)
    current = (current or ""):lower()
    if exact then
      if current == wanted then return track end
    elseif current:find(wanted, 1, true) then
      -- Substring selectors can hit two tracks. The bridge refuses that case
      -- (AMBIGUOUS), and so does this: acting on the wrong guitar is worse
      -- than a button that explains itself.
      if found then return nil, "more than one track matches " .. name end
      found = track
    end
  end
  return found, found and nil or ("no track named " .. name)
end

local function resolve_track(selector)
  if type(selector) ~= "table" then return nil, "the change named no track" end
  if selector.kind == "selected" then
    local track = reaper.GetSelectedTrack(0, 0)
    return track, track and nil or "no track is selected"
  end
  return track_by_name(selector.value, selector.kind == "track")
end

local function track_by_guid(guid)
  if not guid or guid == "" then return nil end
  local wanted = guid:upper()
  local master = reaper.GetMasterTrack(0)
  if master and (reaper.GetTrackGUID(master) or ""):upper() == wanted then
    return master
  end
  for i = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, i)
    if (reaper.GetTrackGUID(track) or ""):upper() == wanted then return track end
  end
  return nil
end

local function resolve_fx(track, change)
  if not track then return nil end
  if type(change.fx_index) == "number" then
    local index = math.floor(change.fx_index)
    if index >= 0 and index < reaper.TrackFX_GetCount(track) then return index end
    return nil, "FX index " .. index .. " is gone"
  end
  local wanted = (change.fx_name_contains or ""):lower()
  if wanted == "" then return nil, "the change named no FX" end
  for i = 0, reaper.TrackFX_GetCount(track) - 1 do
    local _, name = reaper.TrackFX_GetFXName(track, i, "")
    if (name or ""):lower():find(wanted, 1, true) then return i end
  end
  return nil, "no FX matching " .. change.fx_name_contains
end

-- Bars to seconds through REAPER's own tempo map. Never 240/tempo: that is
-- wrong for any project that is not 4/4, and the bridge says so emphatically
-- at reaper_agent_bridge.lua:366.
local function bar_start_time(bar)
  return reaper.TimeMap2_beatsToTime(0, 0.0, math.max(0, (tonumber(bar) or 1) - 1))
end

local function locate(change)
  local bars = change.bars
  if type(bars) ~= "table" or not bars.start then
    note("That change did not say which bars it touched.")
    return
  end
  local first = bar_start_time(bars.start)
  -- The end bar is the bar the change ENDS IN, so the range runs to the top of
  -- the one after it. Stopping at the end bar's downbeat would loop a range
  -- that cuts the last bar off entirely.
  local last = bar_start_time((bars["end"] or bars.start) + 1)
  reaper.GetSet_LoopTimeRange(true, true, first, last, false)
  -- moveview scrolls to it; it does not zoom. Zooming to the selection would
  -- throw away whatever view he had set up.
  reaper.SetEditCurPos(first, true, false)
  note(string.format("Located %s.", M.change_bars_label(bars)), 4)
end

-- A/B is an FX enable toggle, never undo/redo. Undo/redo A/B stops telling the
-- truth the moment he makes an edit of his own between presses, and the undo
-- stack is already shared with him.
local function toggle_ab(change)
  local track, why = resolve_track(change.track)
  if not track then note("Cannot A/B: " .. (why or "track not found"), 8); return end
  local index, fx_why = resolve_fx(track, change)
  if not index then note("Cannot A/B: " .. (fx_why or "FX not found"), 8); return end

  local enabled = reaper.TrackFX_GetEnabled(track, index)
  reaper.TrackFX_SetEnabled(track, index, not enabled)
  local _, name = reaper.TrackFX_GetFXName(track, index, "")
  S.ab_enabled = not enabled
  note(string.format("%s %s", M.short(name, 40),
                     S.ab_enabled and "ON (after)" or "OFF (before)"), 4)
end

-- Loop-play whatever Locate set up, so before and after are heard in the same
-- window without reaching for the transport.
local function loop_play()
  -- -1 queries. NOT `if not reaper.GetSetRepeat(-1)`: 0 is truthy in Lua, so
  -- that test can never fire and repeat would never come on.
  if reaper.GetSetRepeat(-1) == 0 then reaper.GetSetRepeat(1) end
  local range_start = reaper.GetSet_LoopTimeRange(false, false, 0, 0, false)
  reaper.SetEditCurPos(range_start, false, true)
  reaper.Main_OnCommand(1007, 0)  -- Transport: Play
end

-- 40029 is Edit: Undo. One press per bridge command, not one per session: the
-- undo stack is shared with his own edits, which is what the edit counter in
-- the status strip is for.
local function undo_last()
  reaper.Main_OnCommand(40029, 0)
  note("Ctrl+Z sent. The stack is shared with your own edits.", 6)
end

-- A/B bypass toggles can add their own undo history after the agent edit. For
-- set_fx_param the bridge result gives us a stronger path: stable identities
-- plus exact before/after normalized values. Restore only while the parameter
-- still equals the recorded after-value, so this can never overwrite a later
-- human or agent adjustment.
local function restore_or_undo(change)
  local restore = type(change) == "table" and change.restore or nil
  if type(restore) ~= "table" or restore.kind ~= "fx_param" then
    undo_last()
    return
  end

  local track = track_by_guid(restore.track_guid)
  if not track then note("Cannot restore: the target track is gone.", 8); return end
  local fx = tonumber(restore.fx_api_index)
  local param = tonumber(restore.param_index)
  if not fx or not param then note("Cannot restore: incomplete parameter identity.", 8); return end
  if (reaper.TrackFX_GetFXGUID(track, fx) or ""):upper()
       ~= tostring(restore.fx_guid or ""):upper() then
    note("Cannot restore: the target FX moved or was replaced.", 8)
    return
  end

  local current = reaper.TrackFX_GetParamNormalized(track, fx, param)
  local before = tonumber(restore.before_normalized)
  local after = tonumber(restore.after_normalized)
  if not current or not before or not after then
    note("Cannot restore: the recorded values are incomplete.", 8)
    return
  end
  local decision = M.restore_decision(current, before, after)
  if decision == "already" then
    note("Already restored to " .. tostring(restore.before_formatted or "the prior value") .. ".", 5)
    return
  end
  if decision == "changed" then
    note("Cannot restore: this parameter changed again after the agent edit.", 8)
    return
  end
  if decision ~= "restore" then
    note("Cannot restore: the recorded values are incomplete.", 8)
    return
  end

  reaper.Undo_BeginBlock2(0)
  reaper.TrackFX_SetParamNormalized(track, fx, param, before)
  reaper.TrackFX_EndParamEdit(track, fx, param)
  reaper.Undo_EndBlock2(0, "Daemon Console: restore FX parameter", -1)
  S.change_dismissed = change.id
  S.ab_enabled = nil
  note("Restored " .. tostring(restore.before_formatted or "the prior value") .. ".", 5)
end

-- ---------------------------------------------------------------------------
-- Drawing
-- ---------------------------------------------------------------------------

local function text_colored(color, text)
  ImGui.PushStyleColor(S.ctx, FLAGS.col_text, color)
  ImGui.TextWrapped(S.ctx, text)
  ImGui.PopStyleColor(S.ctx)
end

local function draw_status()
  local state = S.state

  -- No state file, or a stamp that stopped moving: nothing is publishing, so
  -- the sidecar is down whatever the file still says.
  -- Alive means "publishing", proven by a stamp that moved since this panel
  -- opened, not merely by a readable file.
  if state == nil or S.sidecar_changes == 0 or S.sidecar_age > 10 then
    text_colored(COLOR.bad, "Sidecar: not running")
    ImGui.SameLine(S.ctx)
    if ImGui.Button(S.ctx, "Start") then spawn_sidecar() end
    return
  end

  local bridge, bridge_color
  if state.bridge_busy and state.bridge_busy ~= "none" then
    bridge, bridge_color = "BUSY (" .. state.bridge_busy .. "), panel frozen", COLOR.tool
  elseif state.bridge_ok then
    bridge, bridge_color = "CONNECTED", COLOR.ok
  else
    bridge, bridge_color = "STALE", COLOR.bad
  end

  -- One composed line per row, one draw. Chaining SameLine into TextWrapped
  -- fights the wrap point and the strip stops making sense in a narrow dock.
  text_colored(bridge_color, "bridge " .. bridge)
  -- The edit counter is not decoration: the undo stack is shared with his own
  -- edits, so "one Ctrl+Z" is true per bridge command, not per session.
  text_colored(COLOR.dim, string.format("%s  |  %s session, %s today  |  %d agent edits%s",
    state.model or "?",
    M.money(state.session_cost_usd),
    M.money(state.daily_cost_usd),
    S.edits,
    (state.queue_depth or 0) > 0 and string.format("  |  %d queued", state.queue_depth) or ""))

  if state.turn_in_flight then
    if ImGui.Button(S.ctx, "Stop") then
      write_control("interrupt")
      note("Interrupt sent.")
    end
    ImGui.SameLine(S.ctx)
    text_colored(COLOR.dim, "working\u{2026}")
  end

  if state.warn_pending then
    text_colored(COLOR.bad, "That turn cost more than the warn threshold.")
    if ImGui.Button(S.ctx, "Acknowledge and continue") then
      write_control("ack_warn")
    end
  end

  if state.last_error then
    text_colored(COLOR.bad, string.format("%s: %s",
      state.last_error.code or "ERROR",
      M.short(state.last_error.details, 160)))
  end
end

local function draw_block(index, block)
  ImGui.PushID(S.ctx, index)

  if block.role == "user" then
    text_colored(COLOR.user, "> " .. block.text)

  elseif block.role == "assistant" then
    text_colored(COLOR.assistant, block.text)
    -- Text* is not selectable in ImGui, and he will want to lift a parameter
    -- list out of an answer.
    if FLAGS.can_copy and not block.open and #block.text > 0 then
      if ImGui.SmallButton(S.ctx, "Copy") then
        ImGui.SetClipboardText(S.ctx, block.text)
      end
    end

  elseif block.role == "thinking" then
    if ImGui.CollapsingHeader(S.ctx, "thinking") then
      text_colored(COLOR.thinking, block.text)
    end

  elseif block.role == "tool" then
    -- Collapsed by default. A tool call is scaffolding: he wants to see THAT
    -- it measured the bass, not the 400 lines it got back, unless he asks.
    local label = string.format("%s  %s  %s",
      (block.name or "?"):gsub("^mcp__reaper%-daemon__", ""),
      block.verdict or "",
      M.short(block.input, 70))
    if ImGui.CollapsingHeader(S.ctx, label) then
      text_colored(COLOR.dim, M.short(block.text, 4000))
      if FLAGS.can_copy and #block.text > 0
         and ImGui.SmallButton(S.ctx, "Copy result") then
        ImGui.SetClipboardText(S.ctx, block.text)
      end
    end

  elseif block.role == "error" then
    text_colored(COLOR.bad, block.text)

  else
    text_colored(COLOR.meta, block.text)
  end

  ImGui.PopID(S.ctx)
end

local function draw_transcript(height)
  if ImGui.BeginChild(S.ctx, "transcript", 0, height, FLAGS.child_borders) then
    -- No HorizontalScrollbar flag: it makes the content region unbounded and
    -- TextWrapped silently stops wrapping.
    if #S.blocks == 0 then
      text_colored(COLOR.dim,
        "Ask about the session. The selected track, the edit cursor, the time " ..
        "selection and the tempo are attached to every message, so \"what is " ..
        "wrong with this take\" already knows which take.")
    end
    for i = 1, #S.blocks do
      draw_block(i, S.blocks[i])
      ImGui.Spacing(S.ctx)
    end
    -- Pin to the bottom only when he is already there, so scrolling up to read
    -- an earlier answer is not yanked away by the next streaming delta. The
    -- tolerance is two lines rather than a few pixels: streaming grows the
    -- content every frame, and a tight threshold unsticks itself instantly.
    local tolerance = ImGui.GetTextLineHeightWithSpacing(S.ctx) * 2
    local at_bottom = ImGui.GetScrollY(S.ctx) >= ImGui.GetScrollMaxY(S.ctx) - tolerance
    if S.force_bottom or at_bottom then
      ImGui.SetScrollHereY(S.ctx, 1.0)
      S.force_bottom = false
    end
    ImGui.EndChild(S.ctx)
  end
end

-- The audition strip. Drawn only when there is a change to audition, so the
-- panel stays a chat box until the agent has actually done something.
local function audition_change()
  local change = S.state and S.state.last_change
  if type(change) ~= "table" then return nil end
  if change.id and change.id == S.change_dismissed then return nil end
  -- A new change means the A/B label is about a different FX. Carrying the old
  -- one over would say "hearing before" about a bypass he never toggled.
  if change.id ~= S.ab_change_id then
    S.ab_change_id = change.id
    S.ab_enabled = nil
  end
  return change
end

local function draw_audition()
  local change = audition_change()
  if not change then return false end

  ImGui.Separator(S.ctx)
  text_colored(COLOR.tool, "Last change: " .. (M.change_line(change) or "?"))

  if ImGui.Button(S.ctx, "\u{25C0} Locate") then locate(change) end
  ImGui.SameLine(S.ctx)

  local can_ab, why = M.ab_reason(change)
  if can_ab then
    local label = S.ab_enabled == false and "A/B (hearing before)" or "A/B"
    if ImGui.Button(S.ctx, label) then toggle_ab(change) end
    ImGui.SameLine(S.ctx)
    if ImGui.Button(S.ctx, "\u{25B6} Loop") then loop_play() end
  else
    -- Shown, not hidden. A button that silently does nothing is the Send bug
    -- again; a sentence saying why costs one line and lies about nothing.
    text_colored(COLOR.dim, "no A/B: " .. why)
    ImGui.SameLine(S.ctx)
    if ImGui.Button(S.ctx, "\u{25B6} Loop") then loop_play() end
  end
  ImGui.SameLine(S.ctx)
  local undo_label = type(change.restore) == "table" and "\u{21BA} Restore" or "\u{21BA} Undo"
  if ImGui.Button(S.ctx, undo_label) then restore_or_undo(change) end

  -- The verdicts. Each sends a complete instruction naming the target, so a
  -- compacted session cannot turn "too much" into "too much of what".
  for i, verdict in ipairs(M.VERDICTS) do
    if i > 1 then ImGui.SameLine(S.ctx) end
    if ImGui.SmallButton(S.ctx, verdict.label) then
      send(M.verdict_prompt(change, verdict.key))
    end
  end
  ImGui.SameLine(S.ctx)
  -- Keep it costs nothing and must not spend a turn: it only clears the strip.
  if ImGui.SmallButton(S.ctx, "keep it") then
    S.change_dismissed = change.id
    S.ab_enabled = nil
  end
  return true
end

local function draw_input()
  text_colored(COLOR.dim, S.focus_line)

  -- There is no multiline InputTextWithHint in Dear ImGui, so the placeholder
  -- is drawn as dim text above the box rather than inside it.
  if S.input == "" then
    text_colored(COLOR.dim, "Ctrl+Enter sends, Esc releases keys to REAPER")
  end

  -- NO EnterReturnsTrue here, deliberately. With that flag the call returns
  -- true only on Enter, and whether the binding still hands back the edited
  -- buffer on the false frames is not documented either way. It does not, so
  -- every keystroke was thrown away and Send saw an empty string. Without the
  -- flag the return is the ordinary "value changed", the buffer always comes
  -- back, and submission is detected from the keyboard directly.
  local height = ImGui.GetTextLineHeight(S.ctx) * 3
  local _, value = ImGui.InputTextMultiline(S.ctx, "##input", S.input, -1, height)
  if value ~= nil then S.input = value end

  -- Ctrl+Enter while the box has focus. Checked against the widget's own
  -- focus rather than globally, so the chord does not fire while he is
  -- somewhere else in the panel.
  local editing = ImGui.IsItemActive(S.ctx) or ImGui.IsItemFocused(S.ctx)
  if editing then
    local mods = ImGui.GetKeyMods(S.ctx)
    local ctrl = (mods & ImGui.Mod_Ctrl) ~= 0
    if ctrl and (ImGui.IsKeyPressed(S.ctx, ImGui.Key_Enter)
                 or ImGui.IsKeyPressed(S.ctx, ImGui.Key_KeypadEnter)) then
      if send(S.input) then S.input = "" end
    end
  end

  if ImGui.Button(S.ctx, "Send") then
    log(string.format("send clicked: %d chars", #S.input))
    if send(S.input) then S.input = "" end
  end

  local state = S.state
  local actions = state and state.quick_actions or {}
  for i = 1, math.min(#actions, 4) do
    local action = actions[i]
    if action and action.label then
      ImGui.SameLine(S.ctx)
      if ImGui.Button(S.ctx, action.label) then
        send(action.prompt or action.label)
      end
    end
  end

  if S.status_note and reaper.time_precise() < S.note_until then
    text_colored(COLOR.bad, S.status_note)
  end
end

-- ---------------------------------------------------------------------------
-- Docking
-- ---------------------------------------------------------------------------
--
-- ReaImGui persists floating position and its own docking in
-- %APPDATA%/REAPER/ReaImGui/<hash>.ini, but NOT the REAPER docker id. Round
-- trip it ourselves, the gfx2imgui.lua:3048 way. SetNextWindowDockID must be
-- called ONCE before the first Begin: unconditionally every frame makes the
-- window impossible to undock by hand.

local function apply_dock()
  if S.dock_applied then return end
  S.dock_applied = true
  local saved = tonumber(reaper.GetExtState(EXT_SECTION, "dock_id"))
  if saved and saved ~= 0 then
    ImGui.SetNextWindowDockID(S.ctx, ~saved, FLAGS.cond_first)
  end
end

local function save_dock()
  if not S.ctx or not ImGui.ValidatePtr(S.ctx, "ImGui_Context*") then return end
  local ok, docked = pcall(ImGui.IsWindowDocked, S.ctx)
  if ok and docked then
    local got, id = pcall(ImGui.GetWindowDockID, S.ctx)
    if got and id then
      reaper.SetExtState(EXT_SECTION, "dock_id", tostring(~id), true)
    end
  end
end

-- ---------------------------------------------------------------------------
-- Main loop
-- ---------------------------------------------------------------------------

local function ensure_context()
  -- ReaImGui reclaims contexts that go several defer cycles without use, and a
  -- 180 second blocking capture does exactly that. Recreate rather than die.
  if S.ctx and ImGui.ValidatePtr(S.ctx, "ImGui_Context*") then return end
  if S.ctx then log("context was reclaimed; recreating") end
  S.ctx = ImGui.CreateContext(SCRIPT_NAME)
  ImGui.SetConfigVar(S.ctx, FLAGS.cfg_nav_escape, 1)
  S.dock_applied = false
end

local function frame()
  S.frame = S.frame + 1
  ensure_context()
  poll()
  refresh_focus()
  beat()

  apply_dock()
  ImGui.SetNextWindowSize(S.ctx, 460, 620, FLAGS.cond_first)
  ImGui.PushFont(S.ctx, nil, 13)
  local visible, open = ImGui.Begin(S.ctx, SCRIPT_NAME, true, FLAGS.window_none)
  if visible then
    draw_status()
    ImGui.Separator(S.ctx)
    -- Reserve the input rows; the transcript takes whatever is left. The
    -- audition strip is three more rows, and only when there is a change.
    local rows = audition_change() and 7 or 4
    local reserved = ImGui.GetFrameHeightWithSpacing(S.ctx) * rows
    draw_transcript(-reserved)
    draw_audition()
    draw_input()
    ImGui.End(S.ctx)
  end
  ImGui.PopFont(S.ctx)

  if open then
    reaper.defer(frame)
  else
    save_dock()
  end
end

local function shutdown()
  save_dock()
  log("panel closed")
end

-- Re-running the action focuses the existing panel instead of opening a second
-- one. The nil guard matches talagan_MaCCLane.lua:89: older REAPER builds have
-- no set_action_options at all.
if reaper.set_action_options ~= nil then reaper.set_action_options(1) end

math.randomseed(os.time() + math.floor((reaper.time_precise() * 1000) % 100000))
reaper.RecursiveCreateDirectory(P.prompts, 0)
reaper.RecursiveCreateDirectory(P.control, 0)
reaper.RecursiveCreateDirectory(P.events, 0)
reaper.RecursiveCreateDirectory(join(BRIDGE_ROOT, "logs"), 0)

S.start_time = reaper.time_precise()
S.last_beat = 0
log("panel opened; root=" .. BRIDGE_ROOT)

-- Start the sidecar if nothing is publishing state. Done once, at open: a
-- retry loop here would spawn a second paid session every few seconds if the
-- first one is failing for a reason the panel cannot see.
do
  local state = M.read_json_file(P.state)
  if not state then
    spawn_sidecar()
  end
  -- A state file that exists is NOT proof of life; freshness() waits for the
  -- stamp to move before the panel believes any of it. Autostart is deliberately
  -- not attempted in that case: the sidecar's own lock refuses a second
  -- instance, and a panel that retried would spawn paid sessions in a loop.
end

reaper.atexit(shutdown)
reaper.defer(frame)
