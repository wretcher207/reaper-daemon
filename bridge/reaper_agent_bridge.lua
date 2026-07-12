-- @description Reaper Daemon (REAPER agent file bridge)
-- @version 3.9.0
-- @author Dead Pixel Design
-- @link https://github.com/wretcher207/reaper-daemon
-- @provides
--   json.lua
-- @about
--   Reaper Daemon lets an AI coding agent control REAPER through a local file
--   bridge: the agent drops JSON command files in inbox/, this background
--   script runs them inside REAPER and writes JSON results to outbox/. No
--   network, no socket. ReaPack note: this does NOT auto-start; run the action
--   once per REAPER session, or add it to Scripts/__startup.lua. The bridge
--   root (where inbox/ and outbox/ are created on first run) is the folder one
--   level up from this script. Point your agent there.
-- @changelog
--   3.3.0: Robust add_fx_chain splicer (correct chunk depth + restore-on-error);
--   UI time-budget on the drain loop; singleton-lock race closed via owner
--   token; fx_index now requires an explicit fx_scope; dropped the fake
--   reaper_focused field; optional shared-secret auth token; doc + polish fixes;
--   added a cross-platform CI self-test (bridge/test_bridge.lua).
--   3.2.0: Windows atomic-write fix (heartbeat no longer freezes after first
--   write); never sweep outbox/ (stop deleting unread replies); set_fx_param
--   rolls back on a failed search and parses kHz displays correctly; bridge
--   re-derives its root after the repo moves. Supersedes the 3.1.x line.

-- Runs as a deferred script. The bridge root is the folder one level up from
-- this script, so it works wherever the repo is cloned or ReaPack installs it.

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
      -- bridge_config.json lives inside the clone and moves with it, but its
      -- saved bridge_root is absolute — after the repo moves it points at the OLD
      -- location and the bridge silently polls a dead inbox while reaperd.py
      -- writes to the new one. SCRIPT_DIR is always the live clone (the launcher
      -- sets it), so DEFAULT_BRIDGE_ROOT is authoritative: if the saved root
      -- disagrees, re-derive and rewrite. (Other config keys are preserved.)
      if parsed.bridge_root ~= DEFAULT_BRIDGE_ROOT then
        parsed.bridge_root = DEFAULT_BRIDGE_ROOT
        pcall(write_file, CONFIG_PATH, json.encode(parsed))
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

-- Optional shared secret. When config.auth_token is a non-empty string, every
-- command must carry a matching `token` field or it's rejected (AUTH_FAILED).
-- Off by default. See the SECURITY note in README: this gates accidental or
-- other-app writes to inbox/, NOT a local attacker who can read the
-- (world-readable) config — the real control is keeping the folder local.
local auth_token = (type(config.auth_token) == "string" and config.auth_token ~= "")
  and config.auth_token or nil

local paths = {
  inbox = join(root, "inbox"),
  processing = join(root, "processing"),
  outbox = join(root, "outbox"),
  failed = join(root, "failed"),
  archive = join(root, "archive"),
  logs = join(root, "logs"),
  heartbeat = join(root, "bridge", "heartbeat.json"),
}

-- Create the working folders if they are missing (fresh install or moved root).
for _, dir in pairs({ paths.inbox, paths.processing, paths.outbox, paths.failed, paths.archive, paths.logs }) do
  if reaper.RecursiveCreateDirectory then reaper.RecursiveCreateDirectory(dir, 0) end
end

-- Singleton guard: refuse to start a second defer loop pointed at the same
-- root. Two bridges on one inbox race on os.rename (non-deterministic which
-- one grabs a file) and write competing heartbeats. The lock holds the
-- startup time and a `busy` state, refreshed every heartbeat (~5s); a dead
-- bridge's lock goes stale, so on startup we reclaim one older than 60s (no
-- bridge tick is that long). EXCEPT a `busy=render` lock: render blocks the
-- defer loop synchronously for the whole render duration (no ticks, no
-- heartbeats), which can exceed 60s. We never reclaim a render-busy lock no
-- matter how old — a second bridge would race the first on the same inbox.
--
-- Lua has no atomic O_EXCL create, so the startup read→reclaim→write has a
-- TOCTOU window: two bridges starting in the same tick both see a stale/absent
-- lock and both write. To close it, each bridge stamps the lock with a unique
-- OWNER token, then CONFIRMS ownership one race-window later (see loop): both
-- read the same settled file, exactly one sees its own token and keeps looping;
-- the other stops. No O_EXCL needed.
local lockfile = join(paths.logs, "bridge.lock")

-- POSIX rename(2) atomically replaces the destination whether it exists or
-- not, so the first rename succeeds and we never pre-remove (a pre-remove opens
-- a window where readers see no file). Windows rename() CANNOT replace an
-- existing dest, so there the first rename fails on every overwrite — that froze
-- the heartbeat after its first write. Fall back to remove + retry: still atomic
-- on POSIX, and the only correctness-preserving option Windows offers.
-- (Defined here, above the lock code, so the lock file gets the same guarantee:
-- a plain write can be read half-written by a starting bridge, which is the
-- exact race the OWNER-confirm protocol depends on the file settling for.)
local function atomic_write_json(path, value)
  local tmp = path .. ".tmp"
  write_file(tmp, json.encode(value))
  local ok, err = os.rename(tmp, path)
  if not ok then
    os.remove(path)
    ok, err = os.rename(tmp, path)
  end
  if not ok then error(err or ("Cannot rename " .. tmp)) end
end

-- Unique per process: a fresh table's identity string plus the high-res clock,
-- so two bridges that start in the same second (even on Lua builds where table
-- addresses aren't shown) still get distinct tokens.
local OWNER = tostring({}) .. "-" .. string.format("%.6f", reaper.time_precise())
local lock_confirmed = false
local lock_claim_t = 0

local function write_lock(busy)
  atomic_write_json(lockfile, { started = os.time(), busy = busy or "none", owner = OWNER })
end

local function read_lock()
  local text = read_file(lockfile)
  if not text then return nil end
  -- Backward-compat: pre-3.1 locks were a bare epoch timestamp.
  local started_only = tonumber(text:match("^%d+$"))
  if started_only then return { started = started_only, busy = "none" } end
  local ok, parsed = pcall(json.decode, text)
  if ok and type(parsed) == "table" then return parsed end
  return nil
end

-- `started` is refreshed on every heartbeat (~5s), so on a live bridge it is
-- always recent; it only ages during a synchronous render (no ticks) or after
-- a death. A render lock is never reclaimed inside the generous bound — but
-- past it (power loss mid-render, force-quit) the old behavior bricked the
-- bridge permanently: BRIDGE_ALREADY_RUNNING on every start, watchdog restarts
-- included, until logs/bridge.lock was hand-deleted.
local RENDER_LOCK_MAX_AGE = 6 * 3600

-- nil = proceed (no lock, stale lock, or ancient render lock: reclaim);
-- string = refuse to start with this error.
local function lock_verdict(existing, now_epoch)
  if not existing then return nil end
  local started = tonumber(existing.started)
  local busy = existing.busy or "none"
  if busy == "render" then
    if started and (now_epoch - started) > RENDER_LOCK_MAX_AGE then return nil end
    return "BRIDGE_ALREADY_RUNNING: lock held by a bridge that is rendering (started at " .. tostring(started) .. ")"
  end
  if started and (now_epoch - started) < 60 then
    return "BRIDGE_ALREADY_RUNNING: lock held by a bridge started at " .. tostring(started)
  end
  return nil
end

do
  local verdict = lock_verdict(read_lock(), os.time())
  if verdict then error(verdict) end
  write_lock("none")
  lock_claim_t = reaper.time_precise()
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

-- Error strings look like "<script path>:559: NO_FX: details" (Lua prefixes
-- error() with "file:line: "), so the UPPER_SNAKE code is searched anywhere in
-- the string. Require >=2 leading uppercase: on Windows the script path starts
-- with a drive letter ("C:\..."), and a one-uppercase match decoded every
-- error as code "C".
local function error_code_from(message, fallback)
  return tostring(message):match("(%u%u[%u_]*):") or fallback
end

-- command.id names the reply file in outbox/, so it must never traverse out
-- of it ("../bridge/heartbeat" would clobber the heartbeat — the auth check
-- runs AFTER the id is adopted, so the token does not protect this) and must
-- be a string (a numeric id crashed before any reply, stranding the file).
-- Anything suspicious falls back to the inbox filename's stem, which is what
-- the CLI polls for anyway.
local function safe_id(id, fallback)
  if type(id) == "string" and id:match("^[%w%-_.]+$") and not id:match("%.%.") then
    return id
  end
  return fallback
end

local function move_file(src, dst)
  if os.rename(src, dst) then return true end
  -- Cross-volume or perm failure: fall back to copy + remove. Copy to dst.tmp
  -- then rename into place so a crash mid-copy can't leave a partial dst that
  -- looks like a complete command. If the remove of src fails, drop the copy so
  -- the file isn't left in both places (which would re-queue and re-run it).
  local text = read_file(src)
  if not text then return false end
  local tmp = dst .. ".tmp"
  write_file(tmp, text)
  if not os.rename(tmp, dst) then os.remove(tmp); return false end
  if os.remove(src) then return true end
  os.remove(dst)
  return false
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

-- JSON has no Infinity/NaN; some plugins report a non-finite param value or bound
-- (e.g. an unbounded gain max). Coerce to nil so the field drops out of the result
-- instead of throwing in json.encode and stranding the whole scan with no reply.
local function finite_or_nil(x)
  if type(x) ~= "number" or x ~= x or x == math.huge or x == -math.huge then return nil end
  return x
end

-- Parse the leading signed number from a plugin's formatted display, kHz-aware.
-- Mirrors reaperd.py's _num_from. Without the ×1000, a band displaying "1.20 kHz"
-- parses as 1.2 while the target "1200 Hz" parses as 1200, so the range check
-- rejects every high-frequency band whose plugin shows kHz (FabFilter, ReaEQ…).
-- "inf"/"-inf" map to ±1e30 so unbounded endpoints don't read as nil.
local function parse_display_number(s)
  if type(s) ~= "string" then return nil end
  local low = s:lower()
  if low:find("inf", 1, true) then
    return low:find("-", 1, true) and -1e30 or 1e30
  end
  local num = tonumber(low:match("[-+]?%d*%.?%d+"))
  if not num then return nil end
  if low:find("khz", 1, true) then num = num * 1000 end
  return num
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

-- The master track is a real MediaTrack but is excluded from CountTracks/GetTrack
-- enumeration; REAPER reports its IP_TRACKNUMBER as -1. Use that as the test.
local function is_master_track(track)
  return reaper.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER") == -1
end

-- End time N bars after start_time, preserving the beat offset WITHIN the start
-- bar and honoring tempo/meter changes. The old time_from_bar(bar_from_time(s)+n)
-- discarded the fractional beat (bar_from_time floors to a whole bar), so a 4-bar
-- region from bar 5 beat 3 snapped to the bar 9 downbeat instead of bar 9 beat 3.
local function bars_from(start_time, n)
  local beat_in_measure, measure = reaper.TimeMap2_timeToBeats(0, start_time)
  return reaper.TimeMap2_beatsToTime(0, beat_in_measure, measure + n)
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

-- Heartbeat shape in one place so write_heartbeat and the pre-render heartbeat
-- can't drift. `extra` overlays fields (render passes busy="render").
local function heartbeat_payload(extra)
  -- No reaper_focused: there's no clean native API for it, so it was always
  -- hardcoded true — a lie an agent could act on. Omitted rather than faked.
  -- (A real value needs js_ReaScriptAPI's Js_Window_GetForeground, optional.)
  local hb = {
    alive_at = now(),
    bridge_version = 3,
    project_name = get_project_name(),
    in_flight_command = in_flight_command,
  }
  if extra then for k, v in pairs(extra) do hb[k] = v end end
  return hb
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

  -- The master track is not in CountTracks/GetTrack, so add it explicitly or it
  -- is invisible to the documented get_context discovery call. index 0 + is_master
  -- match find_track's master convention.
  local master = reaper.GetMasterTrack(0)
  local master_vol = reaper.GetMediaTrackInfo_Value(master, "D_VOL")
  local master_info = {
    index = 0,
    is_master = true,
    guid = reaper.GetTrackGUID(master),
    name = "MASTER",
    selected = reaper.IsTrackSelected(master),
    muted = reaper.GetMediaTrackInfo_Value(master, "B_MUTE") == 1,
    soloed = reaper.GetMediaTrackInfo_Value(master, "I_SOLO") ~= 0,
    armed = false,
    volume = master_vol,
    volume_db = db_from_volume(master_vol),
    pan = reaper.GetMediaTrackInfo_Value(master, "D_PAN"),
    folder_depth = 0,
    item_count = 0,
  }
  if include_fx then
    master_info.fx = {}
    for fx = 0, reaper.TrackFX_GetCount(master) - 1 do
      local _, fx_name = reaper.TrackFX_GetFXName(master, fx, "")
      master_info.fx[#master_info.fx + 1] = { index = fx, api_index = fx, scope = "track", name = fx_name }
    end
    master_info.input_fx = {}
  end
  tracks[#tracks + 1] = master_info
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
    -- Master isn't in the CountTracks enumeration; check it first so a GUID
    -- captured for the master (the contract's first-choice selector) resolves.
    local master = reaper.GetMasterTrack(0)
    if reaper.GetTrackGUID(master) == payload.target_track_guid then return master, 0 end
    for i = 0, reaper.CountTracks(0) - 1 do
      local track = reaper.GetTrack(0, i)
      if reaper.GetTrackGUID(track) == payload.target_track_guid then return track, i + 1 end
    end
    error("NO_TARGET_TRACK: No track with guid " .. payload.target_track_guid)
  end
  if payload.target_track_name then
    local found, found_index = nil, nil
    local needle = payload.target_track_name:lower()
    if needle == "master" then
      return reaper.GetMasterTrack(0), 0
    end
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
  if payload.track_name_contains then
    local found, found_index = nil, nil
    local needle = payload.track_name_contains:lower()
    for i = 0, reaper.CountTracks(0) - 1 do
      local track = reaper.GetTrack(0, i)
      local _, name = reaper.GetTrackName(track, "")
      if name:lower():find(needle, 1, true) then
        if found then error("AMBIGUOUS_TARGET_TRACK: Multiple tracks match '" .. payload.track_name_contains .. "'") end
        found, found_index = track, i + 1
      end
    end
    if found then return found, found_index end
    error("NO_TARGET_TRACK: No track containing '" .. payload.track_name_contains .. "'")
  end
  if payload.use_selected_track then
    local selected = reaper.GetSelectedTrack(0, 0)
    if selected then
      return selected, math.floor(reaper.GetMediaTrackInfo_Value(selected, "IP_TRACKNUMBER"))
    end
    error("NO_TARGET_TRACK: use_selected_track=true but nothing is selected")
  end
  error("NO_TARGET_TRACK: Provide target_track_name, target_track_guid, or track_name_contains")
end

local function contains_ci(haystack, needle)
  if not haystack or not needle then return false end
  return tostring(haystack):lower():find(tostring(needle):lower(), 1, true) ~= nil
end

local function find_fx(payload)
  local track, track_index = find_track(payload)
  local explicit_scope = payload.fx_scope or payload.scope
  local scope = explicit_scope or "all"
  local search_track_fx = scope == "all" or scope == "track" or scope == "normal"
  local search_input_fx = scope == "all" or scope == "input" or scope == "rec" or scope == "record"
  local matches = {}

  if payload.fx_index ~= nil then
    -- A bare fx_index with no scope used to silently mean "track FX N" — an
    -- agent that meant input FX N but forgot fx_scope hit the wrong plugin and
    -- got a success reply. Require the scope explicitly; only name searches,
    -- which span both lists, may default it.
    if not explicit_scope then
      error("AMBIGUOUS_SCOPE: fx_index requires an explicit fx_scope (\"track\" or \"input\")")
    end
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
    value = finite_or_nil(value),
    normalized_value = finite_or_nil(normalized),
    min = finite_or_nil(min_value),
    max = finite_or_nil(max_value),
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
    return bars_from(start_time, tonumber(length.bars or 1) or 1) - start_time
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

-- Turn a payload's end / length_bars / length_seconds into an end time, off the
-- given start. Shared by set_time_selection, add_region (and the bars math is
-- now meter-correct via bars_from).
local function resolve_range_end(payload, start_time)
  if payload["end"] then
    return (resolve_position(payload["end"]))
  elseif payload.length_bars then
    return bars_from(start_time, tonumber(payload.length_bars) or 1)
  elseif payload.length_seconds then
    return start_time + (tonumber(payload.length_seconds) or 0)
  end
  error("BAD_RANGE: Provide end, length_bars, or length_seconds")
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

  -- NOTE: depending on the user's "Import MIDI as" preference, InsertMedia can
  -- pop a modal import dialog, which blocks the defer loop until dismissed —
  -- a hang in unattended use. If that bites, set the project's MIDI import mode
  -- via GetSetProjectInfo before this call. Documented in command_schema.md.
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
  -- Force REAPER's "color is set" high bit so a raw RGB int actually shows,
  -- matching the {r,g,b} branch above (the bit is idempotent if already set).
  if type(color) == "number" then return color | 0x1000000 end
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

-- Build the identity block shared by every FX discovery response. `index` is
-- the human-facing zero-based index within its scope; `api_index` is REAPER's
-- encoded index (input FX carry the 0x1000000 offset). The GUID is the stable
-- identity: names can be duplicated and indices shift when a chain is edited.
local function fx_summary(track, api_index, display_fx_index, scope, name, extra)
  local summary = {
    index = display_fx_index or api_index,
    api_index = api_index,
    scope = scope or "track",
    name = name,
    guid = reaper.TrackFX_GetFXGUID(track, api_index),
  }
  if extra then
    for key, value in pairs(extra) do summary[key] = value end
  end
  return summary
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
    local looks_empty = info.name:match("^#%d+$") and info.formatted_value == ""
    if (include_empty or not looks_empty) and (not filter or contains_ci(info.name, filter)) then
      matched_count = matched_count + 1
      if matched_count > offset and #params < limit then
        params[#params + 1] = info
      end
    end
  end
  return {
    track = { index = track_index, name = track_name, guid = reaper.GetTrackGUID(track) },
    fx = fx_summary(track, api_index, display_fx_index, fx_scope, fx_name, {
      parameter_count = param_count,
    }),
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

  if payload.normalized_value ~= nil or payload.value ~= nil then
    local raw = payload.normalized_value ~= nil and payload.normalized_value or payload.value
    local value = tonumber(raw)
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
    -- REAPER has no "set by formatted string" API, and many plugins (FabFilter,
    -- most VST3) expose params as normalized 0..1 with no real-world range, so
    -- min/max can't convert "80 Hz" to normalized. Instead, binary-search the
    -- normalized value whose formatted display matches the target number. Works
    -- for any monotonic numeric param (Hz, dB, %, Q, gain, ratio). Enum/string
    -- params ("Bell", "Off") have no parseable number and are rejected — use
    -- normalized_value for those (scan to find the value that formats right).
    local fv = tostring(payload.formatted_value)
    local target = parse_display_number(fv)
    if not target then
      error("FORMATTED_VALUE_UNSUPPORTED: could not parse a number from '" .. fv
            .. "' (use normalized_value 0..1, or relative)")
    end
    -- Snapshot the live value so a failed search rolls back. The search writes
    -- the param ~26 times (each probe is a real SetParamNormalized); without
    -- this, an out-of-range or non-numeric param left the band stranded at the
    -- last probe — a garbage frequency/gain — instead of where it started.
    local original = reaper.TrackFX_GetParamNormalized(track, api_index, param_index)
    local function fmt_num(norm)
      reaper.TrackFX_SetParamNormalized(track, api_index, param_index, norm)
      local pok, retval, text = pcall(reaper.TrackFX_GetFormattedParamValue, track, api_index, param_index, "")
      local s = ""
      if pok then
        if type(text) == "string" then s = text
        elseif type(retval) == "string" then s = retval end
      end
      return parse_display_number(s)
    end
    local search_ok, search_err = pcall(function()
      local lo, hi = 0.0, 1.0
      local lo_num = fmt_num(lo)
      local hi_num = fmt_num(hi)
      if lo_num == nil or hi_num == nil then
        error("FORMATTED_VALUE_UNSUPPORTED: parameter is not numeric (use normalized_value)")
      end
      local ascending = hi_num >= lo_num
      if (ascending and (target < lo_num or target > hi_num))
         or (not ascending and (target > lo_num or target < hi_num)) then
        error("FORMATTED_VALUE_UNSUPPORTED: " .. fv .. " outside param range ("
              .. tostring(lo_num) .. " .. " .. tostring(hi_num) .. "); use normalized_value")
      end
      local norm = (lo + hi) / 2
      for _ = 1, 24 do
        local num = fmt_num(norm)
        if num == nil then break end
        if (ascending and num < target) or (not ascending and num > target) then
          lo = norm
        else
          hi = norm
        end
        norm = (lo + hi) / 2
      end
      local best, d_best = lo, math.abs((fmt_num(lo) or target) - target)
      local n_hi = fmt_num(hi)
      if n_hi then
        local d_hi = math.abs(n_hi - target)
        if d_hi < d_best then best, d_best = hi, d_hi end
      end
      ok = reaper.TrackFX_SetParamNormalized(track, api_index, param_index, best)
    end)
    if not search_ok then
      reaper.TrackFX_SetParamNormalized(track, api_index, param_index, original)
      error(search_err)
    end
    if not ok then
      reaper.TrackFX_SetParamNormalized(track, api_index, param_index, original)
    end
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
  local raw = reaper.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER")
  local is_master = raw == -1
  return {
    index = is_master and 0 or math.floor(raw),
    name = name,
    guid = reaper.GetTrackGUID(track),
    is_master = is_master or nil,
  }
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
  local end_time = resolve_range_end(payload, start_time)
  reaper.GetSet_LoopTimeRange(true, payload.loop == true, start_time, end_time, false)
  return { time_selection = get_time_selection() }
end

local function command_set_tempo(command)
  local payload = command.payload or {}
  local bpm = tonumber(payload.bpm)
  if not bpm or bpm <= 0 then error("BAD_TEMPO: Provide a positive bpm") end
  -- Sets the project's base/initial tempo. NOTE: in a project that already has
  -- tempo-map markers, this changes the base only, not the markers — the playing
  -- tempo at the cursor may not change. A positioned tempo change would need
  -- SetTempoTimeSigMarker; expose that as its own verb if it's ever needed.
  reaper.SetCurrentBPM(0, bpm, true)
  return { tempo = reaper.Master_GetTempo(), has_tempo_markers = reaper.CountTempoTimeSigMarkers(0) > 0 }
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

-- Phase 2 (Post Mortem Verified Fix Preview), P2-001: track state snapshot and
-- restore. The snapshot is written to disk BEFORE any mutation happens (same
-- crash-safety pattern as the render-settings state file), and covers exactly
-- what the four supported preview operations can mutate: track volume, track
-- pan, per-FX enabled state, and named FX parameter values.

local SNAPSHOT_STATE_DIR = join(root, "state", "snapshots")

local function enumerate_track_fx(track)
  local fx = {}
  for i = 0, reaper.TrackFX_GetCount(track) - 1 do
    local _, name = reaper.TrackFX_GetFXName(track, i, "")
    fx[#fx + 1] = {
      guid = reaper.TrackFX_GetFXGUID(track, i),
      index = i, api_index = i, scope = "track", name = name,
      enabled = reaper.TrackFX_GetEnabled(track, i),
    }
  end
  if reaper.TrackFX_GetRecCount then
    for i = 0, reaper.TrackFX_GetRecCount(track) - 1 do
      local api_index = 0x1000000 + i
      local _, name = reaper.TrackFX_GetFXName(track, api_index, "")
      fx[#fx + 1] = {
        guid = reaper.TrackFX_GetFXGUID(track, api_index),
        index = i, api_index = api_index, scope = "input", name = name,
        enabled = reaper.TrackFX_GetEnabled(track, api_index),
      }
    end
  end
  return fx
end

-- Pure: shape-check a parsed snapshot file. Returns nil when valid, else a
-- typed error string. A snapshot that fails here restores nothing.
local function snapshot_validate(parsed)
  if type(parsed) ~= "table" then return "BAD_SNAPSHOT: not an object" end
  if parsed.schema_version ~= 1 then
    return "BAD_SNAPSHOT: unsupported schema_version"
  end
  local track = parsed.track
  if type(track) ~= "table" or type(track.guid) ~= "string" or track.guid == "" then
    return "BAD_SNAPSHOT: missing track.guid"
  end
  local values = parsed.values
  if type(values) ~= "table" then return "BAD_SNAPSHOT: missing values" end
  if values.fx ~= nil and type(values.fx) ~= "table" then
    return "BAD_SNAPSHOT: values.fx must be a list"
  end
  for _, entry in ipairs(values.fx or {}) do
    if type(entry) ~= "table" or type(entry.guid) ~= "string" or entry.guid == "" then
      return "BAD_SNAPSHOT: fx entry missing guid"
    end
    for _, param in ipairs(entry.parameters or {}) do
      if type(param) ~= "table" or type(param.index) ~= "number"
         or type(param.normalized_value) ~= "number" then
        return "BAD_SNAPSHOT: fx parameter entry needs index and normalized_value"
      end
    end
  end
  return nil
end

-- Pure: given a validated snapshot and an index of the live FX chain
-- (track_guid plus fx_by_guid = guid -> {api_index, ...}), plan the restore
-- writes. Restores whatever still resolves and reports exactly what it could
-- not restore; never invents a write for state the snapshot does not contain.
local function restore_plan(snapshot, live)
  if snapshot.track.guid ~= live.track_guid then
    return nil, "SNAPSHOT_TRACK_MISMATCH: snapshot is for track "
      .. snapshot.track.guid .. ", not " .. tostring(live.track_guid)
  end
  local ops, unrestored = {}, {}
  local values = snapshot.values
  if values.volume ~= nil then
    ops[#ops + 1] = { kind = "volume", value = values.volume }
  end
  if values.pan ~= nil then
    ops[#ops + 1] = { kind = "pan", value = values.pan }
  end
  for _, fx in ipairs(values.fx or {}) do
    local live_fx = live.fx_by_guid[fx.guid]
    if not live_fx then
      unrestored[#unrestored + 1] = {
        kind = "fx", guid = fx.guid, name = fx.name, reason = "FX_NOT_FOUND",
      }
    else
      if fx.enabled ~= nil then
        ops[#ops + 1] = {
          kind = "fx_enabled", api_index = live_fx.api_index,
          guid = fx.guid, value = fx.enabled,
        }
      end
      for _, param in ipairs(fx.parameters or {}) do
        ops[#ops + 1] = {
          kind = "fx_param", api_index = live_fx.api_index, guid = fx.guid,
          parameter_index = param.index, value = param.normalized_value,
        }
      end
    end
  end
  return { ops = ops, unrestored = unrestored }
end

local function command_snapshot_track_state(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  local _, track_name = reaper.GetTrackName(track, "")
  local fx = enumerate_track_fx(track)
  local by_guid, by_api = {}, {}
  for _, entry in ipairs(fx) do
    by_guid[entry.guid] = entry
    by_api[entry.api_index] = entry
  end

  -- Named parameters to capture (a preview touches one parameter; snapshot
  -- exactly what the caller will mutate and fail closed on anything that
  -- does not resolve — this runs BEFORE the mutation, so refusing is free).
  for _, want in ipairs(payload.parameters or {}) do
    local entry
    if want.fx_guid then
      entry = by_guid[want.fx_guid]
      if not entry then error("NO_FX: no FX with guid " .. tostring(want.fx_guid)) end
    elseif want.fx_index ~= nil then
      local scope = want.fx_scope
      if scope ~= "track" and scope ~= "input" then
        error("AMBIGUOUS_SCOPE: parameters[].fx_index requires fx_scope \"track\" or \"input\"")
      end
      local api = (scope == "input" and 0x1000000 or 0) + tonumber(want.fx_index)
      entry = by_api[api]
      if not entry then
        error("NO_FX: no " .. scope .. " FX at index " .. tostring(want.fx_index))
      end
    else
      error("BAD_SNAPSHOT_REQUEST: parameters[] entries need fx_guid or fx_index+fx_scope")
    end
    local pidx = tonumber(want.parameter_index)
    if not pidx or pidx < 0
       or pidx >= reaper.TrackFX_GetNumParams(track, entry.api_index) then
      error("NO_FX_PARAM: parameter_index " .. tostring(want.parameter_index)
        .. " out of range for " .. entry.name)
    end
    local _, pname = reaper.TrackFX_GetParamName(track, entry.api_index, pidx, "")
    entry.parameters = entry.parameters or {}
    entry.parameters[#entry.parameters + 1] = {
      index = pidx,
      name = pname,
      normalized_value = reaper.TrackFX_GetParamNormalized(track, entry.api_index, pidx),
    }
  end

  local volume = reaper.GetMediaTrackInfo_Value(track, "D_VOL")
  local snapshot = {
    schema_version = 1,
    snapshot_id = "snap-" .. os.date("!%Y%m%dT%H%M%SZ") .. "-"
      .. string.format("%06x", math.floor((reaper.time_precise() % 1) * 0xffffff)),
    created_at = now(),
    track = { guid = reaper.GetTrackGUID(track), name = track_name },
    values = {
      volume = volume,
      volume_db = db_from_volume(volume),
      pan = reaper.GetMediaTrackInfo_Value(track, "D_PAN"),
      fx = fx,
    },
  }
  if reaper.RecursiveCreateDirectory then
    reaper.RecursiveCreateDirectory(SNAPSHOT_STATE_DIR, 0)
  end
  local path = join(SNAPSHOT_STATE_DIR, snapshot.snapshot_id .. ".json")
  atomic_write_json(path, snapshot)
  return { snapshot_id = snapshot.snapshot_id, path = path, snapshot = snapshot }
end

local function command_restore_track_state(command)
  local payload = command.payload or {}
  local snapshot_id = safe_id(payload.snapshot_id, nil)
  if not snapshot_id then
    error("BAD_SNAPSHOT_REQUEST: Provide snapshot_id (from snapshot_track_state)")
  end
  local path = join(SNAPSHOT_STATE_DIR, snapshot_id .. ".json")
  local text = read_file(path)
  if not text then error("NO_SNAPSHOT: no snapshot " .. snapshot_id) end
  local decoded_ok, parsed = pcall(json.decode, text)
  if not decoded_ok then error("BAD_SNAPSHOT: snapshot file is not valid JSON") end
  local invalid = snapshot_validate(parsed)
  if invalid then error(invalid) end

  -- Resolve by GUID only: a deleted track fails closed here (NO_TARGET_TRACK)
  -- and nothing is written.
  local track = find_track({ target_track_guid = parsed.track.guid })
  local fx_by_guid = {}
  for _, entry in ipairs(enumerate_track_fx(track)) do
    fx_by_guid[entry.guid] = entry
  end
  local plan, plan_err = restore_plan(parsed, {
    track_guid = reaper.GetTrackGUID(track),
    fx_by_guid = fx_by_guid,
  })
  if not plan then error(plan_err) end

  local restored = {}
  for _, op in ipairs(plan.ops) do
    if op.kind == "volume" then
      reaper.SetMediaTrackInfo_Value(track, "D_VOL", op.value)
    elseif op.kind == "pan" then
      reaper.SetMediaTrackInfo_Value(track, "D_PAN", op.value)
    elseif op.kind == "fx_enabled" then
      reaper.TrackFX_SetEnabled(track, op.api_index, op.value)
    elseif op.kind == "fx_param" then
      reaper.TrackFX_SetParamNormalized(track, op.api_index, op.parameter_index, op.value)
    end
    restored[#restored + 1] = {
      kind = op.kind, guid = op.guid, parameter_index = op.parameter_index,
    }
  end
  local fully_restored = #plan.unrestored == 0
  local deleted = false
  if payload.delete_after == true and fully_restored then
    deleted = os.remove(path) ~= nil
  end
  return {
    track = track_summary(track),
    snapshot_id = snapshot_id,
    restored = restored,
    unrestored = plan.unrestored,
    fully_restored = fully_restored,
    deleted = deleted,
  }
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

-- Split a REAPER state chunk into lines (tolerating \r). Shared by the FX-chain
-- splicer and its tests.
local function split_lines(s)
  local out = {}
  for line in (s .. "\n"):gmatch("(.-)\n") do out[#out + 1] = (line:gsub("\r$", "")) end
  return out
end

-- Splice a .RfxChain body (a sequence of FX blocks) into a track state chunk as
-- direct children of the track's top-level <FXCHAIN>/<MASTERFXLIST>; if none
-- exists yet, wrap a fresh one before the track's close. Pure + testable.
--
-- Depth follows REAPER's chunk invariant: a block OPENS on a line whose first
-- non-space char is '<' and CLOSES on a line that is a lone '>'. A line that
-- both opens and closes ('<X ...>') is net-zero and must NOT bump depth — the
-- old code counted its '<' but never its '>', skewing every block after it and
-- splicing into the middle of an FX (a corrupt chunk SetTrackStateChunk rejects,
-- or worse, silently mangles). Data lines never start with '<' or equal '>'
-- (brackets inside values are quoted), so they read as depth-0.
-- Returns merged_lines, or nil + an error code.
local function splice_fx_chain(lines, body_lines, container)
  local open_idx, close_idx, depth = nil, nil, 0
  for i = 1, #lines do
    local t = lines[i]:match("^%s*(.-)%s*$")
    local opens = t:sub(1, 1) == "<"
    local single = opens and t:sub(-1) == ">"   -- '<X ...>' on one line: net 0
    if opens and not single and depth == 1
       and (t:find("^<FXCHAIN") or t:find("^<MASTERFXLIST")) then
      open_idx = i
    end
    if opens and not single then
      depth = depth + 1
    elseif t == ">" then
      depth = depth - 1
      if open_idx and not close_idx and depth == 1 then close_idx = i end
    end
  end

  local merged = {}
  local function push_body() for _, v in ipairs(body_lines) do merged[#merged + 1] = v end end

  if close_idx then
    for i = 1, close_idx - 1 do merged[#merged + 1] = lines[i] end
    push_body()
    for i = close_idx, #lines do merged[#merged + 1] = lines[i] end
  else
    local track_close
    for i = #lines, 1, -1 do
      if lines[i]:match("^%s*>%s*$") then track_close = i; break end
    end
    if not track_close then return nil, "CHUNK_NO_TRACK_CLOSE" end
    for i = 1, track_close - 1 do merged[#merged + 1] = lines[i] end
    merged[#merged + 1] = "<" .. container
    merged[#merged + 1] = "SHOW 0"
    merged[#merged + 1] = "LASTSEL 0"
    merged[#merged + 1] = "DOCKED 0"
    push_body()
    merged[#merged + 1] = ">"
    for i = track_close, #lines do merged[#merged + 1] = lines[i] end
  end
  return merged
end

-- Load a saved REAPER FX chain (.RfxChain) onto a track WITH its stored state.
-- add_fx only instantiates a single plugin by name (TrackFX_AddByName) and
-- cannot carry a chain file's saved parameters. This reads the .RfxChain and
-- merges it into the track's FXCHAIN via the state chunk, so the dialed-in
-- settings come across intact. Appends to whatever FX the track already has.
local function command_add_fx_chain(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  -- The master stores its FX under <MASTERFXLIST>; regular tracks use <FXCHAIN>.
  local container = is_master_track(track) and "MASTERFXLIST" or "FXCHAIN"

  local name = payload.chain_name or payload.fx_chain or payload.name
  local path = payload.chain_path
  if not path or path == "" then
    if not name or name == "" then
      error("BAD_CHAIN_NAME: Provide chain_name (a saved .RfxChain) or chain_path")
    end
    local base = tostring(name):gsub("%.RfxChain$", "")
    path = join(reaper.GetResourcePath(), "FXChains", base .. ".RfxChain")
  end
  if not exists(path) then error("CHAIN_NOT_FOUND: " .. tostring(path)) end

  local body = read_file(path)
  if not body or not body:match("%S") then error("CHAIN_EMPTY: " .. tostring(path)) end
  body = body:gsub("\r\n", "\n"):gsub("\r", "\n"):gsub("%s+$", "")

  local function is_fx_open(line)
    return line:match("^%s*<VST") or line:match("^%s*<JS")
      or line:match("^%s*<AU") or line:match("^%s*<CLAP")
      or line:match("^%s*<LV2") or line:match("^%s*<DX")
  end

  local body_lines = split_lines(body)
  local fx_in_chain = 0
  for _, line in ipairs(body_lines) do
    if is_fx_open(line) then fx_in_chain = fx_in_chain + 1 end
  end
  if fx_in_chain == 0 then error("CHAIN_NO_FX: no FX blocks parsed from " .. tostring(path)) end

  local ok_chunk, chunk = reaper.GetTrackStateChunk(track, "", false)
  if not ok_chunk then error("CHUNK_READ_FAILED: GetTrackStateChunk returned false") end

  local merged, splice_err = splice_fx_chain(split_lines(chunk), body_lines, container)
  if not merged then error(splice_err .. ": malformed track chunk") end

  -- Snapshot the FX count, write, then verify it grew by exactly the chain's FX.
  -- If the splice landed wrong (a chunk shape we mis-parsed), the count won't
  -- match — restore the original chunk so a parser bug can never leave a corrupt
  -- or half-merged track, and fail loud instead.
  local fx_before = reaper.TrackFX_GetCount(track)
  if not reaper.SetTrackStateChunk(track, table.concat(merged, "\n"), false) then
    error("CHUNK_WRITE_FAILED: SetTrackStateChunk rejected the merged chunk")
  end
  local fx_after = reaper.TrackFX_GetCount(track)
  if fx_after ~= fx_before + fx_in_chain then
    reaper.SetTrackStateChunk(track, chunk, false)  -- restore the original chain
    error("CHAIN_SPLICE_MISMATCH: expected " .. (fx_before + fx_in_chain)
          .. " FX after splice, got " .. fx_after .. " — restored original chain")
  end
  local fx_names = {}
  for i = 0, fx_after - 1 do
    local _, fxname = reaper.TrackFX_GetFXName(track, i, "")
    fx_names[#fx_names + 1] = fxname
  end

  return {
    track = track_summary(track),
    chain = { name = name or path, path = path, fx_in_chain = fx_in_chain },
    fx_count_after = fx_after,
    fx = fx_names,
  }
end

local function command_remove_fx(command)
  local payload = command.payload or {}
  local track, _, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
  local ok = reaper.TrackFX_Delete(track, api_index)
  if not ok then error("REMOVE_FX_FAILED: REAPER rejected the FX delete") end
  return {
    track = track_summary(track),
    removed = { index = display_fx_index or api_index, scope = fx_scope or "track", name = fx_name },
  }
end

local function command_bypass_fx(command)
  local payload = command.payload or {}
  local track, _, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
  local bypass = payload.bypass ~= false
  reaper.TrackFX_SetEnabled(track, api_index, not bypass)
  return {
    track = track_summary(track),
    fx = { index = display_fx_index or api_index, scope = fx_scope or "track", name = fx_name, bypassed = bypass },
  }
end

local function command_move_fx(command)
  local payload = command.payload or {}
  local track, _, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
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
  local end_time = resolve_range_end(payload, start_time)
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
      end_time = bars_from(start_time, tonumber(payload.length_bars) or 1)
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

-- REAPER's render action (42230) is synchronous AND leaves the "Rendering to
-- File" progress window open until it is dismissed, UNLESS the window's
-- "Automatically close when finished" checkbox is ticked. That checkbox is the
-- config var `renderclosewhendone` bit 0 (&1); on a fresh install it is off, so
-- Main_OnCommand(42230) never returns and the whole defer loop hangs (observed
-- ~11 min) until the window is closed by hand. Force the bit on for our render
-- and restore the user's setting afterward. This is the bridge's ONLY use of
-- SWS (SNM_*), so degrade gracefully when SWS is absent: we cannot force the
-- checkbox, so the caller surfaces a warning instead of silently risking a hang.
local RENDER_AUTOCLOSE_VAR = "renderclosewhendone"

local function ensure_render_autoclose()
  if not (reaper.SNM_GetIntConfigVar and reaper.SNM_SetIntConfigVar) then
    return { guaranteed = false, reason = "SWS not installed: cannot force render-window auto-close" }
  end
  local cur = reaper.SNM_GetIntConfigVar(RENDER_AUTOCLOSE_VAR, -1)
  if cur == -1 then
    -- -1 is the "var not found" sentinel; the real bitfield is never negative.
    return { guaranteed = false, reason = "renderclosewhendone unavailable" }
  end
  if cur & 1 == 1 then
    return { guaranteed = true }  -- already auto-closing; leave it, nothing to restore
  end
  reaper.SNM_SetIntConfigVar(RENDER_AUTOCLOSE_VAR, cur | 1)
  return { guaranteed = true, restore = cur }
end

local function restore_render_autoclose(token)
  if token and token.restore ~= nil and reaper.SNM_SetIntConfigVar then
    reaper.SNM_SetIntConfigVar(RENDER_AUTOCLOSE_VAR, token.restore)
  end
end

-- Capture provenance is part of the public result contract. A caller must never
-- need to infer whether a WAV is a real isolated stem by parsing a prose note.
-- `full_mix` is deliberately explicit: it is useful for debugging, but unsafe
-- as evidence for a per-track diagnosis or cross-track masking claim.
local function capture_provenance(isolate, is_master)
  if isolate then
    return {
      capture_scope = "isolated_track",
      isolation_verified = true,
      note = "Isolated item-less routing track (e.g. a Kontakt multi-out stem): target exclusive-soloed and the FX on every downstream bus (parent folders + master) bypassed for the render, so this is the track's own signal and its own FX only. Sends silenced by the solo. All solo and FX states restored after.",
    }
  end
  if is_master then
    return {
      capture_scope = "master_output",
      isolation_verified = false,
      note = "Master output capture: this WAV represents the project master, not an isolated track.",
    }
  end
  return {
    capture_scope = "full_mix",
    isolation_verified = false,
    note = "CAUTION: this track has media items and is NOT isolated. REAPER's stems render returns the full master mix here (isolation via solo silences tracks whose FX, e.g. some amp sims, don't render offline). Treat these measurements as the whole mix, not this track alone, until per-track isolation for item tracks is solved.",
  }
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
  atomic_write_json(paths.heartbeat, heartbeat_payload({ busy = "render" }))
  -- Refresh the lock too: render blocks the loop, so without this a long render
  -- lets the lock age past the 60s stale-reclaim window and a second bridge could
  -- grab the inbox. Mark the lock busy=render so a startup bridge NEVER reclaims
  -- it mid-render (the age check is bypassed for render-busy locks).
  write_lock("render")
  -- Render with the project's most recent render settings (format, sample rate,
  -- etc. must be configured once in REAPER's Render dialog).
  local autoclose = ensure_render_autoclose()
  reaper.Main_OnCommand(42230, 0) -- 42230 = File: Render project
  restore_render_autoclose(autoclose)
  return {
    rendered = true,
    output_file = select(2, reaper.GetSetProjectInfo_String(0, "RENDER_FILE", "", false)),
    note = "Render used REAPER's last-saved render settings.",
    render_autoclose_warning = autoclose.guaranteed and nil or autoclose.reason,
  }
end

-- Post Mortem capture: render ONE track's post-FX output to a temp WAV using
-- REAPER's stems render source (RENDER_SETTINGS=2, selected tracks, pre-master)
-- and custom bounds (RENDER_BOUNDSFLAG=0 + RENDER_STARTPOS/ENDPOS), so neither
-- the user's solo states nor their time selection is ever touched. Everything
-- this mutates (track selection, render settings) is restored afterwards, even
-- when the render throws — hence no undo block (nothing left to Ctrl+Z).
local function command_capture_track_audio(command)
  if not config.allow_risk_level_3 then
    error("CAPTURE_BLOCKED: capture_track_audio is gated; set allow_risk_level_3 true in bridge_config.json")
  end
  local payload = command.payload or {}
  local track, track_index = find_track(payload)
  local output_file = payload.output_file
  if not output_file or output_file == "" then
    error("BAD_PAYLOAD: output_file is required (use a unique/timestamped name so REAPER never prompts to overwrite)")
  end
  local duration = tonumber(payload.duration_seconds) or 30
  if duration <= 0 or duration > 600 then
    error("BAD_PAYLOAD: duration_seconds must be in (0, 600]")
  end

  -- Capture range: explicit start_seconds, else the active time selection's
  -- range (read-only — custom render bounds mean we never modify it), else
  -- the edit cursor.
  local start_time
  if payload.start_seconds ~= nil then
    start_time = tonumber(payload.start_seconds) or 0
  else
    local ts_start, ts_end = reaper.GetSet_LoopTimeRange(false, false, 0, 0, false)
    if ts_end > ts_start then
      start_time = ts_start
      duration = math.min(duration, ts_end - ts_start)
    else
      start_time = reaper.GetCursorPosition()
    end
  end
  local end_time = start_time + duration

  -- Split output into directory + name: RENDER_FILE is the directory,
  -- RENDER_PATTERN the filename (extension comes from the sink format).
  local dir, name = output_file:match("^(.*)[/\\]([^/\\]+)$")
  if not dir then dir, name = "", output_file end
  local pattern = name:gsub("%.wav$", "")

  -- Capture everything we are about to mutate.
  local saved = {
    settings = reaper.GetSetProjectInfo(0, "RENDER_SETTINGS", 0, false),
    bounds = reaper.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", 0, false),
    startpos = reaper.GetSetProjectInfo(0, "RENDER_STARTPOS", 0, false),
    endpos = reaper.GetSetProjectInfo(0, "RENDER_ENDPOS", 0, false),
    srate = reaper.GetSetProjectInfo(0, "RENDER_SRATE", 0, false),
    file = select(2, reaper.GetSetProjectInfo_String(0, "RENDER_FILE", "", false)),
    pattern = select(2, reaper.GetSetProjectInfo_String(0, "RENDER_PATTERN", "", false)),
    format = select(2, reaper.GetSetProjectInfo_String(0, "RENDER_FORMAT", "", false)),
  }
  local saved_selection = {}
  for i = 0, reaper.CountSelectedTracks(0) - 1 do
    saved_selection[#saved_selection + 1] = reaper.GetSelectedTrack(0, i)
  end

  -- Isolation. "Stems (selected tracks)" can only render a track that holds
  -- media items; an item-less routing track (e.g. a Kontakt multi-out stem)
  -- silently falls back to the master mix, so the capture returns the WHOLE
  -- mix — every stem comes back byte-identical. Force real isolation by
  -- exclusive-soloing the target for the render, then restoring every track's
  -- solo state. Verified live 2026-07-09: unsoloed Kick/Snare/Hi_Hats captures
  -- were identical full-mix renders; soloed, each isolated correctly.
  local master_track = reaper.GetMasterTrack(0)
  -- Only item-less routing tracks (no media items — audio arrives via receives,
  -- e.g. a Kontakt multi-out drum stem) need the solo+bus-bypass isolation:
  -- REAPER's stems render can't render them and falls back to the full master
  -- mix. Tracks WITH media items already render as isolated pre-master stems
  -- (verified on track "L", 2026-07-03); soloing THEM makes the stems render
  -- output silence, so leave them on the plain stems path.
  local isolate = track ~= master_track and reaper.CountTrackMediaItems(track) == 0
  local provenance = capture_provenance(isolate, track == master_track)
  local track_count = reaper.CountTracks(0)
  local saved_solo = {}
  -- Downstream FX would color an isolated capture: the master bus plus every
  -- ancestor folder bus the target feeds up through. Bypass their FX for the
  -- render so the measurement is the target's OWN signal (its own FX included),
  -- not the bus/mastering chain (verified 2026-07-09: the master limiter added
  -- ~7 dB to a soloed kick). Sends to other tracks are already silenced by the
  -- exclusive solo, so they need no handling. Every bypass is restored after.
  -- saved_fx[k] = { track = <MediaTrack>, count = <n>, states = { [i] = bool } }
  local bypass_tracks = {}
  local saved_fx = {}
  if isolate then
    for i = 0, track_count - 1 do
      saved_solo[i] = reaper.GetMediaTrackInfo_Value(reaper.GetTrack(0, i), "I_SOLO")
    end
    bypass_tracks[#bypass_tracks + 1] = master_track
    local parent = reaper.GetParentTrack(track)
    while parent do
      bypass_tracks[#bypass_tracks + 1] = parent
      parent = reaper.GetParentTrack(parent)
    end
    for k = 1, #bypass_tracks do
      local bt = bypass_tracks[k]
      local n = reaper.TrackFX_GetCount(bt)
      local states = {}
      for i = 0, n - 1 do states[i] = reaper.TrackFX_GetEnabled(bt, i) end
      saved_fx[k] = { track = bt, count = n, states = states }
    end
  end

  local autoclose_token = nil
  local ok, result = pcall(function()
    reaper.SetOnlyTrackSelected(track)
    if isolate then
      for i = 0, track_count - 1 do
        reaper.SetMediaTrackInfo_Value(reaper.GetTrack(0, i), "I_SOLO", 0)
      end
      reaper.SetMediaTrackInfo_Value(track, "I_SOLO", 1)
      for k = 1, #saved_fx do
        local sf = saved_fx[k]
        for i = 0, sf.count - 1 do reaper.TrackFX_SetEnabled(sf.track, i, false) end
      end
    end
    reaper.GetSetProjectInfo(0, "RENDER_SETTINGS", 2, true) -- selected-track render, custom bounds
    reaper.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", 0, true) -- custom time bounds
    reaper.GetSetProjectInfo(0, "RENDER_STARTPOS", start_time, true)
    reaper.GetSetProjectInfo(0, "RENDER_ENDPOS", end_time, true)
    reaper.GetSetProjectInfo(0, "RENDER_SRATE", tonumber(payload.sample_rate) or 48000, true)
    reaper.GetSetProjectInfo_String(0, "RENDER_FILE", dir, true)
    reaper.GetSetProjectInfo_String(0, "RENDER_PATTERN", pattern, true)
    -- 4-byte sink id, reversed: "wave" -> "evaw" (default WAV sink settings).
    reaper.GetSetProjectInfo_String(0, "RENDER_FORMAT", "evaw", true)

    -- REAPER tells us exactly what it will write; trust that over our guess.
    local _, targets = reaper.GetSetProjectInfo_String(0, "RENDER_TARGETS", "", false)
    local target = targets and targets:match("^[^;]+") or output_file

    -- Same blocking-render heartbeat/lock dance as command_render.
    atomic_write_json(paths.heartbeat, heartbeat_payload({ busy = "render" }))
    write_lock("render")
    autoclose_token = ensure_render_autoclose()
    reaper.Main_OnCommand(42230, 0) -- File: Render project

    local f = io.open(target, "rb")
    if not f then
      error("CAPTURE_FAILED: render completed but no file at " .. tostring(target))
    end
    local size = f:seek("end")
    f:close()
    if size == 0 then error("CAPTURE_FAILED: rendered file is empty: " .. tostring(target)) end

    -- Loudness stats for the most recent render. Semicolon-separated
    -- key:value string, not JSON; pass the raw string through and parse
    -- LUFS-I defensively (key name observed as LUFSI).
    local _, stats = reaper.GetSetProjectInfo_String(0, "RENDER_STATS", "", false)
    local lufs_i = stats and tonumber(stats:match("LUFSI:([%-%d%.]+)")) or nil

    local _, track_name = reaper.GetTrackName(track, "")
    return {
      track = { index = track_index, name = track_name, guid = reaper.GetTrackGUID(track) },
      file_path = target,
      file_size_bytes = size,
      duration_seconds = end_time - start_time,
      start_seconds = start_time,
      sample_rate = tonumber(payload.sample_rate) or 48000,
      render_loudness_lufs = finite_or_nil(lufs_i),
      render_stats_raw = stats ~= "" and stats or nil,
      capture_scope = provenance.capture_scope,
      isolation_verified = provenance.isolation_verified,
      note = provenance.note,
      render_autoclose_warning = autoclose_token.guaranteed and nil or autoclose_token.reason,
    }
  end)

  -- Restore, error or not. REAPER has no try/finally; this is it.
  reaper.GetSetProjectInfo(0, "RENDER_SETTINGS", saved.settings, true)
  reaper.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", saved.bounds, true)
  reaper.GetSetProjectInfo(0, "RENDER_STARTPOS", saved.startpos, true)
  reaper.GetSetProjectInfo(0, "RENDER_ENDPOS", saved.endpos, true)
  reaper.GetSetProjectInfo(0, "RENDER_SRATE", saved.srate, true)
  reaper.GetSetProjectInfo_String(0, "RENDER_FILE", saved.file, true)
  reaper.GetSetProjectInfo_String(0, "RENDER_PATTERN", saved.pattern, true)
  reaper.GetSetProjectInfo_String(0, "RENDER_FORMAT", saved.format, true)
  restore_render_autoclose(autoclose_token)
  if isolate then
    for i = 0, track_count - 1 do
      reaper.SetMediaTrackInfo_Value(reaper.GetTrack(0, i), "I_SOLO", saved_solo[i] or 0)
    end
    for k = 1, #saved_fx do
      local sf = saved_fx[k]
      for i = 0, sf.count - 1 do reaper.TrackFX_SetEnabled(sf.track, i, sf.states[i]) end
    end
  end
  if #saved_selection > 0 then
    reaper.SetOnlyTrackSelected(saved_selection[1])
    for i = 2, #saved_selection do
      reaper.SetTrackSelected(saved_selection[i], true)
    end
  else
    reaper.SetTrackSelected(track, false)
  end

  if not ok then error(result) end
  return result
end

local AUTOMATION_MODE_NAMES = {
  [0] = "trim/read", [1] = "read", [2] = "touch", [3] = "write", [4] = "latch",
}

-- Read-only routing snapshot for mix diagnosis: sends, receives, parent bus,
-- volume, pan, phase, automation mode. All volumes are converted to dB here
-- in the bridge (D_VOL is linear, 1.0 = unity) so clients always see dB.
local function command_get_track_routing(command)
  local payload = command.payload or {}
  local track, track_index = find_track(payload)

  -- GetTrackSendName's index space is hardware outputs first, THEN track
  -- sends — a category-0 send index must be offset by the hw-output count.
  local hw_output_count = reaper.GetTrackNumSends(track, 1)

  local function send_entries(category)
    -- category 0 = sends, -1 = receives (hardware outs deliberately excluded)
    local entries = {}
    for i = 0, reaper.GetTrackNumSends(track, category) - 1 do
      local _, other_name
      if category < 0 then
        _, other_name = reaper.GetTrackReceiveName(track, i)
      else
        _, other_name = reaper.GetTrackSendName(track, hw_output_count + i)
      end
      local function v(key) return reaper.GetTrackSendInfo_Value(track, category, i, key) end
      entries[#entries + 1] = {
        [category < 0 and "source_track_name" or "target_track_name"] = other_name,
        volume_db = db_from_volume(v("D_VOL")),
        pan = v("D_PAN"),
        mute = v("B_MUTE") == 1,
        mono = v("B_MONO") == 1,
        phase_inverted = v("B_PHASE") == 1,
        src_channel = math.floor(v("I_SRCCHAN")),
        dst_channel = math.floor(v("I_DSTCHAN")),
      }
    end
    return entries
  end

  local parent = reaper.GetParentTrack(track)
  local parent_info = nil
  if parent then
    local _, parent_name = reaper.GetTrackName(parent, "")
    parent_info = {
      name = parent_name,
      index = math.floor(reaper.GetMediaTrackInfo_Value(parent, "IP_TRACKNUMBER")),
      guid = reaper.GetTrackGUID(parent),
    }
  end

  local _, track_name = reaper.GetTrackName(track, "")
  local automode = math.floor(reaper.GetMediaTrackInfo_Value(track, "I_AUTOMODE"))
  return {
    track = { index = track_index, name = track_name, guid = reaper.GetTrackGUID(track) },
    sends = send_entries(0),
    receives = send_entries(-1),
    parent_track = parent_info,
    volume_db = db_from_volume(reaper.GetMediaTrackInfo_Value(track, "D_VOL")),
    pan = reaper.GetMediaTrackInfo_Value(track, "D_PAN"),
    phase_inverted = reaper.GetMediaTrackInfo_Value(track, "B_PHASE") == 1,
    automation_mode = AUTOMATION_MODE_NAMES[automode] or tostring(automode),
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
      local entry = fx_summary(track, api_index, fx, scope, fx_name, {
        enabled = reaper.TrackFX_GetEnabled(track, api_index),
        parameter_count = reaper.TrackFX_GetNumParams(track, api_index),
      })
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
    -- Master is outside the CountTracks enumeration; scan it too (master-bus
    -- limiter / EQ / dither live here and were invisible to the no-target scan).
    local master = reaper.GetMasterTrack(0)
    local master_summary = track_summary(master)
    master_summary.fx = scan_track_fx(master, include_values, max_params)
    total_fx = total_fx + #master_summary.fx
    tracks[#tracks + 1] = master_summary
  end
  return {
    project_name = get_project_name(),
    track_count = #tracks,
    fx_count = total_fx,
    include_values = include_values,
    tracks = tracks,
  }
end

-- Enumerate plugins INSTALLED in REAPER (not just FX already on tracks).
-- add_fx fails when fx_name doesn't match REAPER's exact listing; this lets an
-- agent discover the precise name (incl. "VST3:" prefix and vendor suffix)
-- before adding. Optional payload.query filters case-insensitively by substring.
local function command_enum_installed_fx(command)
  local payload = command.payload or {}
  local query = payload.query and tostring(payload.query):lower() or nil
  local matches = {}
  local i = 0
  while true do
    local ok_, name = reaper.EnumInstalledFX(i)
    if not ok_ or not name or name == "" then break end
    if not query or name:lower():find(query, 1, true) then
      matches[#matches + 1] = name
    end
    i = i + 1
  end
  return { query = payload.query or nil, count = #matches, fx = matches }
end

-- Discover a drum library's note->piece mapping by reading the MIDI note
-- names REAPER has for the track. Most serious drum samplers (GGD, Superior
-- Drummer, EZdrummer, BFD, Additive Drums) install a .midnam that REAPER
-- exposes via GetTrackMIDINoteNameEx; the agent's mapdetect.match_roles then
-- classifies those names into groovekit roles. This is the generic, library-
-- agnostic path. Kits with no .midnam (some Kontakt libraries) return an
-- empty note list and the agent falls back to GM Standard / a manual map.
local function command_discover_drum_map(command)
  local payload = command.payload or {}
  local track, track_index = find_track(payload)
  local channels = payload.channels or { 0 }
  if type(channels) ~= "table" then channels = { 0 } end
  local max_pitch = payload.max_pitch or 127
  local notes = {}
  local any_name = false
  for _, chan in ipairs(channels) do
    for pitch = 0, max_pitch do
      -- GetTrackMIDINoteNameEx takes the MediaTrack (the non-Ex variant wants
      -- a track INDEX, so passing a MediaTrack always failed) and returns the
      -- single name string, nil/"" when no name is set for that note.
      local name = reaper.GetTrackMIDINoteNameEx(0, track, pitch, chan)
      if name and name ~= "" then
        any_name = true
        -- Key by pitch; first channel that names a note wins. Store the
        -- channel so the agent can report which channel a kit lives on.
        if notes[tostring(pitch)] == nil then
          notes[tostring(pitch)] = { name = name, channel = chan }
        end
      end
    end
  end
  local _, track_name = reaper.GetTrackName(track, "")
  local fx_names = {}
  for i = 0, reaper.TrackFX_GetCount(track) - 1 do
    local _, fname = reaper.TrackFX_GetFXName(track, i, "")
    fx_names[#fx_names + 1] = fname
  end
  return {
    track = { index = track_index, name = track_name },
    fx = fx_names,
    channels = channels,
    has_note_names = any_name,
    note_count = (function()
      local n = 0; for _ in pairs(notes) do n = n + 1 end return n
    end)(),
    notes = notes,
  }
end

local handlers = {}

-- Commands that don't need an undo block: they read state, not project state.
-- Everything else mutates the project and gets wrapped. Named for what it IS.
local NO_UNDO_BLOCK = {
  get_context = true, get_fx_parameters = true, scan_fx = true,
  enum_installed_fx = true,
  discover_drum_map = true,
  get_track_routing = true,
  -- capture_track_audio restores everything it touches (selection, render
  -- settings), so there is no project state a user would want to Ctrl+Z.
  capture_track_audio = true,
  -- snapshot_track_state only reads project state; its write is a state file
  -- outside the project.
  snapshot_track_state = true,
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

  -- batch opens its own single Undo block around the whole set, so don't
  -- double-wrap it here.
  local self_wraps = command.type == "batch"
  local undo_started = false
  if is_mutating(command.type) and not in_batch and not self_wraps then
    reaper.Undo_BeginBlock()
    undo_started = true
  end
  -- Run the handler under pcall so a thrown error (AMBIGUOUS_FX,
  -- SET_PARAM_FAILED, RANGE_OCCUPIED, ...) still closes the Undo block.
  -- REAPER does NOT auto-close an open Undo_BeginBlock, so without this the
  -- block would stay open and fold the user's subsequent manual edits into one
  -- giant undo step for the rest of the session. Any partial mutation the
  -- handler made before throwing stays inside the now-closed block (Cmd+Z safe).
  local ok, data = pcall(handler, command)
  if undo_started then
    reaper.Undo_EndBlock(command.undo_label or ("Agent: " .. command.type), -1)
  end
  if not ok then error(data) end
  return data
end

-- batch replays sub-commands through run_command, so it's defined here (after
-- run_command) and registered with the handlers below.
local function batch_result(index, command_type, succeeded, data)
  local result = { index = index, type = command_type, ok = succeeded }
  if succeeded then
    result.data = data
  else
    result.error = tostring(data)
  end
  return result
end

local function command_batch(command)
  local payload = command.payload or {}
  local commands = payload.commands or {}
  local results = {}
  reaper.Undo_BeginBlock()
  for i, sub in ipairs(commands) do
    local ok, data = pcall(run_command, sub, true)
    results[#results + 1] = batch_result(i, sub.type, ok, data)
    if not ok and payload.stop_on_error ~= false then
      reaper.Undo_EndBlock(command.undo_label or payload.undo_label or "Agent: batch failed", -1)
      local inner = error_code_from(data, "BATCH_FAILED")
      error(inner .. ": batch sub-command " .. i .. " failed: " .. tostring(data))
    end
  end
  reaper.Undo_EndBlock(command.undo_label or payload.undo_label or "Agent: batch", -1)
  return { results = results }
end

local function command_get_selected_track()
  local track = reaper.GetSelectedTrack(0, 0)
  if not track then error("NO_TARGET_TRACK: No track selected") end
  local _, name = reaper.GetTrackName(track)
  return { name = name, guid = reaper.GetTrackGUID(track) }
end

-- Read / context
handlers.get_selected_track = command_get_selected_track
handlers.get_context = command_get_context
handlers.get_fx_parameters = command_get_fx_parameters
handlers.scan_fx = command_scan_fx
handlers.enum_installed_fx = command_enum_installed_fx
handlers.discover_drum_map = command_discover_drum_map

-- Transport / project
handlers.play = command_play
handlers.stop = command_stop
handlers.pause = command_pause
handlers.record = command_record
handlers.set_cursor = command_set_cursor
handlers.set_time_selection = command_set_time_selection
handlers.set_tempo = command_set_tempo
handlers.render = command_render
handlers.capture_track_audio = command_capture_track_audio
handlers.get_track_routing = command_get_track_routing

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

-- Track state snapshot/restore (Post Mortem Verified Fix Preview, P2-001)
handlers.snapshot_track_state = command_snapshot_track_state
handlers.restore_track_state = command_restore_track_state

-- FX
handlers.add_fx = command_add_fx
handlers.add_fx_chain = command_add_fx_chain
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

-- Composition
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
      error = { code = error_code_from(data_or_error, "COMMAND_FAILED"), details = tostring(data_or_error) },
    }
  end
  atomic_write_json(join(paths.outbox, command.id .. ".json"), result)
end

local function write_heartbeat()
  atomic_write_json(paths.heartbeat, heartbeat_payload())
  write_lock("none")
end

-- Write the heartbeat at most every `heartbeat_interval` seconds, or
-- immediately when a command starts/finishes (in_flight_command changes)
-- or on the first tick. The old code wrote it 4×/second forever — ~345k
-- renames/day on a file nothing was reading that often.
-- (Defined above process_file so the in-flight marker actually reaches disk:
-- it used to be set and cleared strictly inside process_file while every
-- maybe_heartbeat call ran outside it, so no heartbeat ever carried it and a
-- 30s sampler load read as "bridge died".)
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

  command.id = safe_id(command.id, filename:gsub("%.json$", ""))
  if auth_token and command.token ~= auth_token then
    write_result(command, false, "AUTH_FAILED: missing or wrong token")
    move_file(processing_path, join(paths.failed, filename))
    return
  end
  in_flight_command = command.id
  maybe_heartbeat()  -- publish the marker BEFORE the (possibly long) command runs
  log_line("start " .. command.id .. " " .. tostring(command.type))
  local run_ok, data = pcall(run_command, command, false)
  -- write_result -> json.encode can throw (a value json can't encode); if it does,
  -- still emit a failure reply so the command never strands with no outbox.
  local wrote_ok, werr = pcall(write_result, command, run_ok, data)
  if not wrote_ok then
    pcall(write_result, command, false, "RESULT_ENCODE_FAILED: " .. tostring(werr))
  end
  log_line((run_ok and "ok " or "fail ") .. command.id .. " " .. tostring(command.type))

  local destination = run_ok and paths.archive or paths.failed
  move_file(processing_path, join(destination, filename))
  in_flight_command = nil
end

-- One-shot retention sweep at startup: bound logs/ and the archive|failed dirs
-- so a long-lived install never grows without limit. Runs once before the
-- loop, so zero per-tick cost. Log: rotate bridge.log -> bridge.log.1 past
-- 1 MB (one backup, max ~2 MB). Dirs: keep the 200 newest .json files; command
-- ids are timestamp-prefixed (manual-/agent-YYYY-MM-DDTHH-MM-SS-hex), so
-- list_json_files' lexical sort == chronological, and files[1] is the oldest.
-- outbox/ is DELIBERATELY excluded: it is the agent's reply queue, and a batch
-- dump of >200 commands would let a count-based sweep delete replies the agent
-- has not polled yet (send_command --wait would then time out on a command that
-- actually succeeded). The reader (reaperd.py) deletes its own reply after
-- reading it. ponytail: non-waited replies leak a few KB/session; add a
-- time-based TTL here only if that ever matters.
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
  -- Order by the embedded ISO stamp, not the whole filename: ids are prefixed
  -- "manual-"/"agent-" and "manual" > "agent" lexically, so a plain name sort
  -- groups by prefix and would delete the wrong (newer) files. files[1] must be
  -- the oldest for the slice below to drop the oldest.
  local function stamp(f) return f:match("(%d%d%d%d%-%d%d%-%d%dT%d%d%-%d%d%-%d%d)") or f end
  for _, dir in ipairs({ paths.archive, paths.failed }) do
    local files = list_json_files(dir)
    table.sort(files, function(a, b) return stamp(a) < stamp(b) end)
    if #files > keep then
      for i = 1, #files - keep do
        os.remove(join(dir, files[i]))
      end
    end
  end
end

local last_sweep = nil
-- Run the retention sweep periodically, not just at startup: REAPER stays open
-- for hours and the dirs would otherwise grow unbounded within one session.
local function maybe_sweep()
  local t = reaper.time_precise()
  if last_sweep == nil or t - last_sweep >= 300 then
    sweep_once()
    last_sweep = t
  end
end

-- One race-window after claiming the lock, confirm we still own it (M4). 0.75s
-- comfortably exceeds startup write-settling time and is well under the 5s
-- heartbeat interval, so two racing bridges read the same stable file and
-- exactly one sees its own token.
local LOCK_CONFIRM_DELAY = 0.75
-- Per-tick wall-clock budget for draining the inbox (M3). Checked AFTER each
-- command — never splits one — so a burst of heavy commands can't freeze the UI
-- thread; the remainder waits for the next tick. 50 stays as a hard backstop.
local DRAIN_BUDGET = 0.03

local function loop()
  local current = reaper.time_precise()

  if not lock_confirmed and current - lock_claim_t >= LOCK_CONFIRM_DELAY then
    local held = read_lock()
    if held and held.owner and held.owner ~= OWNER then
      log_line("BRIDGE_RACE_LOST: lock owned by " .. tostring(held.owner) .. "; stopping this bridge")
      return  -- stop deferring; the winning bridge keeps running
    end
    lock_confirmed = true
  end

  if current - last_poll >= poll_interval then
    last_poll = current
    local ok, err = pcall(function()
      maybe_heartbeat(false)
      local files = list_json_files(paths.inbox)
      local tick_start = reaper.time_precise()
      for i = 1, math.min(#files, 50) do
        process_file(files[i])
        maybe_heartbeat(false)  -- keep heartbeat/lock fresh mid-drain (self-throttles)
        if reaper.time_precise() - tick_start >= DRAIN_BUDGET then break end
      end
      maybe_sweep()
    end)
    if not ok then
      log_line("loop error " .. tostring(err))
      in_flight_command = nil
    end
  end
  reaper.defer(loop)
end

-- Startup requeue triage for files stranded in processing/ from a previous
-- run. Blind requeue had two honesty bugs: (a) a command that already ran
-- (reply written, but the processing->archive move failed) re-executes on
-- restart — double-apply; (b) a command whose CLI long ago reported TIMEOUT
-- silently applies later to whatever project is open then. Decision:
--   "skip"    reply or archive entry for this id exists: it already executed.
--   "discard" older than REQUEUE_MAX_AGE: too stale to trust; route to failed/.
--   "requeue" fresh crash mid-command: the undo block evaporated, safe to re-run.
-- Unknown age (no parseable created_at) requeues, matching the old behavior.
local REQUEUE_MAX_AGE = 15 * 60

local function parse_created_at(text)
  -- created_at is written by the CLI as local time with a zone suffix; the
  -- date/time components are local, so os.time() on them is comparable to
  -- os.time() now. Coarse is fine: the gate is minutes-scale.
  local y, mo, d, h, mi, s = tostring(text or ""):match(
    '"created_at"%s*:%s*"(%d+)%-(%d+)%-(%d+)T(%d+):(%d+):(%d+)')
  if not y then return nil end
  return os.time({ year = tonumber(y), month = tonumber(mo), day = tonumber(d),
                   hour = tonumber(h), min = tonumber(mi), sec = tonumber(s) })
end

local function requeue_decision(text, has_reply, has_archive, now_epoch)
  if has_reply or has_archive then return "skip" end
  local created = parse_created_at(text)
  if created and (now_epoch - created) > REQUEUE_MAX_AGE then return "discard" end
  return "requeue"
end

-- Self-check seam: test_bridge.lua loads this file with REAPER_BRIDGE_SELFTEST
-- set to exercise the pure/atomic helpers, then returns here before the defer
-- loop starts (no live REAPER needed). One global check; no production cost.
if _G.REAPER_BRIDGE_SELFTEST then
  return {
    parse_display_number = parse_display_number,
    atomic_write_json = atomic_write_json,
    split_lines = split_lines,
    splice_fx_chain = splice_fx_chain,
    parse_created_at = parse_created_at,
    requeue_decision = requeue_decision,
    error_code_from = error_code_from,
    safe_id = safe_id,
    lock_verdict = lock_verdict,
    ensure_render_autoclose = ensure_render_autoclose,
    restore_render_autoclose = restore_render_autoclose,
    fx_summary = fx_summary,
    batch_result = batch_result,
    capture_provenance = capture_provenance,
    snapshot_validate = snapshot_validate,
    restore_plan = restore_plan,
  }
end

-- One-shot triage of processing/ at startup (see requeue_decision above).
for _, filename in ipairs(list_json_files(paths.processing)) do
  local processing_path = join(paths.processing, filename)
  local text = read_file(processing_path)
  local id = safe_id(tostring(text or ""):match('"id"%s*:%s*"([^"]+)"'),
    filename:gsub("%.json$", ""))
  local action = requeue_decision(text,
    exists(join(paths.outbox, id .. ".json")),
    exists(join(paths.archive, filename)),
    os.time())
  if action == "requeue" then
    move_file(processing_path, join(paths.inbox, filename))
  elseif action == "skip" then
    log_line("requeue skip (already executed) " .. filename)
    move_file(processing_path, join(paths.archive, filename))
  else
    log_line("requeue discard (stale) " .. filename)
    pcall(write_result, { id = id, type = "requeue" }, false,
      "STALE_COMMAND: stranded in processing/ past the requeue age gate; not re-run")
    move_file(processing_path, join(paths.failed, filename))
  end
end

sweep_once()
last_sweep = reaper.time_precise()

log_line("bridge started")
loop()
