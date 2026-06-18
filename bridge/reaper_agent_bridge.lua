-- Reaper Daemon v3 (REAPER agent file bridge)
-- Load this once in REAPER's Action List. It runs as a deferred script and
-- watches <bridge_root>\inbox for JSON commands. The bridge root is the folder
-- one level up from this script, so it works wherever the repo is cloned.

local SCRIPT_PATH = ({ reaper.get_action_context() })[2] or ""
local SCRIPT_DIR = SCRIPT_PATH:match("^(.*)[/\\][^/\\]+$") or "."

-- When a launcher (e.g. __startup.lua) loads this file via dofile(),
-- get_action_context() reports the launcher's path, not ours — so SCRIPT_DIR
-- would be wrong. The launcher sets this global to the bridge's own dir first.
if type(REAPER_AGENT_BRIDGE_DIR) == "string" and REAPER_AGENT_BRIDGE_DIR ~= "" then
  SCRIPT_DIR = REAPER_AGENT_BRIDGE_DIR
end

-- Path separator for this OS: "/" on macOS/Linux, "\" on Windows. Lua exposes
-- it as the first char of package.config, so the bridge builds correct paths
-- wherever it runs. (Match patterns keep accepting both separators.)
local SEP = package.config:sub(1, 1)
local function join(...)
  return table.concat({ ... }, SEP)
end

package.path = join(SCRIPT_DIR, "?.lua") .. ";" .. package.path

local json = require("json")

local CONFIG_PATH = join(SCRIPT_DIR, "bridge_config.json")

local function read_file(path)
  local file = io.open(path, "rb")
  if not file then return nil end
  local text = file:read("*a")
  file:close()
  return text
end

local function write_file(path, text)
  local file, err = io.open(path, "wb")
  if not file then error(err or ("Cannot write " .. path)) end
  file:write(text)
  file:close()
end

local function exists(path)
  local file = io.open(path, "rb")
  if file then file:close(); return true end
  return false
end

local function parent_dir(path)
  return path:match("^(.*)[/\\][^/\\]+$") or path
end

-- The script lives in <bridge_root>\bridge, so the bridge root is one level up.
local DEFAULT_BRIDGE_ROOT = parent_dir(SCRIPT_DIR)

local function default_config()
  return {
    bridge_root = DEFAULT_BRIDGE_ROOT,
    poll_interval_seconds = 0.25,
    archive_successful_commands = true,
    allow_risk_level_3 = false,
    bridge_version = 3,
  }
end

-- Generate a config on first run so a ReaPack install works without setup.
local function load_config()
  local text = read_file(CONFIG_PATH)
  if text then
    local ok, parsed = pcall(json.decode, text)
    if ok and type(parsed) == "table" then
      if not parsed.bridge_root or parsed.bridge_root == "" then
        parsed.bridge_root = DEFAULT_BRIDGE_ROOT
      end
      return parsed
    end
  end
  local config = default_config()
  pcall(write_file, CONFIG_PATH, json.encode(config))
  return config
end

local config = load_config()
local root = config.bridge_root

local paths = {
  inbox = join(root, "inbox"),
  processing = join(root, "processing"),
  outbox = join(root, "outbox"),
  failed = join(root, "failed"),
  archive = join(root, "archive"),
  logs = join(root, "logs"),
  recipes = join(root, "recipes"),
  heartbeat = join(root, "bridge", "heartbeat.json"),
}

-- Create the working folders if they are missing (fresh install or moved root).
for _, dir in pairs({ paths.inbox, paths.processing, paths.outbox, paths.failed, paths.archive, paths.logs, paths.recipes }) do
  if reaper.RecursiveCreateDirectory then reaper.RecursiveCreateDirectory(dir, 0) end
end

-- Singleton guard: refuse to start a second defer loop pointed at the same
-- root. Two bridges on one inbox race on os.rename (non-deterministic which
-- one grabs a file) and write competing heartbeats. The lock holds the
-- startup time and is refreshed every heartbeat (~5s); a dead bridge's lock
-- goes stale, so on startup we reclaim one older than 60s (no bridge tick
-- is that long).
local lockfile = join(paths.logs, "bridge.lock")
do
  local existing = read_file(lockfile)
  if existing then
    local started = tonumber(existing:match("^%d+"))
    if started and (os.time() - started) < 60 then
      error("BRIDGE_ALREADY_RUNNING: lock held by a bridge started at " .. existing)
    end
  end
  write_file(lockfile, tostring(os.time()))
end

local in_flight_command = nil
local last_poll = 0
local poll_interval = tonumber(config.poll_interval_seconds or 0.25)
local heartbeat_interval = 5
local last_heartbeat = nil
local last_in_flight = nil

local function now()
  return os.date("!%Y-%m-%dT%H:%M:%SZ")
end

local function log_line(message)
  local file = io.open(join(paths.logs, "bridge.log"), "ab")
  if file then
    file:write("[" .. now() .. "] " .. message .. "\n")
    file:close()
  end
end

-- POSIX rename(2) atomically replaces the destination whether it exists or
-- not, so we never pre-remove it: a pre-remove opens a window where readers
-- see no file, and if the rename then fails the old file is gone for good.
local function atomic_write_json(path, value)
  local tmp = path .. ".tmp"
  write_file(tmp, json.encode(value))
  local ok, err = os.rename(tmp, path)
  if not ok then error(err or ("Cannot rename " .. tmp)) end
end

local function move_file(src, dst)
  local ok = os.rename(src, dst)
  if ok then return true end
  -- Cross-volume or perm failure: fall back to copy + remove. Returning the
  -- remove result prevents a stranded src from being re-queued and re-run.
  local text = read_file(src)
  if not text then return false end
  write_file(dst, text)
  return os.remove(src)
end

local function list_json_files(dir)
  local files = {}
  local index = 0
  while true do
    local filename = reaper.EnumerateFiles(dir, index)
    if not filename then break end
    if filename:match("%.json$") and not filename:match("%.tmp$") then
      files[#files + 1] = filename
    end
    index = index + 1
  end
  table.sort(files)
  return files
end

local function selected_item_count()
  return reaper.CountSelectedMediaItems(0)
end

local function db_from_volume(volume)
  if not volume or volume <= 0 then return -150.0 end
  return 20.0 * math.log(volume, 10)
end

-- TimeMap2_timeToBeats / TimeMap2_beatsToTime are present in every REAPER
-- build we target and handle tempo changes + non-4/4 meters correctly. The
-- old fallbacks here used 240/tempo, which is seconds-per-bar ONLY in 4/4 —
-- wrong for 3/4, 6/8, 7/8, and silently misplaces markers/MIDI. Dropped.
local function bar_from_time(seconds)
  local _, measure = reaper.TimeMap2_timeToBeats(0, seconds)
  return measure + 1
end

local function time_from_bar(bar)
  return reaper.TimeMap2_beatsToTime(0, 0, math.max(0, bar - 1))
end

local function get_project_name()
  local _, name = reaper.EnumProjects(-1, "")
  if name and name ~= "" then
    return name:match("[^/\\]+$") or name
  end
  local ok, project_name = reaper.GetProjectName(0, "")
  if ok and project_name and project_name ~= "" then return project_name end
  return "Untitled"
end

local function get_time_selection()
  local start_time, end_time = reaper.GetSet_LoopTimeRange(false, false, 0, 0, false)
  return {
    start = start_time,
    ["end"] = end_time,
    start_bar = bar_from_time(start_time),
    end_bar = bar_from_time(end_time),
    active = end_time > start_time,
  }
end

local function get_transport()
  local state = reaper.GetPlayState()
  return {
    playing = (state & 1) == 1,
    paused = (state & 2) == 2,
    recording = (state & 4) == 4,
  }
end

local function get_tracks(include_fx)
  local tracks = {}
  for i = 0, reaper.CountTracks(0) - 1 do
    local track = reaper.GetTrack(0, i)
    local _, name = reaper.GetTrackName(track, "")
    local volume = reaper.GetMediaTrackInfo_Value(track, "D_VOL")
    local track_info = {
      index = i + 1,
      guid = reaper.GetTrackGUID(track),
      name = name,
      selected = reaper.IsTrackSelected(track),
      muted = reaper.GetMediaTrackInfo_Value(track, "B_MUTE") == 1,
      soloed = reaper.GetMediaTrackInfo_Value(track, "I_SOLO") ~= 0,
      armed = reaper.GetMediaTrackInfo_Value(track, "I_RECARM") == 1,
      volume = volume,
      volume_db = db_from_volume(volume),
      pan = reaper.GetMediaTrackInfo_Value(track, "D_PAN"),
      folder_depth = reaper.GetMediaTrackInfo_Value(track, "I_FOLDERDEPTH"),
      item_count = reaper.CountTrackMediaItems(track),
    }
    if include_fx then
      track_info.fx = {}
      for fx = 0, reaper.TrackFX_GetCount(track) - 1 do
        local _, fx_name = reaper.TrackFX_GetFXName(track, fx, "")
        track_info.fx[#track_info.fx + 1] = { index = fx, api_index = fx, scope = "track", name = fx_name }
      end
      track_info.input_fx = {}
      if reaper.TrackFX_GetRecCount then
        for fx = 0, reaper.TrackFX_GetRecCount(track) - 1 do
          local api_index = 0x1000000 + fx
          local _, fx_name = reaper.TrackFX_GetFXName(track, api_index, "")
          track_info.input_fx[#track_info.input_fx + 1] = { index = fx, api_index = api_index, scope = "input", name = fx_name }
        end
      end
    end
    tracks[#tracks + 1] = track_info
  end
  return tracks
end

local function get_markers_regions()
  local markers, regions = {}, {}
  local _, marker_count, region_count = reaper.CountProjectMarkers(0)
  for i = 0, marker_count + region_count - 1 do
    local ok, is_region, pos, region_end, name, index, color = reaper.EnumProjectMarkers3(0, i)
    if ok then
      local entry = {
        name = name,
        index = index,
        color = color,
        start = pos,
        start_bar = bar_from_time(pos),
      }
      if is_region then
        entry["end"] = region_end
        entry.end_bar = bar_from_time(region_end)
        regions[#regions + 1] = entry
      else
        entry.position = pos
        entry.bar = bar_from_time(pos)
        markers[#markers + 1] = entry
      end
    end
  end
  return markers, regions
end

local function command_get_context(command)
  local payload = command.payload or {}
  local cursor = reaper.GetCursorPosition()
  local markers, regions = get_markers_regions()
  return {
    project_name = get_project_name(),
    tempo = reaper.Master_GetTempo(),
    has_tempo_changes = reaper.CountTempoTimeSigMarkers(0) > 0,
    cursor = { seconds = cursor, bar = bar_from_time(cursor) },
    time_selection = get_time_selection(),
    transport = get_transport(),
    tracks = get_tracks(payload.include_fx ~= false),
    markers = markers,
    regions = regions,
    selected_track_count = reaper.CountSelectedTracks(0),
    selected_item_count = selected_item_count(),
  }
end

local function find_track(payload)
  if payload.target_track_guid then
    for i = 0, reaper.CountTracks(0) - 1 do
      local track = reaper.GetTrack(0, i)
      if reaper.GetTrackGUID(track) == payload.target_track_guid then return track, i + 1 end
    end
    error("NO_TARGET_TRACK: No track with guid " .. payload.target_track_guid)
  end
  if payload.target_track_name then
    local found, found_index = nil, nil
    local needle = payload.target_track_name:lower()
    for i = 0, reaper.CountTracks(0) - 1 do
      local track = reaper.GetTrack(0, i)
      local _, name = reaper.GetTrackName(track, "")
      if name:lower() == needle then
        if found then error("AMBIGUOUS_TARGET_TRACK: Multiple tracks named " .. payload.target_track_name) end
        found, found_index = track, i + 1
      end
    end
    if found then return found, found_index end
    error("NO_TARGET_TRACK: No track named " .. payload.target_track_name)
  end
  local selected = reaper.GetSelectedTrack(0, 0)
  if selected then
    return selected, math.floor(reaper.GetMediaTrackInfo_Value(selected, "IP_TRACKNUMBER"))
  end
  error("NO_TARGET_TRACK: Select a track or provide target_track_name")
end

local function contains_ci(haystack, needle)
  if not haystack or not needle then return false end
  return tostring(haystack):lower():find(tostring(needle):lower(), 1, true) ~= nil
end

local function find_fx(payload)
  local track, track_index = find_track(payload)
  local scope = payload.fx_scope or payload.scope or "all"
  local search_track_fx = scope == "all" or scope == "track" or scope == "normal"
  local search_input_fx = scope == "all" or scope == "input" or scope == "rec" or scope == "record"
  local matches = {}

  if payload.fx_index ~= nil then
    local fx_index = tonumber(payload.fx_index)
    local api_index = fx_index
    local resolved_scope = "track"
    if scope == "input" or scope == "rec" or scope == "record" then
      local rec_count = reaper.TrackFX_GetRecCount and reaper.TrackFX_GetRecCount(track) or 0
      if not fx_index or fx_index < 0 or fx_index >= rec_count then
        error("NO_FX: Input FX index out of range")
      end
      api_index = 0x1000000 + fx_index
      resolved_scope = "input"
    else
      local fx_count = reaper.TrackFX_GetCount(track)
      if not fx_index or fx_index < 0 or fx_index >= fx_count then
        error("NO_FX: Track FX index out of range")
      end
    end
    local _, fx_name = reaper.TrackFX_GetFXName(track, api_index, "")
    return track, track_index, api_index, fx_name, resolved_scope, fx_index
  end

  local needle = payload.fx_name_contains
  if not needle or needle == "" then
    error("NO_FX_SELECTOR: Provide fx_name_contains or fx_index")
  end

  local function add_matches(count, api_offset, match_scope)
    for fx = 0, count - 1 do
      local api_index = api_offset + fx
      local _, fx_name = reaper.TrackFX_GetFXName(track, api_index, "")
      if contains_ci(fx_name, needle) then
        matches[#matches + 1] = { index = fx, api_index = api_index, scope = match_scope, name = fx_name }
      end
    end
  end

  if search_track_fx then
    add_matches(reaper.TrackFX_GetCount(track), 0, "track")
  end
  if search_input_fx and reaper.TrackFX_GetRecCount then
    add_matches(reaper.TrackFX_GetRecCount(track), 0x1000000, "input")
  end

  if #matches == 0 then error("NO_FX: No FX matched " .. tostring(needle)) end
  if #matches > 1 then error("AMBIGUOUS_FX: Multiple FX matched " .. tostring(needle)) end
  return track, track_index, matches[1].api_index, matches[1].name, matches[1].scope, matches[1].index
end

local function get_fx_param_info(track, api_index, param_index)
  local _, name = reaper.TrackFX_GetParamName(track, api_index, param_index, "")
  local normalized = reaper.TrackFX_GetParamNormalized(track, api_index, param_index)
  local value, min_value, max_value = reaper.TrackFX_GetParam(track, api_index, param_index)
  local formatted = ""
  local ok, retval, text = pcall(reaper.TrackFX_GetFormattedParamValue, track, api_index, param_index, "")
  if ok then
    if type(text) == "string" then
      formatted = text
    elseif type(retval) == "string" then
      formatted = retval
    end
  end
  return {
    index = param_index,
    name = name,
    value = value,
    normalized_value = normalized,
    min = min_value,
    max = max_value,
    formatted_value = formatted,
  }
end

local function find_fx_param(track, api_index, payload)
  local param_count = reaper.TrackFX_GetNumParams(track, api_index)
  if payload.param_index ~= nil then
    local param_index = tonumber(payload.param_index)
    if not param_index or param_index < 0 or param_index >= param_count then
      error("NO_PARAM: Parameter index out of range")
    end
    return param_index, get_fx_param_info(track, api_index, param_index)
  end

  local needle = payload.param_name_contains
  if not needle or needle == "" then
    error("NO_PARAM_SELECTOR: Provide param_name_contains or param_index")
  end

  local matches = {}
  for param = 0, param_count - 1 do
    local info = get_fx_param_info(track, api_index, param)
    if contains_ci(info.name, needle) then
      matches[#matches + 1] = info
    end
  end

  if #matches == 0 then error("NO_PARAM: No parameter matched " .. tostring(needle)) end
  if #matches > 1 then error("AMBIGUOUS_PARAM: Multiple parameters matched " .. tostring(needle)) end
  return matches[1].index, matches[1]
end

local function time_from_point(point)
  if point.time ~= nil then return tonumber(point.time) or 0 end
  if point.seconds ~= nil then return tonumber(point.seconds) or 0 end
  if point.bar ~= nil then
    local bar = tonumber(point.bar) or 1
    local beat = tonumber(point.beat or 1) or 1
    local whole_bar = math.floor(bar)
    local beat_offset = 0
    if beat > 0 then beat_offset = beat - 1 end
    local ok, value = pcall(function()
      return reaper.TimeMap2_beatsToTime(0, beat_offset, math.max(0, whole_bar - 1))
    end)
    if ok and type(value) == "number" then return value end
    return time_from_bar(bar)
  end
  error("BAD_POINT_TIME: Automation point needs time, seconds, or bar")
end

local function envelope_shape(shape)
  local value = tostring(shape or "linear"):lower()
  if value == "linear" then return 0 end
  if value == "square" or value == "hold" then return 1 end
  if value == "slow_start_end" or value == "slow" then return 2 end
  if value == "fast_start" or value == "fast" then return 3 end
  if value == "bezier" then return 5 end
  return 0
end

local function resolve_position(position)
  position = position or { type = "cursor" }
  local kind = position.type or "cursor"
  if kind == "cursor" then return reaper.GetCursorPosition() end
  if kind == "time" then return tonumber(position.seconds or 0) or 0 end
  if kind == "bar" then return time_from_bar(tonumber(position.bar or 1) or 1) end
  if kind == "time_selection" then
    local start_time, end_time = reaper.GetSet_LoopTimeRange(false, false, 0, 0, false)
    if end_time <= start_time then error("NO_TIME_SELECTION: No active time selection") end
    return start_time, end_time
  end
  if kind == "marker" or kind == "region" then
    local _, marker_count, region_count = reaper.CountProjectMarkers(0)
    local needle = (position.name or ""):lower()
    for i = 0, marker_count + region_count - 1 do
      local ok, is_region, pos, region_end, name = reaper.EnumProjectMarkers3(0, i)
      if ok and name and name:lower() == needle then
        if kind == "region" and is_region then return pos, region_end end
        if kind == "marker" and not is_region then return pos end
      end
    end
    error("NO_" .. kind:upper() .. ": No " .. kind .. " named " .. tostring(position.name))
  end
  if kind == "selected_item" then
    local item = reaper.GetSelectedMediaItem(0, 0)
    if not item then error("NO_SELECTED_ITEM: No selected media item") end
    local start_time = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
    return start_time, start_time + len
  end
  error("BAD_POSITION: Unsupported position type " .. tostring(kind))
end

local function resolve_length(length, position_end, start_time)
  length = length or { type = "as_generated" }
  local kind = length.type or "as_generated"
  if kind == "bars" then
    local end_time = time_from_bar(bar_from_time(start_time) + (tonumber(length.bars or 1) or 1))
    return end_time - start_time
  end
  if kind == "region" or kind == "time_selection" then
    if not position_end or position_end <= start_time then
      error("BAD_LENGTH: " .. kind .. " length needs a range position")
    end
    return position_end - start_time
  end
  if kind == "seconds" then return tonumber(length.seconds or 0) or 0 end
  if kind == "as_generated" then return nil end
  error("BAD_LENGTH: Unsupported length type " .. tostring(kind))
end

local function range_has_items(track, start_time, end_time)
  local overlaps = {}
  if not end_time or end_time <= start_time then return overlaps end
  for i = 0, reaper.CountTrackMediaItems(track) - 1 do
    local item = reaper.GetTrackMediaItem(track, i)
    local pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
    if pos < end_time and (pos + len) > start_time then
      overlaps[#overlaps + 1] = { position = pos, length = len }
    end
  end
  return overlaps
end

local function delete_items_in_range(track, start_time, end_time)
  for i = reaper.CountTrackMediaItems(track) - 1, 0, -1 do
    local item = reaper.GetTrackMediaItem(track, i)
    local pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
    if pos < end_time and (pos + len) > start_time then
      reaper.DeleteTrackMediaItem(track, item)
    end
  end
end

local function insert_midi_payload(payload)
  local midi_path = payload.midi_path
  if not midi_path or not exists(midi_path) then
    error("MIDI_NOT_FOUND: " .. tostring(midi_path))
  end

  local track, track_index = find_track(payload)
  local start_time, position_end = resolve_position(payload.position)
  local requested_length = resolve_length(payload.length, position_end, start_time)
  local end_time = requested_length and (start_time + requested_length) or nil

  if end_time and payload.replace_existing_in_range then
    delete_items_in_range(track, start_time, end_time)
  elseif end_time then
    local overlaps = range_has_items(track, start_time, end_time)
    if #overlaps > 0 then error("RANGE_OCCUPIED: Existing item overlaps target range") end
  end

  reaper.SetOnlyTrackSelected(track)
  reaper.SetEditCurPos(start_time, false, false)

  -- Snapshot existing item pointers so we can identify the one InsertMedia
  -- actually added. The old fallback grabbed "the last item on the track"
  -- when InsertMedia didn't select the new one — which on a track with
  -- existing items means grabbing and then repositioning/resizing a clip
  -- the user already had. Silent data corruption. Diff instead, fail loud.
  local seen = {}
  for i = 0, reaper.CountTrackMediaItems(track) - 1 do
    seen[reaper.GetTrackMediaItem(track, i)] = true
  end

  reaper.InsertMedia(midi_path, 0)

  local item
  for i = 0, reaper.CountTrackMediaItems(track) - 1 do
    local it = reaper.GetTrackMediaItem(track, i)
    if not seen[it] then item = it; break end
  end
  if not item then error("INSERT_FAILED: REAPER did not create a media item") end

  -- InsertMedia does not reliably honor the edit cursor (it can drop the item
  -- at 0), so pin the item to the resolved start_time explicitly. This makes
  -- placement deterministic for every position type, not cursor-dependent.
  reaper.SetMediaItemInfo_Value(item, "D_POSITION", start_time)

  if payload.loop ~= nil then
    reaper.SetMediaItemInfo_Value(item, "B_LOOPSRC", payload.loop == false and 0 or 1)
  end
  if requested_length and requested_length > 0 then
    reaper.SetMediaItemInfo_Value(item, "D_LENGTH", requested_length)
  end
  reaper.UpdateArrange()

  local _, track_name = reaper.GetTrackName(track, "")
  local item_pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
  local item_len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
  return {
    track = { index = track_index, name = track_name },
    item = {
      start_seconds = item_pos,
      end_seconds = item_pos + item_len,
      start_bar = bar_from_time(item_pos),
      length_seconds = item_len,
      looped = payload.loop ~= false,
    },
    midi_path = midi_path,
  }
end

local function command_insert_midi_file(command)
  return insert_midi_payload(command.payload or {})
end

local function command_audition_groove(command)
  local payload = command.payload or {}
  local data = insert_midi_payload(payload)
  local start_time = data.item.start_seconds
  local end_time = data.item.end_seconds
  if payload.solo_track then
    local track = reaper.GetTrack(0, data.track.index - 1)
    if track then reaper.SetMediaTrackInfo_Value(track, "I_SOLO", 1) end
  end
  reaper.GetSet_LoopTimeRange(true, true, start_time, end_time, false)
  reaper.SetEditCurPos(start_time, false, false)
  if payload.play ~= false then reaper.Main_OnCommand(1007, 0) end -- 1007 = Transport: Play/stop
  data.audition = { loop_start = start_time, loop_end = end_time, playing = payload.play ~= false }
  return data
end

local function command_play()
  reaper.Main_OnCommand(1007, 0) -- 1007 = Transport: Play/stop
  return { transport = get_transport() }
end

local function command_stop()
  reaper.Main_OnCommand(1016, 0) -- 1016 = Transport: Stop
  return { transport = get_transport() }
end

local function resolve_color(color)
  if type(color) == "table" then
    return reaper.ColorToNative(color.r or 0, color.g or 0, color.b or 0) | 0x1000000
  end
  if type(color) == "number" then return color end
  error("BAD_COLOR: Provide a native color number or {r, g, b}")
end

local function command_set_track_color(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  local color = resolve_color(payload.color)
  reaper.SetMediaTrackInfo_Value(track, "I_CUSTOMCOLOR", color)
  reaper.TrackList_AdjustWindows(false)
  return { color = color }
end

local function command_solo_track(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  reaper.SetMediaTrackInfo_Value(track, "I_SOLO", payload.solo == false and 0 or 1)
  return { solo = payload.solo ~= false }
end

local function command_get_fx_parameters(command)
  local payload = command.payload or {}
  local track, track_index, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
  local _, track_name = reaper.GetTrackName(track, "")
  local params = {}
  local param_count = reaper.TrackFX_GetNumParams(track, api_index)
  local filter = payload.param_name_contains
  local offset = math.max(0, tonumber(payload.offset or 0) or 0)
  local limit = tonumber(payload.limit or 200) or 200
  if limit < 1 then limit = 1 end
  if limit > 1000 then limit = 1000 end
  local include_empty = payload.include_empty == true
  local matched_count = 0
  for param = 0, param_count - 1 do
    local info = get_fx_param_info(track, api_index, param)
    local looks_empty = info.name:match("^#%d+$") and (info.formatted_value == nil or info.formatted_value == "")
    if (include_empty or not looks_empty) and (not filter or contains_ci(info.name, filter)) then
      matched_count = matched_count + 1
      if matched_count > offset and #params < limit then
        params[#params + 1] = info
      end
    end
  end
  return {
    track = { index = track_index, name = track_name, guid = reaper.GetTrackGUID(track) },
    fx = { index = display_fx_index or api_index, api_index = api_index, scope = fx_scope or "track", name = fx_name, parameter_count = param_count },
    parameters = params,
    paging = {
      offset = offset,
      limit = limit,
      returned = #params,
      matched_count = matched_count,
      has_more = matched_count > (offset + #params),
      include_empty = include_empty,
      filter = filter,
    },
  }
end

local function command_set_fx_param(command)
  local payload = command.payload or {}
  local track, track_index, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
  local param_index, before = find_fx_param(track, api_index, payload)
  local ok = false

  if payload.normalized_value ~= nil then
    local value = tonumber(payload.normalized_value)
    if not value then error("BAD_PARAM_VALUE: normalized_value must be a number") end
    if value < 0 then value = 0 end
    if value > 1 then value = 1 end
    ok = reaper.TrackFX_SetParamNormalized(track, api_index, param_index, value)
  elseif payload.relative ~= nil then
    local delta = tonumber(tostring(payload.relative):gsub("^%+", ""))
    if not delta then error("BAD_PARAM_VALUE: relative must be numeric, like +0.2 or -0.1") end
    local current = reaper.TrackFX_GetParamNormalized(track, api_index, param_index)
    local value = current + delta
    if value < 0 then value = 0 end
    if value > 1 then value = 1 end
    ok = reaper.TrackFX_SetParamNormalized(track, api_index, param_index, value)
  elseif payload.formatted_value ~= nil then
    local call_ok, retval = pcall(reaper.TrackFX_SetFormattedParamValue, track, api_index, param_index, tostring(payload.formatted_value))
    if not call_ok then error("FORMATTED_VALUE_UNSUPPORTED: " .. tostring(retval)) end
    ok = retval
  else
    error("BAD_PARAM_VALUE: Provide normalized_value, relative, or formatted_value")
  end

  if not ok then error("SET_PARAM_FAILED: REAPER rejected the parameter value") end
  local after = get_fx_param_info(track, api_index, param_index)
  local _, track_name = reaper.GetTrackName(track, "")
  return {
    track = { index = track_index, name = track_name, guid = reaper.GetTrackGUID(track) },
    fx = { index = display_fx_index or api_index, api_index = api_index, scope = fx_scope or "track", name = fx_name },
    parameter = { before = before, after = after },
  }
end

local function command_write_fx_param_automation(command)
  local payload = command.payload or {}
  local track, track_index, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
  local param_index, param_info = find_fx_param(track, api_index, payload)
  local points = payload.points or {}
  if #points == 0 then error("NO_POINTS: Provide at least one automation point") end

  local envelope = reaper.GetFXEnvelope(track, api_index, param_index, true)
  if not envelope then error("NO_ENVELOPE: Could not create FX envelope") end
  pcall(reaper.SetEnvelopeInfo_Value, envelope, "B_VISIBLE", 1)
  pcall(reaper.SetEnvelopeInfo_Value, envelope, "B_ARM", 1)
  pcall(reaper.SetEnvelopeInfo_Value, envelope, "I_TCPH", 80)

  local start_time = nil
  local end_time = nil
  if payload.range then
    start_time, end_time = resolve_position(payload.range)
  elseif payload.position then
    start_time, end_time = resolve_position(payload.position)
  end

  if payload.clear_existing_in_range then
    if not start_time or not end_time or end_time <= start_time then
      local min_time, max_time = nil, nil
      for _, point in ipairs(points) do
        local t = time_from_point(point)
        if not min_time or t < min_time then min_time = t end
        if not max_time or t > max_time then max_time = t end
      end
      start_time = min_time
      end_time = max_time
    end
    if start_time and end_time and end_time >= start_time then
      reaper.DeleteEnvelopePointRange(envelope, start_time, end_time)
    end
  end

  local inserted = {}
  for _, point in ipairs(points) do
    local t = time_from_point(point)
    local value = tonumber(point.value)
    if not value then error("BAD_POINT_VALUE: Automation point value must be numeric") end
    if value < 0 then value = 0 end
    if value > 1 then value = 1 end
    local shape = envelope_shape(point.shape)
    local tension = tonumber(point.tension or 0) or 0
    reaper.InsertEnvelopePoint(envelope, t, value, shape, tension, point.selected == true, true)
    inserted[#inserted + 1] = {
      time = t,
      bar = bar_from_time(t),
      value = value,
      shape = shape,
      source = point,
    }
  end
  reaper.Envelope_SortPoints(envelope)
  pcall(reaper.SetCursorContext, 2, envelope)
  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()

  local _, track_name = reaper.GetTrackName(track, "")
  return {
    track = { index = track_index, name = track_name, guid = reaper.GetTrackGUID(track) },
    fx = { index = display_fx_index or api_index, api_index = api_index, scope = fx_scope or "track", name = fx_name },
    parameter = param_info,
    inserted_count = #inserted,
    cleared_range = payload.clear_existing_in_range and { start_time = start_time, end_time = end_time } or nil,
    points = inserted,
  }
end

-- ---------------------------------------------------------------------------
-- Universal DAW verbs
-- ---------------------------------------------------------------------------

local function volume_from_db(db)
  return 10.0 ^ (db / 20.0)
end

local function track_summary(track)
  local _, name = reaper.GetTrackName(track, "")
  local index = math.floor(reaper.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER"))
  return { index = index, name = name, guid = reaper.GetTrackGUID(track) }
end

local function command_pause()
  reaper.Main_OnCommand(1008, 0) -- 1008 = Transport: Pause
  return { transport = get_transport() }
end

local function command_record()
  reaper.Main_OnCommand(1013, 0) -- 1013 = Transport: Record
  return { transport = get_transport() }
end

local function command_set_cursor(command)
  local payload = command.payload or {}
  local time = resolve_position(payload.position or payload)
  reaper.SetEditCurPos(time, payload.move_view ~= false, payload.seek_play == true)
  return { cursor = { seconds = time, bar = bar_from_time(time) } }
end

local function command_set_time_selection(command)
  local payload = command.payload or {}
  if payload.clear == true then
    reaper.GetSet_LoopTimeRange(true, payload.loop == true, 0, 0, false)
    return { time_selection = { active = false } }
  end
  local start_time = resolve_position(payload.start or { type = "cursor" })
  local end_time
  if payload["end"] then
    end_time = resolve_position(payload["end"])
  elseif payload.length_bars then
    end_time = time_from_bar(bar_from_time(start_time) + (tonumber(payload.length_bars) or 1))
  else
    error("BAD_RANGE: Provide end or length_bars")
  end
  reaper.GetSet_LoopTimeRange(true, payload.loop == true, start_time, end_time, false)
  return { time_selection = get_time_selection() }
end

local function command_set_tempo(command)
  local payload = command.payload or {}
  local bpm = tonumber(payload.bpm)
  if not bpm or bpm <= 0 then error("BAD_TEMPO: Provide a positive bpm") end
  reaper.SetCurrentBPM(0, bpm, true)
  return { tempo = reaper.Master_GetTempo() }
end

local function command_add_track(command)
  local payload = command.payload or {}
  local count = reaper.CountTracks(0)
  local index = tonumber(payload.index)
  if not index or index < 1 then index = count + 1 end
  if index > count + 1 then index = count + 1 end
  reaper.InsertTrackAtIndex(index - 1, true)
  reaper.TrackList_AdjustWindows(false)
  local track = reaper.GetTrack(0, index - 1)
  if not track then error("ADD_TRACK_FAILED: REAPER did not create the track") end
  if payload.name and payload.name ~= "" then
    reaper.GetSetMediaTrackInfo_String(track, "P_NAME", tostring(payload.name), true)
  end
  if payload.color ~= nil then
    reaper.SetMediaTrackInfo_Value(track, "I_CUSTOMCOLOR", resolve_color(payload.color))
  end
  if payload.select == true then reaper.SetOnlyTrackSelected(track) end
  reaper.UpdateArrange()
  return { track = track_summary(track) }
end

local function command_delete_track(command)
  local track = find_track(command.payload or {})
  local summary = track_summary(track)
  reaper.DeleteTrack(track)
  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()
  return { deleted = summary }
end

local function command_rename_track(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  local new_name = payload.new_name or payload.name
  if not new_name or new_name == "" then error("BAD_NAME: Provide new_name") end
  reaper.GetSetMediaTrackInfo_String(track, "P_NAME", tostring(new_name), true)
  reaper.TrackList_AdjustWindows(false)
  return { track = track_summary(track) }
end

local function command_select_track(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  if payload.select == false then
    reaper.SetTrackSelected(track, false)
  elseif payload.exclusive == false then
    reaper.SetTrackSelected(track, true)
  else
    reaper.SetOnlyTrackSelected(track)
  end
  reaper.UpdateArrange()
  return { track = track_summary(track), selected = payload.select ~= false }
end

local function command_set_track_volume(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  local volume
  if payload.volume_db ~= nil then
    volume = volume_from_db(tonumber(payload.volume_db) or 0)
  elseif payload.volume ~= nil then
    volume = tonumber(payload.volume)
  else
    error("BAD_VALUE: Provide volume_db or volume")
  end
  if not volume or volume < 0 then error("BAD_VALUE: Volume must be a non-negative number") end
  reaper.SetMediaTrackInfo_Value(track, "D_VOL", volume)
  return { track = track_summary(track), volume = volume, volume_db = db_from_volume(volume) }
end

local function command_set_track_pan(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  local pan = tonumber(payload.pan)
  if not pan then error("BAD_VALUE: Provide pan from -1.0 (left) to 1.0 (right)") end
  if pan < -1 then pan = -1 end
  if pan > 1 then pan = 1 end
  reaper.SetMediaTrackInfo_Value(track, "D_PAN", pan)
  return { track = track_summary(track), pan = pan }
end

local function command_mute_track(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  local mute = payload.mute ~= false
  reaper.SetMediaTrackInfo_Value(track, "B_MUTE", mute and 1 or 0)
  return { track = track_summary(track), muted = mute }
end

local function command_arm_track(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  local armed = payload.armed ~= false
  reaper.SetMediaTrackInfo_Value(track, "I_RECARM", armed and 1 or 0)
  return { track = track_summary(track), armed = armed }
end

local function command_add_fx(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  local fx_name = payload.fx_name
  if not fx_name or fx_name == "" then error("BAD_FX_NAME: Provide fx_name") end
  local scope = payload.fx_scope or payload.scope or "track"
  local is_input = scope == "input" or scope == "rec" or scope == "record"
  local fx_index = reaper.TrackFX_AddByName(track, fx_name, is_input, -1)
  if not fx_index or fx_index < 0 then
    error("ADD_FX_FAILED: REAPER could not add FX " .. tostring(fx_name) ..
      " (name must match the plugin as REAPER lists it)")
  end
  local api_index = is_input and (0x1000000 + fx_index) or fx_index
  local _, resolved_name = reaper.TrackFX_GetFXName(track, api_index, "")
  if payload.show == true then reaper.TrackFX_Show(track, api_index, 1) end
  return {
    track = track_summary(track),
    fx = { index = fx_index, api_index = api_index, scope = is_input and "input" or "track", name = resolved_name },
  }
end

local function command_remove_fx(command)
  local payload = command.payload or {}
  local track, track_index, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
  local ok = reaper.TrackFX_Delete(track, api_index)
  if not ok then error("REMOVE_FX_FAILED: REAPER rejected the FX delete") end
  return {
    track = track_summary(track),
    removed = { index = display_fx_index or api_index, scope = fx_scope or "track", name = fx_name },
  }
end

local function command_bypass_fx(command)
  local payload = command.payload or {}
  local track, track_index, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
  local bypass = payload.bypass ~= false
  reaper.TrackFX_SetEnabled(track, api_index, not bypass)
  return {
    track = track_summary(track),
    fx = { index = display_fx_index or api_index, scope = fx_scope or "track", name = fx_name, bypassed = bypass },
  }
end

local function command_move_fx(command)
  local payload = command.payload or {}
  local track, track_index, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
  if fx_scope == "input" then error("UNSUPPORTED: move_fx only supports track FX") end
  local to_index = tonumber(payload.to_index)
  if to_index == nil then error("BAD_VALUE: Provide to_index (0-based)") end
  local fx_count = reaper.TrackFX_GetCount(track)
  if to_index < 0 then to_index = 0 end
  if to_index >= fx_count then to_index = fx_count - 1 end
  reaper.TrackFX_CopyToTrack(track, api_index, track, to_index, true)
  return {
    track = track_summary(track),
    fx = { name = fx_name, from_index = display_fx_index or api_index, to_index = to_index },
  }
end

local function command_add_marker(command)
  local payload = command.payload or {}
  local time = resolve_position(payload.position or payload)
  local color = payload.color ~= nil and resolve_color(payload.color) or 0
  local index = reaper.AddProjectMarker2(0, false, time, 0, payload.name or "", payload.want_index or -1, color)
  return { marker = { index = index, name = payload.name or "", seconds = time, bar = bar_from_time(time) } }
end

local function command_add_region(command)
  local payload = command.payload or {}
  local start_time = resolve_position(payload.start or payload.position or { type = "cursor" })
  local end_time
  if payload["end"] then
    end_time = resolve_position(payload["end"])
  elseif payload.length_bars then
    end_time = time_from_bar(bar_from_time(start_time) + (tonumber(payload.length_bars) or 1))
  else
    error("BAD_RANGE: Provide end or length_bars")
  end
  local color = payload.color ~= nil and resolve_color(payload.color) or 0
  local index = reaper.AddProjectMarker2(0, true, start_time, end_time, payload.name or "", payload.want_index or -1, color)
  return {
    region = {
      index = index, name = payload.name or "",
      start = start_time, ["end"] = end_time,
      start_bar = bar_from_time(start_time), end_bar = bar_from_time(end_time),
    },
  }
end

local function command_delete_marker(command)
  local payload = command.payload or {}
  local is_region = payload.is_region == true or payload.type == "region"
  if payload.marker_index ~= nil then
    local ok = reaper.DeleteProjectMarker(0, tonumber(payload.marker_index), is_region)
    if not ok then error("NO_MARKER: No marker/region with index " .. tostring(payload.marker_index)) end
    return { deleted = { index = payload.marker_index, is_region = is_region } }
  end
  local needle = (payload.name or ""):lower()
  if needle == "" then error("BAD_SELECTOR: Provide marker_index or name") end
  local _, marker_count, region_count = reaper.CountProjectMarkers(0)
  for i = 0, marker_count + region_count - 1 do
    local ok, rgn, pos, rend, name, idx = reaper.EnumProjectMarkers3(0, i)
    if ok and name and name:lower() == needle and rgn == is_region then
      reaper.DeleteProjectMarker(0, idx, rgn)
      return { deleted = { index = idx, name = name, is_region = rgn } }
    end
  end
  error("NO_MARKER: No " .. (is_region and "region" or "marker") .. " named " .. tostring(payload.name))
end

local function command_delete_items_in_range(command)
  local payload = command.payload or {}
  local start_time, range_end = resolve_position(payload.range or payload.position or { type = "time_selection" })
  local end_time = range_end
  if not end_time then
    if payload.length_bars then
      end_time = time_from_bar(bar_from_time(start_time) + (tonumber(payload.length_bars) or 1))
    elseif payload.length_seconds then
      end_time = start_time + (tonumber(payload.length_seconds) or 0)
    else
      error("BAD_RANGE: range needs an explicit end, length_bars, or length_seconds")
    end
  end
  local targets = {}
  if payload.all_tracks == true then
    for i = 0, reaper.CountTracks(0) - 1 do targets[#targets + 1] = reaper.GetTrack(0, i) end
  else
    targets[1] = (find_track(payload))
  end
  local removed = 0
  for _, track in ipairs(targets) do
    local before = reaper.CountTrackMediaItems(track)
    delete_items_in_range(track, start_time, end_time)
    removed = removed + (before - reaper.CountTrackMediaItems(track))
  end
  reaper.UpdateArrange()
  return { removed_count = removed, range = { start = start_time, ["end"] = end_time } }
end

local function command_render(command)
  if not config.allow_risk_level_3 then
    error("RENDER_BLOCKED: render is gated; set allow_risk_level_3 true in bridge_config.json")
  end
  local payload = command.payload or {}
  if payload.output_file and payload.output_file ~= "" then
    reaper.GetSetProjectInfo_String(0, "RENDER_FILE", tostring(payload.output_file), true)
  end
  local bounds = { entire = 1, project = 1, time_selection = 2, regions = 3, selected_items = 4 }
  if payload.bounds and bounds[payload.bounds] then
    reaper.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", bounds[payload.bounds], true)
  end
  -- Render is synchronous — it blocks the defer loop for the whole duration,
  -- so no heartbeats tick during it. Write one first with `busy` set so an
  -- agent can distinguish "rendering" from "bridge died".
  atomic_write_json(paths.heartbeat, {
    alive_at = now(),
    bridge_version = 3,
    project_name = get_project_name(),
    in_flight_command = in_flight_command,
    reaper_focused = true,
    busy = "render",
  })
  -- Render with the project's most recent render settings (format, sample rate,
  -- etc. must be configured once in REAPER's Render dialog).
  reaper.Main_OnCommand(42230, 0) -- 42230 = File: Render project
  return {
    rendered = true,
    output_file = select(2, reaper.GetSetProjectInfo_String(0, "RENDER_FILE", "", false)),
    note = "Render used REAPER's last-saved render settings.",
  }
end

-- ---------------------------------------------------------------------------
-- Discovery
-- ---------------------------------------------------------------------------

local function scan_track_fx(track, include_values, max_params)
  local entries = {}
  local function scan(count, api_offset, scope)
    for fx = 0, count - 1 do
      local api_index = api_offset + fx
      local _, fx_name = reaper.TrackFX_GetFXName(track, api_index, "")
      local entry = {
        index = fx, api_index = api_index, scope = scope, name = fx_name,
        enabled = reaper.TrackFX_GetEnabled(track, api_index),
        parameter_count = reaper.TrackFX_GetNumParams(track, api_index),
      }
      entry.parameters = {}
      local limit = math.min(entry.parameter_count, max_params)
      for p = 0, limit - 1 do
        if include_values then
          entry.parameters[#entry.parameters + 1] = get_fx_param_info(track, api_index, p)
        else
          local _, pname = reaper.TrackFX_GetParamName(track, api_index, p, "")
          entry.parameters[#entry.parameters + 1] = { index = p, name = pname }
        end
      end
      entry.parameters_truncated = entry.parameter_count > limit
      entries[#entries + 1] = entry
    end
  end
  scan(reaper.TrackFX_GetCount(track), 0, "track")
  if reaper.TrackFX_GetRecCount then
    scan(reaper.TrackFX_GetRecCount(track), 0x1000000, "input")
  end
  return entries
end

local function command_scan_fx(command)
  local payload = command.payload or {}
  local include_values = payload.include_values == true
  local max_params = math.max(1, math.min(2000, tonumber(payload.max_params or 500) or 500))
  local tracks = {}
  local total_fx = 0
  if payload.target_track_name or payload.target_track_guid then
    local track = find_track(payload)
    local summary = track_summary(track)
    summary.fx = scan_track_fx(track, include_values, max_params)
    total_fx = total_fx + #summary.fx
    tracks[1] = summary
  else
    for i = 0, reaper.CountTracks(0) - 1 do
      local track = reaper.GetTrack(0, i)
      local summary = track_summary(track)
      summary.fx = scan_track_fx(track, include_values, max_params)
      total_fx = total_fx + #summary.fx
      tracks[#tracks + 1] = summary
    end
  end
  return {
    project_name = get_project_name(),
    track_count = #tracks,
    fx_count = total_fx,
    include_values = include_values,
    tracks = tracks,
  }
end

-- ---------------------------------------------------------------------------
-- Recipes — plugin-agnostic, reusable command sets an agent saves and replays
-- ---------------------------------------------------------------------------

local function safe_recipe_name(name)
  if type(name) ~= "string" or name == "" then error("BAD_RECIPE_NAME: Provide a recipe name") end
  if not name:match("^[%w%-%. ]+$") then
    error("BAD_RECIPE_NAME: Use only letters, numbers, space, dash, underscore, dot")
  end
  return name
end

local function recipe_path(name)
  return join(paths.recipes, name .. ".json")
end

local function command_save_recipe(command)
  local payload = command.payload or {}
  local name = safe_recipe_name(payload.name)
  local commands = payload.commands
  if type(commands) ~= "table" or #commands == 0 then
    error("BAD_RECIPE: Provide a non-empty commands array")
  end
  local recipe = {
    name = name,
    description = payload.description or "",
    created_by = command.created_by or "agent",
    saved_at = now(),
    commands = commands,
  }
  atomic_write_json(recipe_path(name), recipe)
  return { saved = name, path = recipe_path(name), command_count = #commands }
end

local function command_list_recipes()
  local recipes = {}
  local index = 0
  while true do
    local filename = reaper.EnumerateFiles(paths.recipes, index)
    if not filename then break end
    if filename:match("%.json$") and not filename:match("%.tmp$") then
      local text = read_file(join(paths.recipes, filename))
      local ok, parsed = pcall(json.decode, text or "")
      recipes[#recipes + 1] = {
        name = filename:gsub("%.json$", ""),
        description = (ok and type(parsed) == "table" and parsed.description) or "",
        command_count = (ok and type(parsed) == "table" and parsed.commands and #parsed.commands) or 0,
        saved_at = (ok and type(parsed) == "table" and parsed.saved_at) or nil,
      }
    end
    index = index + 1
  end
  table.sort(recipes, function(a, b) return a.name < b.name end)
  return { recipes = recipes, count = #recipes }
end

local function load_recipe(name)
  local text = read_file(recipe_path(name))
  if not text then error("NO_RECIPE: No recipe named " .. tostring(name)) end
  local ok, parsed = pcall(json.decode, text)
  if not ok or type(parsed) ~= "table" then error("BAD_RECIPE: Recipe file is not valid JSON") end
  return parsed
end

local function command_get_recipe(command)
  local name = safe_recipe_name((command.payload or {}).name)
  return { recipe = load_recipe(name) }
end

local handlers = {}

-- Commands that don't need an undo block. `save_recipe` writes a file, not
-- project state — REAPER undo doesn't cover it. Everything else mutates the
-- project and gets wrapped. Named for what it IS, not what it isn't.
local NO_UNDO_BLOCK = {
  get_context = true, get_fx_parameters = true, scan_fx = true,
  list_recipes = true, get_recipe = true, save_recipe = true,
}

local function is_mutating(command_type)
  return not NO_UNDO_BLOCK[command_type]
end

local function run_command(command, in_batch)
  if type(command) ~= "table" then error("BAD_COMMAND: Command is not an object") end
  if not command.type then error("BAD_COMMAND: Missing type") end
  if command.version ~= nil and command.version ~= 3 then
    error("UNSUPPORTED_VERSION: bridge speaks v3, got " .. tostring(command.version))
  end
  local handler = handlers[command.type]
  if not handler then error("UNKNOWN_COMMAND: " .. tostring(command.type)) end

  if command.dry_run and is_mutating(command.type) then
    return { dry_run = true, would_run = command.type, payload = command.payload or {} }
  end

  local undo_started = false
  if is_mutating(command.type) and not in_batch then
    reaper.Undo_BeginBlock()
    undo_started = true
  end
  local data = handler(command)
  if undo_started then
    reaper.Undo_EndBlock(command.undo_label or ("Agent: " .. command.type), -1)
  end
  return data
end

-- batch and apply_recipe replay sub-commands through run_command, so they're
-- defined here (after run_command) and registered with the handlers below.
local function command_batch(command)
  local payload = command.payload or {}
  local commands = payload.commands or {}
  local results = {}
  reaper.Undo_BeginBlock()
  for i, sub in ipairs(commands) do
    local ok, data = pcall(run_command, sub, true)
    results[#results + 1] = { index = i, type = sub.type, ok = ok, data = ok and data or nil, error = ok and nil or tostring(data) }
    if not ok and payload.stop_on_error ~= false then
      reaper.Undo_EndBlock(command.undo_label or payload.undo_label or "Agent: batch failed", -1)
      error("BATCH_FAILED: sub-command " .. i .. " failed: " .. tostring(data))
    end
  end
  reaper.Undo_EndBlock(command.undo_label or payload.undo_label or "Agent: batch", -1)
  return { results = results }
end

local function command_apply_recipe(command)
  local payload = command.payload or {}
  local name = safe_recipe_name(payload.name)
  local recipe = load_recipe(name)
  local commands = recipe.commands or {}
  if #commands == 0 then error("EMPTY_RECIPE: Recipe " .. name .. " has no commands") end
  local results = {}
  reaper.Undo_BeginBlock()
  for i, sub in ipairs(commands) do
    local ok, data = pcall(run_command, sub, true)
    results[#results + 1] = { index = i, type = sub.type, ok = ok, data = ok and data or nil, error = ok and nil or tostring(data) }
    if not ok and payload.stop_on_error ~= false then
      reaper.Undo_EndBlock("Agent: recipe " .. name .. " (failed)", -1)
      error("RECIPE_FAILED: command " .. i .. " (" .. tostring(sub.type) .. ") failed: " .. tostring(data))
    end
  end
  reaper.Undo_EndBlock(command.undo_label or ("Agent: recipe " .. name), -1)
  return { recipe = name, results = results }
end

-- Read / context
handlers.get_context = command_get_context
handlers.get_fx_parameters = command_get_fx_parameters
handlers.scan_fx = command_scan_fx

-- Transport / project
handlers.play = command_play
handlers.stop = command_stop
handlers.pause = command_pause
handlers.record = command_record
handlers.set_cursor = command_set_cursor
handlers.set_time_selection = command_set_time_selection
handlers.set_tempo = command_set_tempo
handlers.render = command_render

-- Track lifecycle
handlers.add_track = command_add_track
handlers.delete_track = command_delete_track
handlers.rename_track = command_rename_track
handlers.select_track = command_select_track

-- Track properties
handlers.set_track_color = command_set_track_color
handlers.set_track_volume = command_set_track_volume
handlers.set_track_pan = command_set_track_pan
handlers.mute_track = command_mute_track
handlers.solo_track = command_solo_track
handlers.arm_track = command_arm_track

-- FX
handlers.add_fx = command_add_fx
handlers.remove_fx = command_remove_fx
handlers.bypass_fx = command_bypass_fx
handlers.move_fx = command_move_fx
handlers.set_fx_param = command_set_fx_param
handlers.write_fx_param_automation = command_write_fx_param_automation

-- Markers / regions / items
handlers.add_marker = command_add_marker
handlers.add_region = command_add_region
handlers.delete_marker = command_delete_marker
handlers.delete_items_in_range = command_delete_items_in_range

-- MIDI
handlers.insert_midi_file = command_insert_midi_file
handlers.audition_groove = command_audition_groove

-- Recipes
handlers.save_recipe = command_save_recipe
handlers.list_recipes = command_list_recipes
handlers.get_recipe = command_get_recipe
handlers.apply_recipe = command_apply_recipe
handlers.batch = command_batch

local function write_result(command, ok, data_or_error)
  local result
  if ok then
    result = {
      id = command.id,
      ok = true,
      type = command.type,
      finished_at = now(),
      message = "Command completed: " .. tostring(command.type),
      data = data_or_error,
      warnings = {},
    }
  else
    result = {
      id = command.id,
      ok = false,
      type = command.type,
      finished_at = now(),
      message = tostring(data_or_error),
      -- Lua 5.3+ prefixes error() messages with "file:line: ", so anchor the
      -- UPPER_SNAKE code search anywhere in the string, not just at ^.
      error = { code = tostring(data_or_error):match("([A-Z_]+):[^:]") or "COMMAND_FAILED", details = tostring(data_or_error) },
    }
  end
  atomic_write_json(join(paths.outbox, command.id .. ".json"), result)
end

local function process_file(filename)
  local inbox_path = join(paths.inbox, filename)
  local processing_path = join(paths.processing, filename)
  if not move_file(inbox_path, processing_path) then return end

  local command = nil
  local text = read_file(processing_path)
  local ok, parsed = pcall(json.decode, text or "")
  if ok then command = parsed else
    command = { id = filename:gsub("%.json$", ""), type = "parse" }
    write_result(command, false, "BAD_JSON: " .. tostring(parsed))
    move_file(processing_path, join(paths.failed, filename))
    return
  end

  command.id = command.id or filename:gsub("%.json$", "")
  in_flight_command = command.id
  log_line("start " .. command.id .. " " .. tostring(command.type))
  local run_ok, data = pcall(run_command, command, false)
  write_result(command, run_ok, data)
  log_line((run_ok and "ok " or "fail ") .. command.id .. " " .. tostring(command.type))

  local destination = run_ok and paths.archive or paths.failed
  move_file(processing_path, join(destination, filename))
  in_flight_command = nil
end

local function write_heartbeat()
  local heartbeat = {
    alive_at = now(),
    bridge_version = 3,
    project_name = get_project_name(),
    in_flight_command = in_flight_command,
    reaper_focused = true,
  }
  atomic_write_json(paths.heartbeat, heartbeat)
  write_file(lockfile, tostring(os.time()))
end

-- Write the heartbeat at most every `heartbeat_interval` seconds, or
-- immediately when a command starts/finishes (in_flight_command changes)
-- or on the first tick. The old code wrote it 4×/second forever — ~345k
-- renames/day on a file nothing was reading that often.
local function maybe_heartbeat(force)
  local t = reaper.time_precise()
  if force or last_heartbeat == nil
     or in_flight_command ~= last_in_flight
     or t - last_heartbeat >= heartbeat_interval then
    write_heartbeat()
    last_heartbeat = t
    last_in_flight = in_flight_command
  end
end

-- One-shot retention sweep at startup: bound logs/ and the archive|failed|outbox
-- dirs so a long-lived install never grows without limit. Runs once before the
-- loop, so zero per-tick cost. Log: rotate bridge.log -> bridge.log.1 past
-- 1 MB (one backup, max ~2 MB). Dirs: keep the 200 newest .json files; command
-- ids are timestamp-prefixed (manual-/agent-YYYY-MM-DDTHH-MM-SS-hex), so
-- list_json_files' lexical sort == chronological, and files[1] is the oldest.
local function sweep_once()
  local log_path = join(paths.logs, "bridge.log")
  local f = io.open(log_path, "rb")
  if f then
    local size = f:seek("end")
    f:close()
    if size > 1024 * 1024 then
      -- POSIX rename atomically replaces any existing bridge.log.1.
      os.rename(log_path, join(paths.logs, "bridge.log.1"))
    end
  end
  local keep = 200
  for _, dir in ipairs({ paths.archive, paths.failed, paths.outbox }) do
    local files = list_json_files(dir)
    if #files > keep then
      for i = 1, #files - keep do
        os.remove(join(dir, files[i]))
      end
    end
  end
end

local function loop()
  local current = reaper.time_precise()
  if current - last_poll >= poll_interval then
    last_poll = current
    local ok, err = pcall(function()
      maybe_heartbeat(false)
      -- Drain all queued commands in one tick (capped to avoid blocking the
      -- UI on a pathological inbox). The old one-per-tick cap limited
      -- throughput to 4 cmd/s — a 100-command dump took 25s to drain.
      local files = list_json_files(paths.inbox)
      for i = 1, math.min(#files, 50) do
        process_file(files[i])
      end
    end)
    if not ok then
      log_line("loop error " .. tostring(err))
      in_flight_command = nil
    end
  end
  reaper.defer(loop)
end

-- Re-queue anything stranded in processing/ from a previous run (crash or
-- force-quit mid-command). An uncommitted undo block evaporates on crash,
-- so the command never took effect — safe to re-run. One-shot at startup.
for _, filename in ipairs(list_json_files(paths.processing)) do
  move_file(join(paths.processing, filename), join(paths.inbox, filename))
end

sweep_once()

log_line("bridge started")
loop()
