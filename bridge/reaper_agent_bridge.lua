-- @description Reaper Daemon (REAPER agent file bridge)
-- @version 3.13.0
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
--   3.13.0: Daemon Beater release. The bridge script itself is unchanged; the
--   version moves in lockstep with the repo release. In the cloned repo
--   (ReaPack installs only this bridge): the guitar stem profiler reads any
--   window of a song without analyzing the rest (--start-bar / --bars /
--   --max-seconds; hop-aligned, so timing, onset, grid, decay, and band
--   numbers match a full pass, with only the silence ratio scored against
--   a window-local loudness reference), the palm-mute vs open decay read
--   survives real tempos, the
--   analysis passes run about 2x faster, and the verify loop fails toward
--   honesty (every post-send rejection is exit 2 with the rejection code in
--   the JSON; malformed replies return structured errors).
--   3.12.0: Closed-loop verify release. The bridge script itself is unchanged
--   (the loop is a client-side sequencer of existing commands); the version
--   moves in lockstep with the repo release that adds `reaperd.py measure` /
--   `verify` (capture -> mutate -> capture with frozen bounds and measured
--   deltas) and the MCP `verify_change` / `tune_param` tools.
--   3.11.3: First-run captures temporarily enable REAPER 7.75+'s render-stats
--   retention bit as well as auto-close, preventing a modal from blocking the
--   bridge, then restore the user's exact render preference.
--   3.11.2: A fresh bridge heartbeat is authoritative when a packaged macOS
--   sidecar cannot see REAPER through the best-effort process probe.
--   3.11.1: The managed startup watchdog now reads the bridge's JSON lock,
--   respects long renders, and restarts only stale or missing instances. MCP
--   Track Check can return a verified first-run diagnosis to the panel.
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

-- The file reload_bridge compiles and re-runs. NEVER SCRIPT_PATH: under the
-- startup watchdog's dofile, get_action_context reports the LAUNCHER
-- (__startup.lua), and reloading that would re-run the whole managed block —
-- caught live by a dry_run naming the wrong file. SCRIPT_DIR is safe either
-- way because the launcher pins it via REAPER_AGENT_BRIDGE_DIR.
local BRIDGE_FILE = join(SCRIPT_DIR, "reaper_agent_bridge.lua")

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
-- Set by command_reload_bridge, acted on by the defer loop AFTER the command's
-- reply is written and its file archived — the old instance must finish its
-- bookkeeping before a new instance can own the inbox.
local reload_requested = false
-- Active preview (P2-002): mirrors state/preview.json so the heartbeat and the
-- expiry sweep don't re-read the file every tick. Startup recovery repopulates
-- consistency after a crash.
local active_preview = nil
local preview_recovered_at = nil
local last_poll = 0
local poll_interval = tonumber(config.poll_interval_seconds or 0.25)
local heartbeat_interval = 5
local last_heartbeat = nil
local last_in_flight = nil

local function now()
  return os.date("!%Y-%m-%dT%H:%M:%SZ")
end

-- Stamped once per load and carried in every heartbeat, so "did my new code
-- actually load" after reload_bridge is answered by watching this change —
-- alive_at alone cannot tell a reloaded bridge from the old one still ticking.
local LOADED_AT = now()

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

-- Visit every track FX, then every input FX (whose api_index carries REAPER's
-- 0x1000000 offset). fn(index, api_index, scope, name).
local function for_each_fx(track, fn)
  for i = 0, reaper.TrackFX_GetCount(track) - 1 do
    local _, name = reaper.TrackFX_GetFXName(track, i, "")
    fn(i, i, "track", name)
  end
  if reaper.TrackFX_GetRecCount then
    for i = 0, reaper.TrackFX_GetRecCount(track) - 1 do
      local api_index = 0x1000000 + i
      local _, name = reaper.TrackFX_GetFXName(track, api_index, "")
      fn(i, api_index, "input", name)
    end
  end
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
-- Displays that switch units partway up a range broke the formatted_value
-- search: Pro-C 3's Release reads "944.9 ms" low and "1.151 sec" high, so the
-- endpoints parsed as 10 and 2.5, the search decided the parameter ran
-- DESCENDING, and every real target was refused as out of range. Scale each
-- family to one base unit (Hz for frequency, ms for time) so the numbers are
-- comparable no matter which unit the plugin chose to print.
local UNIT_SCALE = {
  hz = 1, khz = 1000,
  ms = 1, msec = 1, msecs = 1,
  s = 1000, sec = 1000, secs = 1000, second = 1000, seconds = 1000,
  us = 0.001, usec = 0.001, ["\194\181s"] = 0.001,
}

local function parse_display_number(s)
  if type(s) ~= "string" then return nil end
  local low = s:lower()
  if low:find("inf", 1, true) then
    return low:find("-", 1, true) and -1e30 or 1e30
  end
  local first, last = low:find("[-+]?%d*%.?%d+")
  if not first then return nil end
  local num = tonumber(low:sub(first, last))
  if not num then return nil end
  -- Only a unit ATTACHED to this number counts. Matching "s" anywhere in the
  -- string would turn "Mid: 0 dB / Side: 0 dB" into seconds.
  local unit = low:sub(last + 1):match("^%s*([%a\194\181]+)")
  local scale = unit and UNIT_SCALE[unit]
  if scale then num = num * scale end
  return num
end

-- A parameter whose display goes non-numeric at ONE end is still searchable
-- over the rest of its range. Pro-Q 4's band Threshold reads "Auto" at
-- normalized 1.0, which used to make the whole parameter unreachable by
-- formatted_value even though 0.0 .. 0.998 is plain dB. Narrow the bracket to
-- the widest span whose ends both print a number.
--
-- `probe` is norm -> number|nil. Pure apart from that call, so the selftest
-- drives it with a table-backed fake instead of a live plugin. Returns
-- lo, hi, lo_num, hi_num, or nil when nothing in the range is numeric.
local function numeric_bracket(probe)
  local lo, hi = 0.0, 1.0
  local lo_num, hi_num = probe(lo), probe(hi)
  if lo_num and hi_num then return lo, hi, lo_num, hi_num end

  local anchor
  for _, n in ipairs({ 0.5, 0.25, 0.75, 0.1, 0.9 }) do
    if probe(n) then anchor = n; break end
  end
  if not anchor then return nil end

  -- Bisect the boundary between the dead end and the anchor. 12 steps puts it
  -- within 1/4096 of normalized, finer than any display resolves.
  local function edge(dead)
    local bad, good = dead, anchor
    for _ = 1, 12 do
      local mid = (bad + good) / 2
      if probe(mid) then good = mid else bad = mid end
    end
    return good, probe(good)
  end

  if not lo_num then lo, lo_num = edge(0.0) end
  if not hi_num then hi, hi_num = edge(1.0) end
  if not lo_num or not hi_num then return nil end
  return lo, hi, lo_num, hi_num
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
    loaded_at = LOADED_AT,
    bridge_version = 3,
    project_name = get_project_name(),
    in_flight_command = in_flight_command,
  }
  if active_preview then
    hb.active_preview_token = active_preview.preview_token
    hb.preview_expires_at = active_preview.expires_at
  end
  if preview_recovered_at then hb.preview_recovered_at = preview_recovered_at end
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
  local function track_row(track, index, is_master)
    local volume = reaper.GetMediaTrackInfo_Value(track, "D_VOL")
    local name = "MASTER"
    if not is_master then
      local _, n = reaper.GetTrackName(track, "")
      name = n
    end
    local info = {
      index = index,
      is_master = is_master or nil,
      guid = reaper.GetTrackGUID(track),
      name = name,
      selected = reaper.IsTrackSelected(track),
      muted = reaper.GetMediaTrackInfo_Value(track, "B_MUTE") == 1,
      soloed = reaper.GetMediaTrackInfo_Value(track, "I_SOLO") ~= 0,
      armed = not is_master and
        reaper.GetMediaTrackInfo_Value(track, "I_RECARM") == 1,
      volume = volume,
      volume_db = db_from_volume(volume),
      pan = reaper.GetMediaTrackInfo_Value(track, "D_PAN"),
      folder_depth = is_master and 0 or
        reaper.GetMediaTrackInfo_Value(track, "I_FOLDERDEPTH"),
      item_count = is_master and 0 or reaper.CountTrackMediaItems(track),
    }
    if include_fx then
      info.fx = {}
      info.input_fx = {}
      for_each_fx(track, function(fx, api_index, scope, fx_name)
        -- The master's 0x1000000 slots are monitoring FX, not input FX, so
        -- the documented shape keeps them out.
        if not (is_master and scope == "input") then
          local list = scope == "input" and info.input_fx or info.fx
          list[#list + 1] = { index = fx, api_index = api_index, scope = scope, name = fx_name }
        end
      end)
    end
    return info
  end

  local tracks = {}
  for i = 0, reaper.CountTracks(0) - 1 do
    tracks[#tracks + 1] = track_row(reaper.GetTrack(0, i), i + 1, false)
  end
  -- The master track is not in CountTracks/GetTrack, so add it explicitly or it
  -- is invisible to the documented get_context discovery call. index 0 + is_master
  -- match find_track's master convention.
  tracks[#tracks + 1] = track_row(reaper.GetMasterTrack(0), 0, true)
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
    -- PROJECT_SRATE is the rate STORED in the project and reads back whether or
    -- not it is in force (measured 2026-08-17: 44100 with the override off).
    -- When sample_rate_overridden is false REAPER runs at the audio device's
    -- rate instead, so the number alone does not say what is playing.
    sample_rate = reaper.GetSetProjectInfo(0, "PROJECT_SRATE", 0, false),
    sample_rate_overridden = reaper.GetSetProjectInfo(0, "PROJECT_SRATE_USE", 0, false) == 1,
    cursor = { seconds = cursor, bar = bar_from_time(cursor) },
    time_selection = get_time_selection(),
    transport = get_transport(),
    tracks = get_tracks(payload.include_fx ~= false),
    markers = markers,
    regions = regions,
    selected_track_count = reaper.CountSelectedTracks(0),
    selected_item_count = reaper.CountSelectedMediaItems(0),
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

  if payload.fx_guid then
    for_each_fx(track, function(fx, api_index, match_scope, fx_name)
      local wanted = match_scope == "input" and search_input_fx or
                     match_scope == "track" and search_track_fx
      if wanted and reaper.TrackFX_GetFXGUID(track, api_index) == payload.fx_guid then
        matches[#matches + 1] = {
          index = fx, api_index = api_index, scope = match_scope, name = fx_name,
        }
      end
    end)
    if #matches == 0 then error("NO_FX: No FX with guid " .. tostring(payload.fx_guid)) end
    if #matches > 1 then error("AMBIGUOUS_FX: Multiple FX have guid " .. tostring(payload.fx_guid)) end
    return track, track_index, matches[1].api_index, matches[1].name,
      matches[1].scope, matches[1].index
  end

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

  for_each_fx(track, function(fx, api_index, match_scope, fx_name)
    local wanted = match_scope == "input" and search_input_fx or
                   match_scope == "track" and search_track_fx
    if wanted and contains_ci(fx_name, needle) then
      matches[#matches + 1] = { index = fx, api_index = api_index, scope = match_scope, name = fx_name }
    end
  end)

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
    return reaper.TimeMap2_beatsToTime(0, beat_offset, math.max(0, whole_bar - 1))
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

-- MIDI notes inside a take
--
-- The bridge could place a MIDI item but never touch a note inside one, so
-- every velocity/articulation pass had to go through the REAPER UI. These two
-- commands close that: read the notes, then set velocities on named notes.
--
-- Notes are addressed by (start PPQ, pitch), never by take index. An index
-- moves the instant anything is inserted or deleted, so an index-addressed
-- edit written against a read from a minute ago can land on the wrong note
-- and look like it worked. A (ppq, pitch) pair either names the same note it
-- named at read time or names nothing at all, which is a failure we can see.

local function midi_take_for(payload)
  local track, track_index = find_track(payload)
  local count = reaper.CountTrackMediaItems(track)
  if count == 0 then
    error("NO_MEDIA_ITEM: target track has no media items")
  end
  local item
  if payload.item_index ~= nil then
    local idx = math.floor(tonumber(payload.item_index) or -1)
    if idx < 0 or idx >= count then
      error("NO_MEDIA_ITEM: item_index " .. idx .. " is out of range; the track has " .. count)
    end
    item = reaper.GetTrackMediaItem(track, idx)
  elseif count > 1 then
    error("AMBIGUOUS_ITEM: the track has " .. count .. " items; pass item_index")
  else
    item = reaper.GetTrackMediaItem(track, 0)
  end
  local take = reaper.GetActiveTake(item)
  if not take then error("NO_TAKE: the item has no active take") end
  if not reaper.TakeIsMIDI(take) then
    error("NOT_MIDI: the active take is audio, not MIDI")
  end
  return take, track, track_index, item
end

local function read_midi_notes(take)
  local _, note_count = reaper.MIDI_CountEvts(take)
  local notes = {}
  for i = 0, (note_count or 0) - 1 do
    local ok, selected, muted, startppq, endppq, chan, pitch, vel = reaper.MIDI_GetNote(take, i)
    if ok then
      notes[#notes + 1] = {
        index = i,
        ppq = math.floor(startppq + 0.5),
        end_ppq = math.floor(endppq + 0.5),
        pitch = pitch,
        velocity = vel,
        channel = chan,
        muted = muted and true or false,
        selected = selected and true or false,
      }
    end
  end
  return notes
end

-- Pure: parse the wire rows of a note edit. Rows arrive as arrays
-- ([ppq, pitch, ...extras]) or objects; `fields` names the extras in array
-- order. Shared by set_note_velocities and set_note_positions.
local note_edit = {}

function note_edit.parse_rows(rows, fields, shape_hint)
  if type(rows) ~= "table" or #rows == 0 then
    error("BAD_PAYLOAD: notes must be a non-empty array of " .. shape_hint)
  end
  local requested = {}
  for i, row in ipairs(rows) do
    if type(row) ~= "table" then
      error("BAD_PAYLOAD: notes[" .. i .. "] is neither an array nor an object")
    end
    local req
    if row.ppq ~= nil or row.pitch ~= nil then
      req = { ppq = math.floor(tonumber(row.ppq) or -1),
              pitch = math.floor(tonumber(row.pitch) or -1) }
      for _, f in ipairs(fields) do req[f] = row[f] end
    else
      req = { ppq = math.floor(tonumber(row[1]) or -1),
              pitch = math.floor(tonumber(row[2]) or -1) }
      for j, f in ipairs(fields) do req[f] = row[j + 2] end
    end
    requested[i] = req
  end
  return requested
end

-- Pure: resolve rows addressed by (ppq, pitch) against the notes a take
-- actually has. Every row lands in exactly one bucket, so the caller can refuse
-- the whole edit on any bucket but `plan` being non-empty.
--
-- Two notes stacked on the same pitch AND the same tick are reported as
-- ambiguous rather than picked between. Guessing there means writing onto a
-- note the caller never looked at.
--
-- `validate(req, where)` returns the plan entry's edit fields (annotating and
-- returning nil for an invalid row); `augment(entry, note)` copies whatever the
-- caller wants recorded from the resolved note.
function note_edit.resolve_rows(existing, requested, validate, augment)
  local by_key, duplicated = {}, {}
  for _, note in ipairs(existing) do
    local key = note.ppq .. ":" .. note.pitch
    if by_key[key] then duplicated[key] = true else by_key[key] = note end
  end
  local plan, missing, ambiguous, invalid = {}, {}, {}, {}
  local claimed = {}
  for row, req in ipairs(requested) do
    local key = tostring(req.ppq) .. ":" .. tostring(req.pitch)
    local where = { row = row, ppq = req.ppq, pitch = req.pitch }
    local fields = validate(req, where)
    if not fields then
      invalid[#invalid + 1] = where
    elseif duplicated[key] then
      where.reason = "two notes share this tick and pitch"
      ambiguous[#ambiguous + 1] = where
    elseif claimed[key] then
      where.reason = "this note was already addressed by row " .. claimed[key]
      ambiguous[#ambiguous + 1] = where
    elseif not by_key[key] then
      missing[#missing + 1] = where
    else
      claimed[key] = row
      local note = by_key[key]
      local entry = { index = note.index, ppq = note.ppq, pitch = note.pitch }
      for k, v in pairs(fields) do entry[k] = v end
      if augment then augment(entry, note) end
      plan[#plan + 1] = entry
    end
  end
  return { plan = plan, missing = missing, ambiguous = ambiguous,
           invalid = invalid, claimed = claimed }
end

local function velocity_plan(existing, requested)
  return note_edit.resolve_rows(existing, requested,
    function(req, where)
      local velocity = tonumber(req.velocity)
      velocity = velocity and math.floor(velocity) or nil
      if not velocity or velocity < 1 or velocity > 127 then
        where.velocity = req.velocity
        return nil
      end
      return { to = velocity }
    end,
    function(entry, note) entry.from = note.velocity end)
end

-- Pure: fold a note list into a per-pitch velocity summary. This is what makes
-- a 400-note edit readable in one line per drum instead of 400 rows.
local function velocity_summary(notes)
  local order, seen = {}, {}
  for _, note in ipairs(notes) do
    local bucket = seen[note.pitch]
    if not bucket then
      bucket = { pitch = note.pitch, count = 0, min = note.velocity, max = note.velocity, total = 0 }
      seen[note.pitch] = bucket
      order[#order + 1] = bucket
    end
    bucket.count = bucket.count + 1
    bucket.total = bucket.total + note.velocity
    if note.velocity < bucket.min then bucket.min = note.velocity end
    if note.velocity > bucket.max then bucket.max = note.velocity end
  end
  table.sort(order, function(a, b) return a.pitch < b.pitch end)
  for _, bucket in ipairs(order) do
    bucket.mean = bucket.total / bucket.count
    bucket.total = nil
  end
  return order
end

local function note_name_lookup(track, notes)
  local names = {}
  for _, note in ipairs(notes) do
    if names[note.pitch] == nil then
      local name = reaper.GetTrackMIDINoteNameEx(0, track, note.pitch, note.channel or 0)
      names[note.pitch] = name or false
    end
  end
  local out = {}
  for pitch, name in pairs(names) do
    if name then out[tostring(pitch)] = name end
  end
  return out
end

local function command_get_midi_notes(command)
  local payload = command.payload or {}
  local take, track, track_index, item = midi_take_for(payload)
  local notes = read_midi_notes(take)

  local wanted
  if type(payload.pitches) == "table" and #payload.pitches > 0 then
    wanted = {}
    for _, p in ipairs(payload.pitches) do wanted[math.floor(tonumber(p) or -1)] = true end
    local kept = {}
    for _, note in ipairs(notes) do
      if wanted[note.pitch] then kept[#kept + 1] = note end
    end
    notes = kept
  end

  local total = #notes
  local cap = math.floor(tonumber(payload.max_notes) or 4000)
  local truncated = false
  if cap > 0 and total > cap then
    local kept = {}
    for i = 1, cap do kept[i] = notes[i] end
    notes = kept
    truncated = true
  end

  -- Bar numbers are the thing a producer actually reads back, and they cost one
  -- API call per note, so they are opt-out rather than always-on.
  if payload.include_bars ~= false then
    for _, note in ipairs(notes) do
      note.bar = bar_from_time(reaper.MIDI_GetProjTimeFromPPQPos(take, note.ppq))
    end
  end

  local _, track_name = reaper.GetTrackName(track, "")
  local data = {
    track = { index = track_index, name = track_name },
    item = {
      start_seconds = reaper.GetMediaItemInfo_Value(item, "D_POSITION"),
      length_seconds = reaper.GetMediaItemInfo_Value(item, "D_LENGTH"),
    },
    ppq_per_quarter = math.floor(
      reaper.MIDI_GetPPQPosFromProjQN(take, 1) - reaper.MIDI_GetPPQPosFromProjQN(take, 0) + 0.5),
    note_count = total,
    returned = #notes,
    truncated = truncated,
    notes = notes,
    velocity_summary = velocity_summary(notes),
  }
  if payload.include_note_names ~= false then
    data.note_names = note_name_lookup(track, notes)
  end
  return data
end

local function command_set_note_velocities(command)
  return note_edit.apply(command, {
    fields = { "velocity" },
    shape_hint = "[ppq, pitch, velocity]",
    plan = velocity_plan,
    noun = "velocity",
    unconfirmed_code = "VELOCITY_WRITE_UNCONFIRMED",
    bucket_text = function(resolved)
      return #resolved.missing .. " missing, " .. #resolved.ambiguous ..
        " ambiguous, " .. #resolved.invalid .. " outside 1-127"
    end,
    write = function(take, hit)
      reaper.MIDI_SetNote(take, hit.index, nil, nil, nil, nil, nil, nil, hit.to, true)
    end,
    confirm = function(by_key, hit)
      local note = by_key[hit.ppq .. ":" .. hit.pitch]
      if note and note.velocity == hit.to then return true end
      return false, { ppq = hit.ppq, pitch = hit.pitch, wanted = hit.to,
                      got = note and note.velocity or nil }
    end,
    finish = function(result, resolved, before, after)
      result.velocity_before = velocity_summary(before)
      result.velocity_after = velocity_summary(after)
    end,
  })
end

-- Pure: resolve requested (ppq, pitch -> new_ppq, new_end_ppq) rows against the
-- notes a take actually has. Mirrors velocity_plan's bucketing so a caller can
-- refuse the whole edit on any bucket but `plan` being non-empty.
--
-- Moving notes carries a hazard velocity writes do not: the edit changes the
-- very key notes are addressed by. Two hits nudged onto the same tick and pitch
-- would leave a take that no later read can address unambiguously, so
-- destination collisions are refused here rather than discovered next session.
local function position_plan(existing, requested)
  local resolved = note_edit.resolve_rows(existing, requested,
    function(req, where)
      local new_ppq = tonumber(req.new_ppq)
      local new_end = tonumber(req.new_end_ppq)
      new_ppq = new_ppq and math.floor(new_ppq + 0.5) or nil
      new_end = new_end and math.floor(new_end + 0.5) or nil
      if not new_ppq or not new_end or new_ppq < 0 or new_end <= new_ppq then
        where.new_ppq = req.new_ppq
        where.new_end_ppq = req.new_end_ppq
        where.reason = "new_ppq must be >= 0 and new_end_ppq must be greater than it"
        return nil
      end
      return { to_ppq = new_ppq, to_end_ppq = new_end }
    end,
    function(entry, note) entry.end_ppq = note.end_ppq end)
  local plan, claimed = resolved.plan, resolved.claimed
  local collisions = {}

  -- Where does every note in the take end up? Moved notes land on their
  -- requested tick; notes no row addressed stay exactly where they are.
  local landing = {}
  for _, hit in ipairs(plan) do
    local dest = hit.to_ppq .. ":" .. hit.pitch
    if landing[dest] then
      collisions[#collisions + 1] = {
        ppq = hit.ppq, pitch = hit.pitch, new_ppq = hit.to_ppq,
        reason = "another moved note lands on this tick and pitch",
      }
    else
      landing[dest] = true
    end
  end
  -- Unclaimed notes are checked against moved destinations only, and are never
  -- entered into `landing` themselves: two untouched notes already stacked on
  -- one tick and pitch are a pre-existing condition this request neither
  -- created nor worsened, and must not veto an edit that leaves them alone.
  for _, note in ipairs(existing) do
    local src = note.ppq .. ":" .. note.pitch
    if not claimed[src] and landing[src] then
      collisions[#collisions + 1] = {
        ppq = note.ppq, pitch = note.pitch, new_ppq = note.ppq,
        reason = "a moved note lands on this note, which the request left in place",
      }
    end
  end

  resolved.collisions = collisions
  return resolved
end

-- Shared scaffold for the two note-edit commands: parse rows, resolve them,
-- refuse all-or-nothing, write with noSort (MIDI_Sort renumbers notes, so
-- sorting inside the loop would invalidate the indices the plan holds), then
-- read the take back and confirm every note -- MIDI_SetNote returning true is
-- a claim about the call, not about the note. `spec` supplies what differs:
-- the row fields, the plan fn, the write, the confirm, and the result extras.
function note_edit.apply(command, spec)
  local payload = command.payload or {}
  local requested = note_edit.parse_rows(payload.notes, spec.fields, spec.shape_hint)

  local take, track, track_index = midi_take_for(payload)
  local before = read_midi_notes(take)
  local resolved = spec.plan(before, requested)
  local unresolved = #resolved.missing + #resolved.ambiguous + #resolved.invalid
    + (resolved.collisions and #resolved.collisions or 0)

  -- Default is all-or-nothing. A velocity or timing pass is one musical
  -- gesture: half of it applied is worse than none of it, because the half
  -- that landed is indistinguishable by ear from the half that did not.
  if unresolved > 0 and payload.allow_partial ~= true then
    error("NOTE_MATCH_FAILED: " .. unresolved .. " of " .. #requested ..
      " requested notes did not resolve (" .. spec.bucket_text(resolved) ..
      "); nothing was changed. The take holds " .. #before ..
      " notes. Re-read it with get_midi_notes and rebuild the request: the " ..
      "MIDI moved after the read this request was built from.")
  end
  if #resolved.plan == 0 then
    error("NOTE_MATCH_FAILED: no requested note resolved; nothing was changed")
  end

  for _, hit in ipairs(resolved.plan) do
    spec.write(take, hit)
  end
  reaper.MIDI_Sort(take)
  reaper.UpdateArrange()

  local after = read_midi_notes(take)
  local by_key = {}
  for _, note in ipairs(after) do by_key[note.ppq .. ":" .. note.pitch] = note end
  local confirmed, wrong = 0, {}
  for _, hit in ipairs(resolved.plan) do
    local ok, wrong_entry = spec.confirm(by_key, hit)
    if ok then
      confirmed = confirmed + 1
    else
      wrong[#wrong + 1] = wrong_entry
    end
  end
  if #wrong > 0 then
    error(spec.unconfirmed_code .. ": " .. #wrong .. " of " .. #resolved.plan ..
      " notes did not read back at the requested " .. spec.noun ..
      ". The edit is inside one undo block; one REAPER undo reverts it. " ..
      "Mismatches: " .. json.encode(wrong))
  end

  local _, track_name = reaper.GetTrackName(track, "")
  local result = {
    track = { index = track_index, name = track_name },
    requested = #requested,
    applied = #resolved.plan,
    confirmed = confirmed,
    unresolved = unresolved,
    missing = resolved.missing,
    ambiguous = resolved.ambiguous,
    invalid = resolved.invalid,
    verified_note = "Every applied note was re-read from the take and matched " ..
      "the requested " .. spec.noun .. ".",
  }
  spec.finish(result, resolved, before, after)
  return result
end

local function command_set_note_positions(command)
  return note_edit.apply(command, {
    fields = { "new_ppq", "new_end_ppq" },
    shape_hint = "[ppq, pitch, new_ppq, new_end_ppq]",
    plan = position_plan,
    noun = "position",
    unconfirmed_code = "POSITION_WRITE_UNCONFIRMED",
    bucket_text = function(resolved)
      return #resolved.missing .. " missing, " .. #resolved.ambiguous ..
        " ambiguous, " .. #resolved.invalid .. " invalid, " ..
        #resolved.collisions .. " colliding at their destination"
    end,
    write = function(take, hit)
      reaper.MIDI_SetNote(take, hit.index, nil, nil, hit.to_ppq, hit.to_end_ppq,
                          nil, nil, nil, true)
    end,
    -- Confirm each note at its NEW key: the move rewrote the address.
    confirm = function(by_key, hit)
      local note = by_key[hit.to_ppq .. ":" .. hit.pitch]
      if note and note.ppq == hit.to_ppq and note.end_ppq == hit.to_end_ppq then
        return true
      end
      return false, { from_ppq = hit.ppq, pitch = hit.pitch,
                      wanted_ppq = hit.to_ppq,
                      got_ppq = note and note.ppq or nil,
                      wanted_end_ppq = hit.to_end_ppq,
                      got_end_ppq = note and note.end_ppq or nil }
    end,
    finish = function(result, resolved)
      local moved_max, moved_total = 0, 0
      for _, hit in ipairs(resolved.plan) do
        local d = math.abs(hit.to_ppq - hit.ppq)
        if d > moved_max then moved_max = d end
        moved_total = moved_total + d
      end
      result.collisions = resolved.collisions
      result.max_move_ticks = moved_max
      result.mean_move_ticks = moved_total / #resolved.plan
    end,
  })
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

local function set_fx_param_result(track, track_index, track_name, api_index,
                                   display_fx_index, fx_scope, fx_name,
                                   before, after)
  return {
    track = { index = track_index, name = track_name,
              guid = reaper.GetTrackGUID(track) },
    -- Mutations need the same stable identity as discovery. The Daemon
    -- Console uses this GUID to prove it is restoring the exact FX that the
    -- bridge changed, even if an audition bypass added a newer undo point.
    fx = fx_summary(track, api_index, display_fx_index, fx_scope, fx_name),
    parameter = { before = before, after = after },
  }
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
      local lo, hi, lo_num, hi_num = numeric_bracket(fmt_num)
      if lo == nil then
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
  return set_fx_param_result(track, track_index, track_name, api_index,
    display_fx_index, fx_scope, fx_name, before, after)
end

local AUTOMATION_MODE_NAMES = {
  [0] = "trim/read", [1] = "read", [2] = "touch", [3] = "write", [4] = "latch",
}

-- command_write_fx_param_automation now lives on the automation table (see
-- automation.write_command below): the transactional engine it shares with
-- apply_automation_transaction is defined after the table.

-- Independent, read-only FX-envelope evidence. Keep the helpers behind one
-- table: the bridge main chunk is close to Lua's 200-local limit.
local automation = {}

function automation.hash_text(text)
  -- FNV-1a/32 is not a security primitive; it is a compact change detector for
  -- canonical envelope rows. Prefix the algorithm so a later upgrade cannot
  -- be mistaken for the same hash surface.
  local hash = 2166136261
  for i = 1, #text do
    hash = ((hash ~ text:byte(i)) * 16777619) & 0xffffffff
  end
  return "fnv1a32:" .. string.format("%08x", hash)
end

function automation.canonicalize(points, time_to_qn)
  local response_points, canonical_rows, by_time = {}, {}, {}
  for i, source in ipairs(points or {}) do
    local point = {}
    for key, value in pairs(source) do point[key] = value end
    point.enumeration_order = tonumber(point.enumeration_order) or i
    point.qn_tick = math.floor((time_to_qn(point.time) * 960) + 0.5)
    point.value_tick = math.floor((point.value * 1000000) + 0.5)
    response_points[#response_points + 1] = point
    local item_id = point.automation_item_id or "underlying"
    canonical_rows[#canonical_rows + 1] = table.concat({
      tostring(point.qn_tick), tostring(point.value_tick),
      tostring(point.shape), string.format("%.17g", point.tension or 0),
      point.selected and "1" or "0", tostring(item_id),
    }, "|")
    local time_key = string.format("%.17g", point.time)
    local group = by_time[time_key]
    if not group then
      group = { time = point.time, enumeration_order = {} }
      by_time[time_key] = group
    end
    group.enumeration_order[#group.enumeration_order + 1] = point.enumeration_order
  end

  -- Response order is chronological and preserves REAPER enumeration order at
  -- duplicate times. The hash is tuple-sorted, so REAPER reordering two equal-
  -- time points does not manufacture a false content change.
  table.sort(response_points, function(a, b)
    if a.time ~= b.time then return a.time < b.time end
    return a.enumeration_order < b.enumeration_order
  end)
  table.sort(canonical_rows)

  local duplicates = {}
  for _, group in pairs(by_time) do
    if #group.enumeration_order > 1 then
      duplicates[#duplicates + 1] = group
    end
  end
  table.sort(duplicates, function(a, b) return a.time < b.time end)
  return response_points, automation.hash_text(table.concat(canonical_rows, "\n")), duplicates
end

function automation.select_range(points, start_time, end_time, include_neighbors)
  local inside, before, after = {}, nil, nil
  -- Marker/bar conversion and envelope insertion can represent the same
  -- musical position a few nanoseconds apart. Treat sub-microsecond drift as
  -- equality or an exact marker-end reset falls outside an "inclusive" range.
  local boundary_epsilon = 1e-7
  for _, point in ipairs(points or {}) do
    local t = point.time
    if (start_time == nil or t >= start_time - boundary_epsilon)
       and (end_time == nil or t <= end_time + boundary_epsilon) then
      inside[#inside + 1] = point
    elseif include_neighbors and start_time ~= nil and t < start_time - boundary_epsilon
           and (not before or t > before.time
                or (t == before.time and point.enumeration_order > before.enumeration_order)) then
      before = point
    elseif include_neighbors and end_time ~= nil and t > end_time + boundary_epsilon
           and (not after or t < after.time
                or (t == after.time and point.enumeration_order < after.enumeration_order)) then
      after = point
    end
  end
  return inside, before, after
end

-- Phase 2: transactional writes. One snapshot before any mutation, inclusive
-- range replacement, whole-envelope reread verification, and automatic
-- preimage restore on any failure. All functions live on the table so the
-- main chunk's local budget is untouched.

function automation.underlying_points(envelope)
  local points = {}
  local count = reaper.CountEnvelopePointsEx(envelope, -1)
  for i = 0, count - 1 do
    local ok, time, value, shape, tension, selected =
      reaper.GetEnvelopePointEx(envelope, -1, i)
    if ok then
      points[#points + 1] = {
        index = i, time = time, value = value, shape = shape,
        tension = tension or 0, selected = selected == true,
      }
    end
  end
  return points
end

-- Snapshot with create=false: observing an absent envelope must never be the
-- thing that creates it.
function automation.snapshot(track, api_index, param_index)
  local envelope = reaper.GetFXEnvelope(track, api_index, param_index, false)
  local snap = {
    track = track, api_index = api_index, param_index = param_index,
    envelope = envelope, existed = envelope ~= nil,
    points = {}, state = nil, automation_items = {},
    automation_mode = math.floor(reaper.GetMediaTrackInfo_Value(track, "I_AUTOMODE")),
  }
  if not envelope then return snap end
  snap.points = automation.underlying_points(envelope)
  local state = {}
  for _, key in ipairs({ "B_ACTIVE", "B_VISIBLE", "B_ARM", "I_TCPH" }) do
    local ok, value = pcall(reaper.GetEnvelopeInfo_Value, envelope, key)
    state[key] = ok and finite_or_nil(value) or nil
  end
  snap.state = state
  local item_count = reaper.CountAutomationItems(envelope)
  for item_index = 0, item_count - 1 do
    snap.automation_items[#snap.automation_items + 1] = {
      position = finite_or_nil(reaper.GetSetAutomationItemInfo(
        envelope, item_index, "D_POSITION", 0, false)),
      length = finite_or_nil(reaper.GetSetAutomationItemInfo(
        envelope, item_index, "D_LENGTH", 0, false)),
    }
  end
  return snap
end

function automation.restore(snap, envelope)
  envelope = envelope or snap.envelope
  local note = nil
  if envelope then
    local count = reaper.CountEnvelopePointsEx(envelope, -1)
    for i = count - 1, 0, -1 do reaper.DeleteEnvelopePointEx(envelope, -1, i) end
    if snap.existed then
      for _, p in ipairs(snap.points) do
        reaper.InsertEnvelopePointEx(envelope, -1, p.time, p.value, p.shape,
          p.tension or 0, p.selected, true)
      end
      reaper.Envelope_SortPoints(envelope)
      if snap.state then
        for key, value in pairs(snap.state) do
          if value ~= nil then pcall(reaper.SetEnvelopeInfo_Value, envelope, key, value) end
        end
      end
    else
      -- ReaScript has no delete-envelope call, so an envelope this transaction
      -- created is restored to empty/hidden/unarmed; because creation sits
      -- inside the transaction's undo block, one REAPER undo removes it.
      pcall(reaper.SetEnvelopeInfo_Value, envelope, "B_VISIBLE", 0)
      pcall(reaper.SetEnvelopeInfo_Value, envelope, "B_ARM", 0)
      note = "envelope created by this transaction was emptied and hidden; one REAPER undo deletes it"
    end
  end
  pcall(reaper.SetMediaTrackInfo_Value, snap.track, "I_AUTOMODE", snap.automation_mode)
  return note
end

function automation.restore_verified(snap, envelope, time_to_qn)
  if not snap.existed then
    if not envelope then return true end
    return #automation.underlying_points(envelope) == 0
  end
  if not envelope then return false end
  local _, before_hash = automation.canonicalize(snap.points, time_to_qn)
  local _, after_hash =
    automation.canonicalize(automation.underlying_points(envelope), time_to_qn)
  return before_hash == after_hash
end

function automation.normalize_points(write)
  local points = write.points or {}
  if #points == 0 then error("NO_POINTS: Provide at least one automation point") end
  local seen = {}
  for i, point in ipairs(points) do
    local t = time_from_point(point)
    local value = tonumber(point.value)
    local tension = tonumber(point.tension or 0) or 0
    if t ~= t or t == math.huge or t == -math.huge then
      error("BAD_POINT_TIME: point " .. i .. " time is not finite")
    end
    if value == nil or value ~= value or value == math.huge or value == -math.huge
       or value < 0 or value > 1 then
      error("BAD_POINT_VALUE: point " .. i .. " value must be a finite number in 0..1")
    end
    if tension ~= tension or tension == math.huge or tension == -math.huge then
      error("BAD_POINT_VALUE: point " .. i .. " tension is not finite")
    end
    local key = string.format("%.17g", t)
    if seen[key] then error("DUPLICATE_POINT_TIME: two points at t=" .. key) end
    seen[key] = true
    points[i] = {
      time = t, value = value, shape = envelope_shape(point.shape),
      tension = tension, selected = point.selected == true, source = point,
    }
  end
  return points
end

-- A declared range always means replacement (inclusive both ends, 1e-7
-- boundary tolerance, matching get_fx_param_automation). No declared range
-- and no clear flag means pure append: nothing is deleted.
function automation.normalize_scope(write, points)
  local ranges = {}
  local function add_range(s, e)
    if s == nil then return end
    if e == nil or e < s then e = s end
    ranges[#ranges + 1] = { start_time = s, end_time = e }
  end
  if write.ranges then
    for _, r in ipairs(write.ranges) do
      local s, e = tonumber(r.start_time), tonumber(r.end_time)
      if not s or not e or s ~= s or e ~= e or e < s
         or s == math.huge or e == math.huge then
        error("BAD_AUTOMATION_RANGE: each range needs finite start_time <= end_time")
      end
      ranges[#ranges + 1] = { start_time = s, end_time = e }
    end
  elseif write.range then
    add_range(resolve_position(write.range))
  elseif write.position then
    add_range(resolve_position(write.position))
  end
  if #ranges == 0 and write.clear_existing_in_range then
    -- legacy inference: replace across the extent of the submitted points
    local min_t, max_t = nil, nil
    for _, p in ipairs(points) do
      if not min_t or p.time < min_t then min_t = p.time end
      if not max_t or p.time > max_t then max_t = p.time end
    end
    add_range(min_t, max_t)
  end
  return { ranges = ranges, append = #ranges == 0 }
end

function automation.point_in_scope(scope, t)
  for _, r in ipairs(scope.ranges) do
    if t >= r.start_time - 1e-7 and t <= r.end_time + 1e-7 then return true end
  end
  return false
end

function automation.scope_hits_item(snap, scope, points)
  if #snap.automation_items == 0 then return nil end
  local spans = {}
  for _, r in ipairs(scope.ranges) do spans[#spans + 1] = r end
  if scope.append then
    -- undeclared scope: the submitted points themselves are the footprint
    local min_t, max_t = nil, nil
    for _, p in ipairs(points) do
      if not min_t or p.time < min_t then min_t = p.time end
      if not max_t or p.time > max_t then max_t = p.time end
    end
    if min_t then spans[#spans + 1] = { start_time = min_t, end_time = max_t } end
  end
  for _, item in ipairs(snap.automation_items) do
    for _, span in ipairs(spans) do
      if item.position ~= nil and item.length ~= nil
         and span.start_time < item.position + item.length + 1e-7
         and span.end_time > item.position - 1e-7 then
        return item
      end
    end
  end
  return nil
end

function automation.point_key(point, time_to_qn)
  local qn_tick = math.floor((time_to_qn(point.time) * 960) + 0.5)
  local value_tick = math.floor((point.value * 1000000) + 0.5)
  return table.concat({ qn_tick, value_tick, point.shape,
    string.format("%.17g", point.tension or 0),
    point.selected and "1" or "0" }, "|")
end

function automation.prepare_write(write)
  if type(write) ~= "table" then error("BAD_PAYLOAD: each write must be an object") end
  local track, track_index, api_index, fx_name, fx_scope, display_fx_index =
    find_fx(write)
  local param_index, param_info = find_fx_param(track, api_index, write)
  local points = automation.normalize_points(write)
  local scope = automation.normalize_scope(write, points)
  if not scope.append then
    for i, p in ipairs(points) do
      if not automation.point_in_scope(scope, p.time) then
        error(("POINT_OUTSIDE_RANGE: point %d at %.9f is outside every declared range")
          :format(i, p.time))
      end
    end
  end
  local snap = automation.snapshot(track, api_index, param_index)
  local hit = automation.scope_hits_item(snap, scope, points)
  if hit then
    error("AUTOMATION_ITEM_UNSUPPORTED: write intersects automation item "
      .. tostring(hit.position) .. ".." .. tostring((hit.position or 0) + (hit.length or 0)))
  end
  return {
    track = track, track_index = track_index, api_index = api_index,
    fx_name = fx_name, fx_scope = fx_scope, display_fx_index = display_fx_index,
    param_index = param_index, param_info = param_info,
    points = points, scope = scope, snap = snap,
    envelope = snap.envelope, created = false, mutated = false,
  }
end

function automation.apply_prepared(prepared, show)
  local envelope = prepared.envelope
  if not envelope then
    envelope = reaper.GetFXEnvelope(prepared.track, prepared.api_index,
      prepared.param_index, true)
    if not envelope then error("NO_ENVELOPE: Could not create FX envelope") end
    prepared.envelope = envelope
    prepared.created = true
  end
  prepared.mutated = true
  local time_to_qn = function(t) return reaper.TimeMap2_timeToQN(0, t) end

  -- Replacement: delete preexisting in-scope points, except those the request
  -- reproduces exactly (idempotent re-apply leaves them untouched).
  local requested, skip_keys = {}, {}
  for _, p in ipairs(prepared.points) do
    requested[automation.point_key(p, time_to_qn)] = true
  end
  local delete_indices = {}
  if not prepared.scope.append then
    local existing = automation.underlying_points(envelope)
    for _, p in ipairs(existing) do
      if automation.point_in_scope(prepared.scope, p.time) then
        local key = automation.point_key(p, time_to_qn)
        if requested[key] then
          skip_keys[key] = true
        else
          delete_indices[#delete_indices + 1] = p.index
        end
      end
    end
    table.sort(delete_indices)
    for i = #delete_indices, 1, -1 do
      reaper.DeleteEnvelopePointEx(envelope, -1, delete_indices[i])
    end
  end
  prepared.replaced = #delete_indices
  prepared.deleted_lookup = {}
  for _, index in ipairs(delete_indices) do prepared.deleted_lookup[index] = true end

  local inserted, skipped = 0, 0
  for _, p in ipairs(prepared.points) do
    local key = automation.point_key(p, time_to_qn)
    if skip_keys[key] then
      skipped = skipped + 1
    else
      local ok = reaper.InsertEnvelopePointEx(envelope, -1, p.time, p.value,
        p.shape, p.tension, p.selected, true)
      if not ok then
        error("INSERT_FAILED: point at " .. string.format("%.9f", p.time))
      end
      inserted = inserted + 1
    end
  end
  prepared.skip_keys = skip_keys
  prepared.inserted, prepared.skipped = inserted, skipped
  reaper.Envelope_SortPoints(envelope)
  if show ~= false then
    pcall(reaper.SetEnvelopeInfo_Value, envelope, "B_VISIBLE", 1)
    pcall(reaper.SetEnvelopeInfo_Value, envelope, "B_ARM", 1)
    pcall(reaper.SetEnvelopeInfo_Value, envelope, "I_TCPH", 80)
  end
end

-- Reread proof: every submitted point must exist exactly, and the whole
-- envelope must equal the intended final state (kept preimage + new points),
-- which simultaneously proves outside-range preservation.
function automation.verify_prepared(prepared)
  local time_to_qn = function(t) return reaper.TimeMap2_timeToQN(0, t) end
  local live = automation.underlying_points(prepared.envelope)
  local live_counts, need_counts = {}, {}
  for _, p in ipairs(live) do
    local key = automation.point_key(p, time_to_qn)
    live_counts[key] = (live_counts[key] or 0) + 1
  end
  for _, p in ipairs(prepared.points) do
    local key = automation.point_key(p, time_to_qn)
    need_counts[key] = (need_counts[key] or 0) + 1
  end
  local confirmed, first_missing = 0, nil
  for key, count in pairs(need_counts) do
    local have = live_counts[key] or 0
    confirmed = confirmed + math.min(have, count)
    if have < count then first_missing = first_missing or key end
  end
  if first_missing then
    error("AUTOMATION_WRITE_UNCONFIRMED: point [" .. first_missing
      .. "] did not reread back from the envelope")
  end
  local intended = {}
  for _, p in ipairs(prepared.snap.points) do
    if not prepared.deleted_lookup[p.index] then intended[#intended + 1] = p end
  end
  for _, p in ipairs(prepared.points) do
    if not prepared.skip_keys[automation.point_key(p, time_to_qn)] then
      intended[#intended + 1] = p
    end
  end
  local _, intended_hash = automation.canonicalize(intended, time_to_qn)
  local _, live_hash = automation.canonicalize(live, time_to_qn)
  if intended_hash ~= live_hash then
    error("AUTOMATION_WRITE_UNCONFIRMED: envelope reread hash "
      .. live_hash .. " != intended " .. intended_hash)
  end
  prepared.verification = {
    requested = #prepared.points,
    replaced = prepared.replaced or 0,
    inserted = prepared.inserted or 0,
    skipped = prepared.skipped or 0,
    confirmed = confirmed,
    refused = 0,
  }
  prepared.final_hash = live_hash
end

-- Runs an all-or-nothing automation transaction. Any failure restores every
-- mutated envelope and rereads the restoration before reporting, so a
-- rolled-back reply is provable, not assumed.
function automation.execute(writes, opts)
  opts = opts or {}
  local prepared = {}
  for i, write in ipairs(writes) do prepared[i] = automation.prepare_write(write) end

  local seen = {}
  for i, p in ipairs(prepared) do
    local key = tostring(reaper.GetTrackGUID(p.track)) .. "/"
      .. tostring(reaper.TrackFX_GetFXGUID(p.track, p.api_index)) .. "/"
      .. tostring(p.param_index)
    if seen[key] then
      error("BAD_PAYLOAD: writes " .. seen[key] .. " and " .. i
        .. " target the same envelope; merge their points into one write")
    end
    seen[key] = i
  end

  local time_to_qn = function(t) return reaper.TimeMap2_timeToQN(0, t) end
  local function rollback(cause)
    local restored, notes = 0, {}
    for i = #prepared, 1, -1 do
      local p = prepared[i]
      if p.mutated then
        local note = automation.restore(p.snap, p.envelope)
        restored = restored + 1
        if note then notes[#notes + 1] = note end
        if not automation.restore_verified(p.snap, p.envelope, time_to_qn) then
          error("ROLLBACK_UNCONFIRMED: " .. tostring(cause) .. "; envelope "
            .. i .. " did not reread as its preimage; one REAPER undo reverts the transaction")
        end
      end
    end
    local extra = #notes > 0 and ("; " .. table.concat(notes, "; ")) or ""
    error("AUTOMATION_ROLLED_BACK: " .. tostring(cause) .. "; restored and reread "
      .. restored .. " envelope(s)" .. extra)
  end

  for _, p in ipairs(prepared) do
    local ok, err = pcall(automation.apply_prepared, p, opts.show ~= false)
    if not ok then rollback(err) end
    local ok2, err2 = pcall(automation.verify_prepared, p)
    if not ok2 then rollback(err2) end
  end

  local summaries = {}
  for i, p in ipairs(prepared) do
    local _, track_name = reaper.GetTrackName(p.track, "")
    local automode = math.floor(reaper.GetMediaTrackInfo_Value(p.track, "I_AUTOMODE"))
    summaries[i] = {
      track = { index = p.track_index, name = track_name, guid = reaper.GetTrackGUID(p.track) },
      fx = fx_summary(p.track, p.api_index, p.display_fx_index or p.api_index,
        p.fx_scope or "track", p.fx_name),
      parameter = p.param_info,
      envelope = automation.envelope_state(p.envelope),
      automation_mode = AUTOMATION_MODE_NAMES[automode] or tostring(automode),
      scope = #p.scope.ranges > 0 and p.scope.ranges or { append = true },
      verification = p.verification,
      final_hash = p.final_hash,
    }
  end
  return { writes = summaries }, prepared
end

function automation.envelope_state(envelope)
  if not envelope then return { exists = false } end
  local _, name = reaper.GetEnvelopeName(envelope, "")
  local function value(key)
    local ok, v = pcall(reaper.GetEnvelopeInfo_Value, envelope, key)
    return ok and finite_or_nil(v) or nil
  end
  return {
    exists = true, name = name,
    active = value("B_ACTIVE") == 1,
    visible = value("B_VISIBLE") == 1,
    armed = value("B_ARM") == 1,
    lane_height = value("I_TCPH"),
  }
end

function automation.read_command(command)
  local payload = command.payload or {}
  local track, track_index, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
  local param_index, param_info = find_fx_param(track, api_index, payload)
  local start_time = payload.start_time ~= nil and tonumber(payload.start_time) or nil
  local end_time = payload.end_time ~= nil and tonumber(payload.end_time) or nil
  if (payload.start_time ~= nil and start_time == nil)
     or (payload.end_time ~= nil and end_time == nil) then
    error("BAD_AUTOMATION_RANGE: start_time and end_time must be numbers")
  end
  if (start_time == nil) ~= (end_time == nil) then
    error("BAD_AUTOMATION_RANGE: provide both start_time and end_time, or neither")
  end
  if start_time and (start_time ~= start_time or end_time ~= end_time
      or start_time == math.huge or start_time == -math.huge
      or end_time == math.huge or end_time == -math.huge
      or end_time < start_time) then
    error("BAD_AUTOMATION_RANGE: range must be finite and end_time >= start_time")
  end

  -- false is load-bearing: this command must never create an envelope merely
  -- by observing it.
  local envelope = reaper.GetFXEnvelope(track, api_index, param_index, false)
  local all_points, automation_items = {}, {}
  local envelope_state = { exists = envelope ~= nil }
  local enumeration_order = 0

  if envelope then
    local _, envelope_name = reaper.GetEnvelopeName(envelope, "")
    local function env_value(key)
      local ok, value = pcall(reaper.GetEnvelopeInfo_Value, envelope, key)
      return ok and finite_or_nil(value) or nil
    end
    envelope_state.name = envelope_name
    envelope_state.active = env_value("B_ACTIVE") == 1
    envelope_state.visible = env_value("B_VISIBLE") == 1
    envelope_state.armed = env_value("B_ARM") == 1
    envelope_state.lane_height = env_value("I_TCPH")

    local function read_points(autoitem_index, item_id)
      local count = reaper.CountEnvelopePointsEx(envelope, autoitem_index)
      for point_index = 0, count - 1 do
        local ok, time, value, shape, tension, selected =
          reaper.GetEnvelopePointEx(envelope, autoitem_index, point_index)
        if ok then
          enumeration_order = enumeration_order + 1
          all_points[#all_points + 1] = {
            index = point_index,
            enumeration_order = enumeration_order,
            time = time,
            value = value,
            shape = shape,
            tension = tension,
            selected = selected == true,
            automation_item = autoitem_index >= 0 and autoitem_index or nil,
            automation_item_id = item_id,
          }
        end
      end
    end

    read_points(-1, "underlying")
    local item_count = reaper.CountAutomationItems(envelope)
    for item_index = 0, item_count - 1 do
      local function item_value(key)
        return finite_or_nil(reaper.GetSetAutomationItemInfo(envelope, item_index, key, 0, false))
      end
      local item = {
        index = item_index,
        pool_id = item_value("D_POOL_ID"),
        position = item_value("D_POSITION"),
        length = item_value("D_LENGTH"),
        start_offset = item_value("D_STARTOFFS"),
        play_rate = item_value("D_PLAYRATE"),
        looped = item_value("D_LOOPSRC") == 1,
        baseline = item_value("D_BASELINE"),
        amplitude = item_value("D_AMPLITUDE"),
        selected = item_value("D_UISEL") == 1,
        muted = item_value("D_MUTE") == 1,
      }
      item.identity = "item:" .. tostring(item.index) .. "/pool:" .. tostring(item.pool_id)
      automation_items[#automation_items + 1] = item
      read_points(item_index, item.identity)
    end
  end

  local selected_points, before, after = automation.select_range(
    all_points, start_time, end_time, payload.include_neighbors == true)
  local canonical_points, canonical_hash, duplicates = automation.canonicalize(
    selected_points, function(time) return reaper.TimeMap2_timeToQN(0, time) end)
  local _, track_name = reaper.GetTrackName(track, "")
  local automode = math.floor(reaper.GetMediaTrackInfo_Value(track, "I_AUTOMODE"))
  return {
    track = { index = track_index, name = track_name, guid = reaper.GetTrackGUID(track) },
    fx = fx_summary(track, api_index, display_fx_index or api_index, fx_scope or "track", fx_name),
    parameter = param_info,
    envelope = envelope_state,
    automation_mode = AUTOMATION_MODE_NAMES[automode] or tostring(automode),
    range = start_time and { start_time = start_time, end_time = end_time,
                             inclusive_start = true, inclusive_end = true } or nil,
    point_count = #canonical_points,
    points = canonical_points,
    neighbor_before = before,
    neighbor_after = after,
    duplicate_times = duplicates,
    automation_items = automation_items,
    canonical_hash = canonical_hash,
    canonical_hash_algorithm = "range-epsilon-1e-7s,qn-1/960,value-1e-6,tuple-sort,fnv1a32",
  }
end

-- Single-envelope transactional write. The payload doubles as the write spec
-- (target selectors, points, ranges) exactly as before; what changed is that
-- the write now snapshots, replaces, rereads, and rolls itself back.
function automation.write_command(command)
  local payload = command.payload or {}
  local body, prepared = automation.execute({ payload },
    { show = payload.show_envelopes ~= false })
  local summary, p = body.writes[1], prepared[1]
  local echo = {}
  for _, point in ipairs(p.points) do
    echo[#echo + 1] = {
      time = point.time, bar = bar_from_time(point.time),
      value = point.value, shape = point.shape, source = point.source,
    }
  end
  pcall(reaper.SetCursorContext, 2, p.envelope)
  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()
  summary.inserted_count = p.verification.inserted
  summary.points = echo
  if not p.scope.append then
    local min_s, max_e = nil, nil
    for _, r in ipairs(p.scope.ranges) do
      if not min_s or r.start_time < min_s then min_s = r.start_time end
      if not max_e or r.end_time > max_e then max_e = r.end_time end
    end
    summary.cleared_range = { start_time = min_s, end_time = max_e }
  end
  return summary
end

-- Multi-envelope atomic write: one undo block (run_command wraps mutating
-- commands), one preimage across every touched envelope, and a rollback that
-- restores every mutated envelope when any write fails partway.
function automation.transaction_command(command)
  local payload = command.payload or {}
  local writes = payload.writes
  if type(writes) ~= "table" or #writes == 0 then
    error("BAD_PAYLOAD: writes must be a non-empty array of write objects")
  end
  local body = automation.execute(writes, { show = payload.show_envelopes ~= false })
  local touched = {}
  for i, summary in ipairs(body.writes) do
    touched[i] = {
      track_guid = summary.track.guid,
      fx_guid = summary.fx.guid,
      fx_name = summary.fx.name,
      param_index = summary.parameter.index,
      param_name = summary.parameter.name,
    }
  end
  body.transaction_id = payload.transaction_id or command.id
  body.touched_envelopes = touched
  body.rolled_back = false
  return body
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

-- REAPER's send-mode ints are not contiguous (2 is unused), so the wire format
-- is a name and these two are the only places that know the numbers. The write
-- path stays an explicit allowlist rather than a reverse lookup, so an unknown
-- name is refused instead of resolving to whatever a table happens to hold; a
-- selftest asserts the two never drift apart.
local function send_mode_value(mode)
  if mode == "post_fader" then return 0 end
  if mode == "pre_fx" then return 1 end
  if mode == "pre_fader" then return 3 end
  return nil
end

local SEND_MODE_NAMES = { [0] = "post_fader", [1] = "pre_fx", [3] = "pre_fader" }

local function command_create_send(command)
  local payload = command.payload or {}
  local source = find_track(payload)
  -- The destination carries the same selector keys as the source, one level
  -- down, so both ends resolve through the same rules (guid, exact name,
  -- substring, selection) instead of the destination getting a lesser one.
  if type(payload.destination) ~= "table" then
    error("BAD_PAYLOAD: Provide destination as an object with a track selector")
  end
  local target = find_track(payload.destination)
  if target == source then
    error("BAD_PAYLOAD: destination resolves to the source track")
  end
  -- Resolve the optional values BEFORE creating anything. Validating the mode
  -- after CreateTrackSend left a stray unity-gain send behind on a command that
  -- then reported BAD_PAYLOAD (found on the first live run, 2026-08-17): a
  -- refused command must mutate nothing.
  local volume = nil
  if payload.volume_db ~= nil then
    local db = tonumber(payload.volume_db)
    if not db then error("BAD_VALUE: volume_db must be a number") end
    volume = volume_from_db(db)
  end
  local mode = nil
  if payload.mode ~= nil then
    mode = send_mode_value(payload.mode)
    if not mode then
      error("BAD_PAYLOAD: mode must be post_fader, pre_fx, or pre_fader")
    end
  end
  local send_index = reaper.CreateTrackSend(source, target)
  if not send_index or send_index < 0 then
    error("CREATE_SEND_FAILED: REAPER did not create the send")
  end
  if volume ~= nil then
    reaper.SetTrackSendInfo_Value(source, 0, send_index, "D_VOL", volume)
  end
  if mode ~= nil then
    reaper.SetTrackSendInfo_Value(source, 0, send_index, "I_SENDMODE", mode)
  end
  reaper.UpdateArrange()
  return {
    source = track_summary(source),
    destination = track_summary(target),
    send_index = send_index,
  }
end

-- A source feeding a bus usually stops feeding the master directly; this is
-- that switch.
local function command_set_master_send(command)
  local payload = command.payload or {}
  local track = find_track(payload)
  if type(payload.enabled) ~= "boolean" then
    error("BAD_PAYLOAD: Provide enabled as true or false")
  end
  reaper.SetMediaTrackInfo_Value(track, "B_MAINSEND", payload.enabled and 1 or 0)
  reaper.UpdateArrange()
  return { track = track_summary(track), master_send = payload.enabled }
end

-- Phase 2 (Post Mortem Verified Fix Preview), P2-001: track state snapshot and
-- restore. The snapshot is written to disk BEFORE any mutation happens (same
-- crash-safety pattern as the render-settings state file), and covers exactly
-- what the four supported preview operations can mutate: track volume, track
-- pan, per-FX enabled state, and named FX parameter values.

local SNAPSHOT_STATE_DIR = join(root, "state", "snapshots")

local function enumerate_track_fx(track)
  local fx = {}
  for_each_fx(track, function(i, api_index, scope, name)
    fx[#fx + 1] = {
      guid = reaper.TrackFX_GetFXGUID(track, api_index),
      index = i, api_index = api_index, scope = scope, name = name,
      enabled = reaper.TrackFX_GetEnabled(track, api_index),
    }
  end)
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

-- Phase 2 (Post Mortem Verified Fix Preview), P2-002: preview lifecycle.
-- preview_change snapshots first (P2-001), persists the preview state to disk,
-- then applies ONE temporary change. cancel_preview restores the snapshot.
-- commit_preview restores the baseline value, then re-applies the proposed
-- value inside exactly one named undo block — the only undo point the whole
-- lifecycle creates. A leftover preview (crash, restart, expiry) restores
-- automatically with cancel semantics.

local PREVIEW_STATE_PATH = join(root, "state", "preview.json")
local PREVIEW_MAX_AGE = 30 * 60

local PREVIEW_OPERATIONS = {
  set_track_volume = true, set_track_pan = true,
  set_fx_param = true, set_fx_bypass = true,
}

-- Pure: classify the stored preview state against a supplied token and clock.
local function preview_state_verdict(state, token, now_epoch)
  if type(state) ~= "table" or type(state.preview_token) ~= "string" then
    return "none"
  end
  if tonumber(state.expires_epoch) and now_epoch > tonumber(state.expires_epoch) then
    return "expired"
  end
  if token ~= nil and token ~= state.preview_token then
    return "token_mismatch"
  end
  return "active"
end

-- Pure: every identity field the caller supplied must still describe the same
-- live object. `live` carries the current names/indices; a nil live.fx means
-- the FX no longer exists. Returns nil when everything matches, else a typed
-- STALE_IDENTITY string that names the first mismatch.
local function preview_target_verdict(target, live)
  if target.track_name ~= nil and target.track_name ~= live.track_name then
    return "STALE_IDENTITY: track is now named " .. tostring(live.track_name)
      .. ", diagnosis said " .. tostring(target.track_name)
  end
  local needs_fx = target.fx_guid ~= nil
  if needs_fx then
    if not live.fx then
      return "STALE_IDENTITY: no FX with guid " .. tostring(target.fx_guid)
    end
    if target.fx_index ~= nil and target.fx_index ~= live.fx.index then
      return "STALE_IDENTITY: FX moved to index " .. tostring(live.fx.index)
        .. ", diagnosis said " .. tostring(target.fx_index)
    end
    if target.fx_scope ~= nil and target.fx_scope ~= live.fx.scope then
      return "STALE_IDENTITY: FX scope is " .. tostring(live.fx.scope)
        .. ", diagnosis said " .. tostring(target.fx_scope)
    end
    if target.fx_name ~= nil and target.fx_name ~= live.fx.name then
      return "STALE_IDENTITY: FX is now named " .. tostring(live.fx.name)
        .. ", diagnosis said " .. tostring(target.fx_name)
    end
    if target.parameter_index ~= nil and target.parameter_name ~= nil
       and live.parameter_name ~= nil
       and target.parameter_name ~= live.parameter_name then
      return "STALE_IDENTITY: parameter " .. tostring(target.parameter_index)
        .. " is now named " .. tostring(live.parameter_name)
        .. ", diagnosis said " .. tostring(target.parameter_name)
    end
  end
  return nil
end

-- Pure: pull the baseline value for the preview target out of a P2-001
-- snapshot so commit can restore-then-reapply exactly one value.
local function baseline_target_value(snapshot, target, operation)
  local values = snapshot.values or {}
  if operation == "set_track_volume" then
    if values.volume == nil then return nil, "SNAPSHOT_MISSING_TARGET: no volume recorded" end
    return values.volume
  end
  if operation == "set_track_pan" then
    if values.pan == nil then return nil, "SNAPSHOT_MISSING_TARGET: no pan recorded" end
    return values.pan
  end
  for _, fx in ipairs(values.fx or {}) do
    if fx.guid == target.fx_guid then
      if operation == "set_fx_bypass" then
        if fx.enabled == nil then return nil, "SNAPSHOT_MISSING_TARGET: no enabled state recorded" end
        return fx.enabled
      end
      for _, param in ipairs(fx.parameters or {}) do
        if param.index == target.parameter_index then
          return param.normalized_value
        end
      end
      return nil, "SNAPSHOT_MISSING_TARGET: parameter " .. tostring(target.parameter_index)
        .. " not in snapshot"
    end
  end
  return nil, "SNAPSHOT_MISSING_TARGET: FX " .. tostring(target.fx_guid) .. " not in snapshot"
end

local function read_preview_state()
  local text = read_file(PREVIEW_STATE_PATH)
  if not text then return nil end
  local ok, parsed = pcall(json.decode, text)
  if ok and type(parsed) == "table" then return parsed end
  return nil
end

-- Resolve and verify the preview target against the LIVE project. Returns the
-- track and (for FX operations) the live FX entry; throws typed errors and
-- mutates nothing.
local function resolve_preview_target(target, operation)
  if type(target) ~= "table" or type(target.track_guid) ~= "string" then
    error("BAD_PREVIEW_REQUEST: target.track_guid is required")
  end
  local track = find_track({ target_track_guid = target.track_guid })
  local _, track_name = reaper.GetTrackName(track, "")
  local live = { track_name = track_name }
  local fx_entry
  if operation == "set_fx_param" or operation == "set_fx_bypass" then
    if type(target.fx_guid) ~= "string" then
      error("BAD_PREVIEW_REQUEST: " .. operation .. " requires target.fx_guid")
    end
    for _, entry in ipairs(enumerate_track_fx(track)) do
      if entry.guid == target.fx_guid then fx_entry = entry break end
    end
    live.fx = fx_entry
    if operation == "set_fx_param" then
      local pidx = tonumber(target.parameter_index)
      if not pidx then
        error("BAD_PREVIEW_REQUEST: set_fx_param requires target.parameter_index")
      end
      if fx_entry then
        if pidx < 0 or pidx >= reaper.TrackFX_GetNumParams(track, fx_entry.api_index) then
          error("STALE_IDENTITY: parameter_index " .. tostring(pidx)
            .. " out of range for " .. fx_entry.name)
        end
        local _, pname = reaper.TrackFX_GetParamName(track, fx_entry.api_index, pidx, "")
        live.parameter_name = pname
      end
    end
  end
  local stale = preview_target_verdict(target, live)
  if stale then error(stale) end
  return track, fx_entry
end

-- Apply one preview value. Returns { before = ..., after = ... } in the
-- operation's natural unit (dB, normalized pan, normalized 0..1, bypassed).
local function apply_preview_value(track, fx_entry, target, operation, proposed)
  if operation == "set_track_volume" then
    local db = tonumber(proposed)
    if not db then error("BAD_PREVIEW_REQUEST: proposed_value must be dB for set_track_volume") end
    local before = db_from_volume(reaper.GetMediaTrackInfo_Value(track, "D_VOL"))
    reaper.SetMediaTrackInfo_Value(track, "D_VOL", volume_from_db(db))
    return { before = before, after = db, unit = "db" }
  end
  if operation == "set_track_pan" then
    local pan = tonumber(proposed)
    if not pan then error("BAD_PREVIEW_REQUEST: proposed_value must be -1..1 for set_track_pan") end
    if pan < -1 then pan = -1 end
    if pan > 1 then pan = 1 end
    local before = reaper.GetMediaTrackInfo_Value(track, "D_PAN")
    reaper.SetMediaTrackInfo_Value(track, "D_PAN", pan)
    return { before = before, after = pan, unit = "normalized_pan" }
  end
  if operation == "set_fx_param" then
    local value = tonumber(proposed)
    if not value then error("BAD_PREVIEW_REQUEST: proposed_value must be normalized 0..1") end
    if value < 0 then value = 0 end
    if value > 1 then value = 1 end
    local pidx = tonumber(target.parameter_index)
    local before = reaper.TrackFX_GetParamNormalized(track, fx_entry.api_index, pidx)
    reaper.TrackFX_SetParamNormalized(track, fx_entry.api_index, pidx, value)
    return { before = before, after = value, unit = "normalized" }
  end
  if operation == "set_fx_bypass" then
    if type(proposed) ~= "boolean" then
      error("BAD_PREVIEW_REQUEST: proposed_value must be a boolean (true = bypassed)")
    end
    local before_enabled = reaper.TrackFX_GetEnabled(track, fx_entry.api_index)
    reaper.TrackFX_SetEnabled(track, fx_entry.api_index, not proposed)
    return { before = not before_enabled, after = proposed, unit = "bypassed" }
  end
  error("BAD_PREVIEW_REQUEST: unsupported operation " .. tostring(operation))
end

-- Cancel semantics shared by cancel_preview, expiry, and startup recovery:
-- restore the full snapshot, then delete the preview state. Never throws; a
-- track that no longer exists has nothing left to restore.
local function restore_preview(state, reason)
  local ok, err = pcall(function()
    command_restore_track_state({
      payload = { snapshot_id = state.snapshot_id, delete_after = true },
    })
  end)
  if not ok then
    log_line("preview restore failed (" .. reason .. "): " .. tostring(err))
  end
  os.remove(PREVIEW_STATE_PATH)
  active_preview = nil
  log_line("preview " .. tostring(state.preview_token) .. " restored (" .. reason .. ")")
  return ok
end

local function command_preview_change(command)
  -- NO_UNDO_BLOCK commands skip run_command's dry_run short-circuit, but this
  -- one really mutates: honor dry_run here.
  if command.dry_run then
    return { dry_run = true, would_run = "preview_change", payload = command.payload or {} }
  end
  local payload = command.payload or {}
  local operation = payload.operation
  if not PREVIEW_OPERATIONS[operation] then
    error("BAD_PREVIEW_REQUEST: operation must be one of set_track_volume, "
      .. "set_track_pan, set_fx_param, set_fx_bypass")
  end
  if payload.proposed_value == nil then
    error("BAD_PREVIEW_REQUEST: proposed_value is required")
  end

  local existing = read_preview_state()
  local verdict = preview_state_verdict(existing, nil, os.time())
  if verdict == "active" then
    error("PREVIEW_ACTIVE: preview " .. existing.preview_token
      .. " is active; commit or cancel it first")
  elseif verdict == "expired" then
    restore_preview(existing, "expired")
  end

  local target = payload.target or {}
  local track, fx_entry = resolve_preview_target(target, operation)

  -- Snapshot BEFORE the mutation (P2-001). For a parameter preview, name the
  -- parameter so its baseline value is recorded.
  local snapshot_request = { payload = { target_track_guid = target.track_guid } }
  if operation == "set_fx_param" then
    snapshot_request.payload.parameters = {
      { fx_guid = target.fx_guid, parameter_index = target.parameter_index },
    }
  end
  local snapshot = command_snapshot_track_state(snapshot_request)

  local state = {
    schema_version = 1,
    preview_token = "pv-" .. os.date("!%Y%m%dT%H%M%SZ") .. "-"
      .. string.format("%06x", math.floor((reaper.time_precise() % 1) * 0xffffff)),
    created_at = now(),
    created_epoch = os.time(),
    expires_epoch = os.time() + PREVIEW_MAX_AGE,
    expires_at = os.date("!%Y-%m-%dT%H:%M:%SZ", os.time() + PREVIEW_MAX_AGE),
    operation = operation,
    target = target,
    proposed_value = payload.proposed_value,
    snapshot_id = snapshot.snapshot_id,
    project_name = get_project_name(),
  }
  -- Persist the preview state before applying: a crash between this write and
  -- the apply recovers to a restore of an unchanged track — harmless.
  atomic_write_json(PREVIEW_STATE_PATH, state)

  local applied = apply_preview_value(track, fx_entry, target, operation, payload.proposed_value)
  active_preview = state
  return {
    preview_token = state.preview_token,
    snapshot_id = snapshot.snapshot_id,
    operation = operation,
    applied = applied,
    expires_at = state.expires_at,
    track = track_summary(track),
  }
end

local function command_cancel_preview(command)
  if command.dry_run then
    return { dry_run = true, would_run = "cancel_preview", payload = command.payload or {} }
  end
  local payload = command.payload or {}
  local state = read_preview_state()
  local verdict = preview_state_verdict(state, payload.preview_token, os.time())
  if verdict == "none" then error("NO_ACTIVE_PREVIEW: nothing to cancel") end
  if verdict == "token_mismatch" then
    error("BAD_PREVIEW_TOKEN: token does not match the active preview")
  end
  if payload.preview_token == nil then
    error("BAD_PREVIEW_REQUEST: preview_token is required")
  end
  local restored = restore_preview(state, "cancelled")
  return { preview_token = state.preview_token, restored = restored }
end

local function command_commit_preview(command)
  if command.dry_run then
    return { dry_run = true, would_run = "commit_preview", payload = command.payload or {} }
  end
  local payload = command.payload or {}
  local state = read_preview_state()
  local verdict = preview_state_verdict(state, payload.preview_token, os.time())
  if verdict == "none" then error("NO_ACTIVE_PREVIEW: nothing to commit") end
  if verdict == "token_mismatch" then
    error("BAD_PREVIEW_TOKEN: token does not match the active preview")
  end
  if verdict == "expired" then
    restore_preview(state, "expired")
    error("PREVIEW_EXPIRED: the preview expired and the baseline was restored")
  end
  if payload.preview_token == nil then
    error("BAD_PREVIEW_REQUEST: preview_token is required")
  end

  -- Re-verify identities: an FX chain edit mid-preview refuses, restores, and
  -- leaves the project at baseline rather than committing against the wrong
  -- object.
  local resolve_ok, track_or_err, fx_entry = pcall(
    resolve_preview_target, state.target, state.operation)
  if not resolve_ok then
    restore_preview(state, "stale identity at commit")
    error(track_or_err)
  end
  local track = track_or_err

  local snapshot_path = join(SNAPSHOT_STATE_DIR, state.snapshot_id .. ".json")
  local snapshot_text = read_file(snapshot_path)
  if not snapshot_text then
    error("NO_SNAPSHOT: preview snapshot " .. tostring(state.snapshot_id) .. " is missing")
  end
  local decoded_ok, snapshot = pcall(json.decode, snapshot_text)
  if not decoded_ok or snapshot_validate(snapshot) then
    error("BAD_SNAPSHOT: preview snapshot is unreadable")
  end
  local baseline, baseline_err = baseline_target_value(snapshot, state.target, state.operation)
  if baseline == nil and baseline_err then error(baseline_err) end

  -- Restore the baseline value silently, then re-apply the proposed value
  -- inside ONE named undo block: undoing that single point returns the user
  -- to their pre-preview state.
  local pidx = tonumber(state.target.parameter_index)
  if state.operation == "set_track_volume" then
    reaper.SetMediaTrackInfo_Value(track, "D_VOL", baseline)
  elseif state.operation == "set_track_pan" then
    reaper.SetMediaTrackInfo_Value(track, "D_PAN", baseline)
  elseif state.operation == "set_fx_param" then
    reaper.TrackFX_SetParamNormalized(track, fx_entry.api_index, pidx, baseline)
  elseif state.operation == "set_fx_bypass" then
    reaper.TrackFX_SetEnabled(track, fx_entry.api_index, baseline)
  end

  local _, track_name = reaper.GetTrackName(track, "")
  local undo_name = "Post Mortem: " .. state.operation .. " on " .. tostring(track_name)
  reaper.Undo_BeginBlock()
  local apply_ok, applied = pcall(
    apply_preview_value, track, fx_entry, state.target, state.operation, state.proposed_value)
  reaper.Undo_EndBlock(undo_name, -1)
  if not apply_ok then error(applied) end

  os.remove(snapshot_path)
  os.remove(PREVIEW_STATE_PATH)
  active_preview = nil
  log_line("preview " .. state.preview_token .. " committed: " .. undo_name)
  return {
    preview_token = state.preview_token,
    committed = applied,
    undo_point = undo_name,
    track = track_summary(track),
  }
end

-- Expire an overdue preview from the defer loop (rule 6). Cheap: file reads
-- only happen while a preview is active.
local function maybe_expire_preview(now_epoch)
  if not active_preview then return end
  if tonumber(active_preview.expires_epoch)
     and now_epoch > tonumber(active_preview.expires_epoch) then
    restore_preview(active_preview, "expired")
  end
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
  local end_time = range_end or resolve_range_end(payload, start_time)
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

-- REAPER's synchronous render path has two first-run modal hazards in the
-- `renderclosewhendone` bitfield. Bit 0 controls "Automatically close when
-- finished". REAPER 7.75 added bit 21 for retaining render statistics; when it
-- is off, our first RENDER_STATS read opens a Yes/No dialog and blocks the defer
-- loop. Force both bits only for our render, read the statistics, then restore
-- the user's exact setting. This is the bridge's ONLY use of SWS (SNM_*), so
-- degrade gracefully when SWS is absent.
local RENDER_AUTOCLOSE_VAR = "renderclosewhendone"
local RENDER_REQUIRED_BITS = 1 | (1 << 21)

local RENDER_PREFERENCES_REMEDIATION =
  "enable both 'Automatically close when finished' and 'Save render statistics' "
  .. "in REAPER's render window"

local function ensure_render_autoclose()
  if not (reaper.SNM_GetIntConfigVar and reaper.SNM_SetIntConfigVar) then
    return {
      guaranteed = false,
      reason = "SWS not installed: cannot safely force render preferences; "
        .. RENDER_PREFERENCES_REMEDIATION,
    }
  end
  local cur = reaper.SNM_GetIntConfigVar(RENDER_AUTOCLOSE_VAR, -1)
  if cur == -1 then
    -- -1 is the "var not found" sentinel; the real bitfield is never negative.
    return {
      guaranteed = false,
      reason = "renderclosewhendone unavailable; " .. RENDER_PREFERENCES_REMEDIATION,
    }
  end
  if cur & RENDER_REQUIRED_BITS == RENDER_REQUIRED_BITS then
    return { guaranteed = true }  -- already safe; leave it, nothing to restore
  end
  local wanted = cur | RENDER_REQUIRED_BITS
  local set_ok = reaper.SNM_SetIntConfigVar(RENDER_AUTOCLOSE_VAR, wanted)
  local observed = reaper.SNM_GetIntConfigVar(RENDER_AUTOCLOSE_VAR, -1)
  if not set_ok or observed ~= wanted then
    return {
      guaranteed = false,
      restore = cur,
      reason = "SWS could not enable the required render preferences; "
        .. RENDER_PREFERENCES_REMEDIATION,
    }
  end
  return { guaranteed = true, restore = cur }
end

local function restore_render_autoclose(token)
  if not token or token.restore == nil then return true end
  if not (reaper.SNM_GetIntConfigVar and reaper.SNM_SetIntConfigVar) then
    return false, "RENDER_PREFERENCES_RESTORE_FAILED: SWS became unavailable"
  end
  local set_ok = reaper.SNM_SetIntConfigVar(RENDER_AUTOCLOSE_VAR, token.restore)
  local observed = reaper.SNM_GetIntConfigVar(RENDER_AUTOCLOSE_VAR, -1)
  if not set_ok or observed ~= token.restore then
    return false, "RENDER_PREFERENCES_RESTORE_FAILED: could not restore the user's exact renderclosewhendone value"
  end
  return true
end

local function render_preferences_error(token)
  if token and token.guaranteed then return nil end
  return "RENDER_PREFERENCES_UNSAFE: "
    .. tostring(token and token.reason or "render preferences were not checked")
end

-- Which tracks live INSIDE the folder opened at `index` (1-based, in project
-- order), given every track's I_FOLDERDEPTH. Returns an empty list when the
-- track is not a folder parent.
--
-- This exists because a folder parent has zero media items and therefore takes
-- the item-less isolation path below, where an exclusive solo would mute the
-- very children that feed it and the parent would render digital silence. The
-- subtree has to stay audible for a folder capture to mean anything.
--
-- Depth encoding is REAPER's: 1 opens a folder, 0 is an ordinary track, and a
-- negative value closes that many levels (so a single track can close several
-- nested folders at once).
local function folder_descendants(depths, index)
  local out = {}
  if (depths[index] or 0) < 1 then return out end
  local level = 1
  for i = index + 1, #depths do
    out[#out + 1] = i
    level = level + (depths[i] or 0)
    if level <= 0 then break end
  end
  return out
end

-- Capture provenance is part of the public result contract. A caller must never
-- need to infer whether a WAV is a real isolated stem by parsing a prose note.
-- `full_mix` is deliberately explicit: it is useful for debugging, but unsafe
-- as evidence for a per-track diagnosis or cross-track masking claim.
local function capture_provenance(isolate, is_master, folder_children)
  if isolate then
    if folder_children and folder_children > 0 then
      return {
        capture_scope = "isolated_track",
        isolation_verified = true,
        folder_children = folder_children,
        note = string.format("Isolated FOLDER bus: the target and its %d child track(s) were soloed together, because a folder parent's audio comes from its children and soloing it alone renders silence. The FX on every bus DOWNSTREAM of the folder (its own parent folders + master) were bypassed, so this is the folder's own summed signal including the children's FX and the folder's own FX. All solo and FX states restored after.", folder_children),
      }
    end
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

-- ---------------------------------------------------------------------------
-- Panel support (Post Mortem Phase 3, P3-002)
-- ---------------------------------------------------------------------------

-- What capture provenance a track WOULD get, without rendering anything: the
-- same decision command_capture_track_audio makes (isolate = non-master track
-- with zero media items). Pure so the selftest covers every combination.
local function expected_capture_scope(is_master, item_count)
  if is_master then return "master_output" end
  if (item_count or 0) == 0 then return "isolated_track" end
  return "full_mix"
end

-- Preflight verdict from plain values (pure, selftest-covered).
-- render_preferences is true only when both modal-prevention bits are already
-- enabled, false when SWS can read and force them, and nil when SWS cannot
-- guarantee a non-blocking render. Unknown preferences fail closed.
local function preflight_verdict(allow_risk, sws_installed, render_preferences)
  local blockers, warnings = {}, {}
  if not allow_risk then
    blockers[#blockers + 1] = {
      code = "capture_gated",
      message = "allow_risk_level_3 is false in bridge_config.json. It is read "
        .. "once at REAPER startup: enable it, then restart REAPER (there is "
        .. "no reload command).",
    }
  end
  local can_force = sws_installed and render_preferences ~= nil
  if render_preferences ~= true and not can_force then
    blockers[#blockers + 1] = {
      code = "render_preferences_unavailable",
      message = "The bridge cannot guarantee a non-blocking render because SWS "
        .. "is missing or renderclosewhendone is unreadable. Install SWS, or "
        .. RENDER_PREFERENCES_REMEDIATION .. ", then run the preflight again.",
    }
  end
  return {
    capture_allowed = #blockers == 0,
    blockers = blockers,
    warnings = warnings,
  }
end

-- Read-only selected-track report for the panel's Track screen idle card.
-- Uses the SAME capture-start resolution as capture_track_audio (active time
-- selection's start when one exists, else the edit cursor) so what the panel
-- shows is what a capture would do.
local function command_get_selected_track(command)
  local selected_count = reaper.CountSelectedTracks(0)
  if selected_count == 0 then
    return { selected = false, selected_count = 0 }
  end
  local track = reaper.GetSelectedTrack(0, 0)
  local summary = track_summary(track)
  local item_count = reaper.CountTrackMediaItems(track)

  local ts_start, ts_end = reaper.GetSet_LoopTimeRange(false, false, 0, 0, false)
  local has_time_selection = ts_end > ts_start
  local probe = has_time_selection and ts_start or reaper.GetCursorPosition()
  local items_at_start = 0
  for i = 0, item_count - 1 do
    local item = reaper.GetTrackMediaItem(track, i)
    local pos = reaper.GetMediaItemInfo_Value(item, "D_POSITION")
    local len = reaper.GetMediaItemInfo_Value(item, "D_LENGTH")
    if probe >= pos and probe < pos + len then
      items_at_start = items_at_start + 1
    end
  end

  return {
    selected = true,
    selected_count = selected_count,
    track = summary,
    fx_count = reaper.TrackFX_GetCount(track),
    item_count = item_count,
    receive_count = reaper.GetTrackNumSends(track, -1),
    capture_source = has_time_selection and "time_selection" or "edit_cursor",
    capture_start_seconds = probe,
    items_at_capture_start = items_at_start,
    expected_capture_scope = expected_capture_scope(summary.is_master == true, item_count),
  }
end

-- Everything that would gate or degrade a capture, WITHOUT rendering: risk
-- gate state, SWS presence, render auto-close state, and (with any track
-- selector) the provenance a capture of that track would carry. Powers the
-- panel's onboarding checklist and "Test Again" without a render per check.
local function command_get_capture_preflight(command)
  local payload = command.payload or {}
  local sws_installed = (reaper.SNM_GetIntConfigVar and reaper.SNM_SetIntConfigVar)
    and true or false
  local autoclose = nil
  local render_preferences = nil
  if sws_installed then
    local cur = reaper.SNM_GetIntConfigVar(RENDER_AUTOCLOSE_VAR, -1)
    if cur ~= -1 then
      autoclose = (cur & 1 == 1)
      render_preferences = (cur & RENDER_REQUIRED_BITS == RENDER_REQUIRED_BITS)
    end
  end
  local verdict = preflight_verdict(
    config.allow_risk_level_3 == true, sws_installed, render_preferences)

  local target = nil
  if payload.target_track_name or payload.target_track_guid
    or payload.track_name_contains or payload.use_selected_track then
    local track = find_track(payload)
    local summary = track_summary(track)
    local item_count = reaper.CountTrackMediaItems(track)
    target = {
      track = summary,
      item_count = item_count,
      expected_capture_scope = expected_capture_scope(summary.is_master == true, item_count),
    }
  end

  return {
    capture_allowed = verdict.capture_allowed,
    blockers = verdict.blockers,
    warnings = verdict.warnings,
    risk_gate = {
      allow_risk_level_3 = config.allow_risk_level_3 == true,
      requires_restart_to_change = true,
    },
    sws_installed = sws_installed,
    render_autoclose = autoclose,
    render_preferences_ready = render_preferences,
    target = target,
  }
end

-- Split one output_file into the pair REAPER actually wants: RENDER_FILE is the
-- directory, RENDER_PATTERN the filename, and the extension comes from the sink
-- format (so a trailing .wav in the pattern would render "name.wav.wav").
-- Capture had this inline; render needs the identical split, so it lives here.
local function split_render_target(output_file)
  local path = tostring(output_file)
  local dir, name = path:match("^(.*)[/\\]([^/\\]+)$")
  if not dir then dir, name = "", path end
  return dir, (name:gsub("%.wav$", ""))
end

local function command_render(command)
  if not config.allow_risk_level_3 then
    error("RENDER_BLOCKED: render is gated; set allow_risk_level_3 true in bridge_config.json")
  end
  local payload = command.payload or {}
  if payload.output_file and payload.output_file ~= "" then
    -- Writing the whole path into RENDER_FILE alone left RENDER_PATTERN at
    -- whatever the user last rendered with, so REAPER wrote somewhere the
    -- reply never named.
    local dir, pattern = split_render_target(payload.output_file)
    reaper.GetSetProjectInfo_String(0, "RENDER_FILE", dir, true)
    reaper.GetSetProjectInfo_String(0, "RENDER_PATTERN", pattern, true)
  end
  local bounds = { entire = 1, project = 1, time_selection = 2, regions = 3, selected_items = 4 }
  if payload.bounds and bounds[payload.bounds] then
    reaper.GetSetProjectInfo(0, "RENDER_BOUNDSFLAG", bounds[payload.bounds], true)
  end
  -- Everything is configured now, so REAPER can say what it will write; trust
  -- that over the request, exactly as capture does.
  local _, targets = reaper.GetSetProjectInfo_String(0, "RENDER_TARGETS", "", false)
  local target = targets and targets:match("^[^;]+") or nil
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
  local preference_error = render_preferences_error(autoclose)
  if preference_error then
    local restored, restore_error = restore_render_autoclose(autoclose)
    if not restored then preference_error = preference_error .. "; " .. restore_error end
    error(preference_error)
  end
  local render_ok, render_error = pcall(reaper.Main_OnCommand, 42230, 0)
  local restored, restore_error = restore_render_autoclose(autoclose)
  if not restored then error(restore_error) end
  if not render_ok then error(render_error) end
  return {
    rendered = true,
    target = target,
    output_file = select(2, reaper.GetSetProjectInfo_String(0, "RENDER_FILE", "", false)),
    note = "Render used REAPER's last-saved render settings.",
    render_autoclose_warning = autoclose.guaranteed and nil or autoclose.reason,
  }
end

-- A project REAPER has never written has no path to overwrite, so
-- Main_SaveProject opens a Save As dialog — and that modal blocks the defer loop
-- until a human dismisses it, the same failure the render preferences exist to
-- prevent. The bridge is headless by definition and nobody may be at the
-- machine, so refuse rather than gamble. Pure, so the selftest covers it without
-- a project open.
local function save_target_error(project_path)
  if project_path == nil or project_path == "" then
    return "SAVE_UNSAFE: the open project has never been saved, so saving would " ..
      "open a Save As dialog and block the bridge; save it once in REAPER first"
  end
  return nil
end

-- Saving overwrites the user's .rpp on disk, which no undo block can take back,
-- so it rides the same gate as render and capture rather than one of its own.
local function command_save_project()
  if not config.allow_risk_level_3 then
    error("SAVE_BLOCKED: save_project is gated; set allow_risk_level_3 true in bridge_config.json")
  end
  -- EnumProjects(-1) is the active project; its filename is "" until the project
  -- has been saved once. get_project_name's "Untitled" fallback reads the same
  -- state, one step further removed.
  local _, project_path = reaper.EnumProjects(-1, "")
  local unsafe = save_target_error(project_path)
  if unsafe then error(unsafe) end
  reaper.Main_SaveProject(0, false)
  -- Name the file that was actually written, not just the project: a reply that
  -- can't be checked against the disk is how the render bug hid.
  return { saved = true, project_name = get_project_name(), project_path = project_path }
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

  local dir, pattern = split_render_target(output_file)

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
  local track_count = reaper.CountTracks(0)

  -- A folder parent also has zero media items, so it lands on the isolation
  -- path above. Its audio comes from its children, and the exclusive solo below
  -- would mute exactly those: soloed alone, a folder bus renders digital
  -- silence at every cursor position. Solo the subtree with it.
  local subtree = {}
  if isolate then
    local depths, target_index = {}, nil
    for i = 0, track_count - 1 do
      local t = reaper.GetTrack(0, i)
      depths[i + 1] = reaper.GetMediaTrackInfo_Value(t, "I_FOLDERDEPTH")
      if t == track then target_index = i + 1 end
    end
    if target_index then
      for _, one_based in ipairs(folder_descendants(depths, target_index)) do
        subtree[#subtree + 1] = reaper.GetTrack(0, one_based - 1)
      end
    end
  end

  local provenance = capture_provenance(isolate, track == master_track, #subtree)
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
      -- The children feed the folder; muting them is what made a folder
      -- capture silent regardless of where the cursor was.
      for k = 1, #subtree do
        reaper.SetMediaTrackInfo_Value(subtree[k], "I_SOLO", 1)
      end
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
    local preference_error = render_preferences_error(autoclose_token)
    if preference_error then error(preference_error) end
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
      -- Structured, not just prose in `note`: a caller must be able to tell a
      -- folder capture from a plain one without parsing English. Absent (not
      -- 0) on a non-folder capture, so `folder_children` present means "the
      -- subtree was soloed with the target".
      folder_children = provenance.folder_children,
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
  local preferences_restored, restore_error = restore_render_autoclose(autoclose_token)
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

  if not preferences_restored then error(restore_error) end
  if not ok then error(result) end
  return result
end

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
        -- create_send writes this and nothing could read it back, so a send's
        -- mode was the one field the bridge could set but never verify.
        mode = SEND_MODE_NAMES[math.floor(v("I_SENDMODE"))]
          or tostring(math.floor(v("I_SENDMODE"))),
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
    -- What set_master_send writes, so a caller can read back the switch it
    -- threw without reopening the routing window.
    master_send = reaper.GetMediaTrackInfo_Value(track, "B_MAINSEND") == 1,
    phase_inverted = reaper.GetMediaTrackInfo_Value(track, "B_PHASE") == 1,
    automation_mode = AUTOMATION_MODE_NAMES[automode] or tostring(automode),
  }
end

-- ---------------------------------------------------------------------------
-- Discovery
-- ---------------------------------------------------------------------------

local function scan_track_fx(track, include_values, max_params)
  local entries = {}
  for_each_fx(track, function(fx, api_index, scope, fx_name)
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
  end)
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
  local note_count = 0
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
          note_count = note_count + 1
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
    note_count = note_count,
    notes = notes,
  }
end

-- Pure: decide whether a reload may proceed. Fails toward keeping the running
-- bridge. An active preview refuses first (the fresh instance's startup
-- recovery would restore its baseline — silently cancelling ephemeral state
-- the agent still thinks is live); a compile failure refuses (handing over to
-- a broken file would kill the bridge for a typo, when the whole point of
-- reload is making bridge edits cheap).
local function reload_verdict(compiles, compile_err, has_active_preview)
  if has_active_preview then
    return "RELOAD_BLOCKED: an active preview would be cancelled by the fresh "
      .. "instance's startup recovery; commit_preview or cancel_preview first"
  end
  if not compiles then
    return "RELOAD_COMPILE_FAILED: " .. tostring(compile_err)
  end
  return nil
end

-- Replace the running bridge with a fresh instance loaded from disk, without a
-- REAPER restart. The handler only schedules: the reply must reach outbox/
-- from the OLD instance before the handover, so the actual work happens at the
-- bottom of the defer loop (see perform_reload). The reply carries the current
-- LOADED_AT so the caller can confirm the handover by watching the heartbeat's
-- loaded_at change.
local function command_reload_bridge(command)
  -- reload_bridge sits in NO_UNDO_BLOCK (it never touches the project), which
  -- means run_command's dry_run short-circuit does not cover it — without this
  -- branch a dry_run would really reload. Same either-position rule as
  -- run_command's.
  if command.dry_run or (type(command.payload) == "table" and command.payload.dry_run) then
    return { dry_run = true, would_run = "reload_bridge", script_path = BRIDGE_FILE }
  end
  local chunk, err = loadfile(BRIDGE_FILE)
  local verdict = reload_verdict(chunk ~= nil, err, active_preview ~= nil)
  if verdict then error(verdict) end
  reload_requested = true
  return {
    reload = "scheduled",
    script_path = BRIDGE_FILE,
    loaded_at = LOADED_AT,
    note = "reload runs after this reply; confirm via heartbeat loaded_at changing",
  }
end

local handlers = {}

-- Commands that don't need an undo block: they read state, not project state.
-- Everything else mutates the project and gets wrapped. Named for what it IS.
local NO_UNDO_BLOCK = {
  get_context = true, get_fx_parameters = true, scan_fx = true,
  get_fx_param_automation = true,
  enum_installed_fx = true,
  discover_drum_map = true,
  get_track_routing = true,
  -- capture_track_audio restores everything it touches (selection, render
  -- settings), so there is no project state a user would want to Ctrl+Z.
  capture_track_audio = true,
  -- snapshot_track_state only reads project state; its write is a state file
  -- outside the project.
  snapshot_track_state = true,
  -- Panel support (P3-002): both are pure reads.
  get_selected_track = true,
  get_capture_preflight = true,
  get_midi_notes = true,
  -- The preview lifecycle deliberately creates NO undo points except the one
  -- commit_preview manages itself (restore baseline, then apply inside a
  -- single named Undo block). Temporary preview state must never appear in
  -- the user's undo history.
  preview_change = true,
  cancel_preview = true,
  commit_preview = true,
  -- reload_bridge mutates the process, not the project — there is nothing an
  -- undo point could hold. Its handler carries its own dry_run branch because
  -- membership here bypasses run_command's dry_run short-circuit.
  reload_bridge = true,
  get_items = true,
  get_render_settings = true,
  -- set_render_settings mutates state that lives OUTSIDE the undo history, so
  -- a block would only add an empty undo point. Own dry_run branch, like
  -- reload_bridge.
  set_render_settings = true,
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

  -- dry_run is an envelope field by convention, but `reaperd.py cmd` can only
  -- reach the payload — and a dry_run that silently EXECUTES is the worst
  -- inversion available (caught live 2026-08-18: a payload dry_run on
  -- reload_bridge really reloaded). Honor it in either position.
  local payload_dry = type(command.payload) == "table" and command.payload.dry_run
  if (command.dry_run or payload_dry) and is_mutating(command.type) then
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

-- Read / context
-- get_selected_track is registered in the panel-support block below; the
-- P3-002 command replaced the old minimal {name, guid} reply (kept as
-- top-level compat fields) and returns selected=false instead of erroring
-- when nothing is selected.
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
handlers.save_project = command_save_project
handlers.capture_track_audio = command_capture_track_audio
handlers.get_track_routing = command_get_track_routing

-- Panel support (Post Mortem Phase 3, P3-002)
handlers.get_selected_track = command_get_selected_track
handlers.get_capture_preflight = command_get_capture_preflight

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
-- Routing writes stop at sends. Folder routing would need tracks reordered and
-- folder depths rewritten, which the API makes fragile enough to lose a user's
-- arrangement; a send is a safe, reversible edit, and get_track_routing already
-- reads back exactly what these two write.
handlers.create_send = command_create_send
handlers.set_master_send = command_set_master_send

-- Track state snapshot/restore (Post Mortem Verified Fix Preview, P2-001)
handlers.snapshot_track_state = command_snapshot_track_state
handlers.restore_track_state = command_restore_track_state

-- Preview lifecycle (Post Mortem Verified Fix Preview, P2-002)
handlers.preview_change = command_preview_change
handlers.cancel_preview = command_cancel_preview
handlers.commit_preview = command_commit_preview

-- FX
handlers.add_fx = command_add_fx
handlers.add_fx_chain = command_add_fx_chain
handlers.remove_fx = command_remove_fx
handlers.bypass_fx = command_bypass_fx
handlers.move_fx = command_move_fx
handlers.set_fx_param = command_set_fx_param
handlers.write_fx_param_automation = automation.write_command
handlers.get_fx_param_automation = automation.read_command
handlers.apply_automation_transaction = automation.transaction_command

-- Markers / regions / items
handlers.add_marker = command_add_marker
handlers.add_region = command_add_region
handlers.delete_marker = command_delete_marker
handlers.delete_items_in_range = command_delete_items_in_range

-- MIDI
handlers.insert_midi_file = command_insert_midi_file
handlers.get_midi_notes = command_get_midi_notes
handlers.set_note_velocities = command_set_note_velocities
handlers.set_note_positions  = command_set_note_positions

-- Diagnostic read/write surface added 2026-08-18. Defined in a do-block
-- assigning straight onto `handlers`: the main chunk is at Lua's 200-local
-- limit, so new top-level `local function`s no longer fit.
do
-- Read-only item inventory for one track. Built during the 2026-08-18
-- silent-render diagnosis: the bridge could count items but not see them, so
-- "are these items offline / do their sources resolve" was unanswerable.
-- source_readable is io.open FROM INSIDE REAPER'S PROCESS — it tests the
-- file access REAPER actually has, which an outside shell cannot.
handlers.get_items = function(command)
  local track, track_index = find_track(command.payload or {})
  local items = {}
  for i = 0, reaper.CountTrackMediaItems(track) - 1 do
    local item = reaper.GetTrackMediaItem(track, i)
    local take = reaper.GetActiveTake(item)
    local entry = {
      index = i,
      position = reaper.GetMediaItemInfo_Value(item, "D_POSITION"),
      length = reaper.GetMediaItemInfo_Value(item, "D_LENGTH"),
      muted = reaper.GetMediaItemInfo_Value(item, "B_MUTE") == 1,
      volume = reaper.GetMediaItemInfo_Value(item, "D_VOL"),
      selected = reaper.IsMediaItemSelected(item),
    }
    if take then
      local _, take_name = reaper.GetSetMediaItemTakeInfo_String(take, "P_NAME", "", false)
      entry.take_name = take_name
      local src = reaper.GetMediaItemTake_Source(take)
      if src then
        entry.source_type = reaper.GetMediaSourceType(src, "")
        local filename = reaper.GetMediaSourceFileName(src, "")
        if filename and filename ~= "" then
          entry.source_file = filename
          local f = io.open(filename, "rb")
          entry.source_readable = f ~= nil
          if f then f:close() end
        end
        local length, is_qn = reaper.GetMediaSourceLength(src)
        entry.source_length = length
        entry.source_length_is_qn = is_qn and true or false
      end
    end
    items[#items + 1] = entry
  end
  local _, track_name = reaper.GetTrackName(track, "")
  return { track = { index = track_index, name = track_name }, item_count = #items, items = items }
end

-- Read-only view of the project's render configuration. Exists because render
-- and capture WRITE these values and nothing could read them back — the
-- render-path clobber (2026-08-17) and the silent-render diagnosis (2026-08-18)
-- both stalled on exactly this missing surface.
local RENDER_NUM_KEYS = {
  settings = "RENDER_SETTINGS", bounds_flag = "RENDER_BOUNDSFLAG",
  start_pos = "RENDER_STARTPOS", end_pos = "RENDER_ENDPOS",
  sample_rate = "RENDER_SRATE", channels = "RENDER_CHANNELS",
}
local RENDER_STR_KEYS = { file = "RENDER_FILE", pattern = "RENDER_PATTERN" }

handlers.get_render_settings = function(command)
  local out = {}
  for name, key in pairs(RENDER_NUM_KEYS) do
    out[name] = reaper.GetSetProjectInfo(0, key, 0, false)
  end
  for name, key in pairs(RENDER_STR_KEYS) do
    local _, v = reaper.GetSetProjectInfo_String(0, key, "", false)
    out[name] = v
  end
  local _, targets = reaper.GetSetProjectInfo_String(0, "RENDER_TARGETS", "", false)
  out.targets = targets
  return out
end

-- Writes render configuration and reads every written key straight back into
-- the reply — same honesty rule as the render fix: a reply nobody can check
-- against REAPER's own state is how the last render bug hid. Render settings
-- live OUTSIDE the undo history, so no undo block (NO_UNDO_BLOCK), which means
-- the dry_run branch here is load-bearing, like reload_bridge's.
handlers.set_render_settings = function(command)
  local payload = command.payload or {}
  if command.dry_run or payload.dry_run then
    return { dry_run = true, would_run = "set_render_settings" }
  end
  local applied, any = {}, false
  for name, key in pairs(RENDER_NUM_KEYS) do
    local v = payload[name]
    if v ~= nil then
      if type(v) ~= "number" then error("BAD_PAYLOAD: " .. name .. " must be a number") end
      reaper.GetSetProjectInfo(0, key, v, true)
      applied[name] = reaper.GetSetProjectInfo(0, key, 0, false)
      any = true
    end
  end
  for name, key in pairs(RENDER_STR_KEYS) do
    local v = payload[name]
    if v ~= nil then
      if type(v) ~= "string" then error("BAD_PAYLOAD: " .. name .. " must be a string") end
      reaper.GetSetProjectInfo_String(0, key, v, true)
      local _, rb = reaper.GetSetProjectInfo_String(0, key, "", false)
      applied[name] = rb
      any = true
    end
  end
  if not any then
    error("BAD_PAYLOAD: nothing to set; accepted keys: settings, bounds_flag, "
      .. "start_pos, end_pos, sample_rate, channels, file, pattern")
  end
  return { applied = applied }
end
end

-- Bridge lifecycle
handlers.reload_bridge = command_reload_bridge

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

-- The reload handover. Runs only from the bottom of the defer loop, after the
-- reload command's reply is in outbox/ and its file archived. The lock is
-- DELETED, not left to go stale: the fresh instance's startup lock_verdict
-- then proceeds immediately instead of refusing for 60s. Re-compiled here even
-- though the handler already checked, because the file can change between the
-- reply and this tick. If the fresh instance's top level throws, this instance
-- re-claims the lock and keeps running — a broken edit costs a log line, not
-- the bridge. (Should a partially-started fresh instance have written its own
-- lock before dying, the re-claim overwrites it; nothing else is deferring.)
local function perform_reload()
  local chunk, err = loadfile(BRIDGE_FILE)
  if not chunk then
    log_line("reload aborted, compile failed: " .. tostring(err))
    return false
  end
  log_line("bridge reloading; handing over to a fresh instance of " .. BRIDGE_FILE)
  os.remove(lockfile)
  -- Loading via a chunk means get_action_context reports the ORIGINAL
  -- launcher, so point the fresh instance at its own dir the same way the
  -- startup watchdog does.
  REAPER_AGENT_BRIDGE_DIR = SCRIPT_DIR
  local ok, run_err = pcall(chunk)
  if ok then return true end
  log_line("reload failed: " .. tostring(run_err) .. "; old bridge resuming")
  write_lock("none")
  return false
end

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
        -- Hand over before touching another command: anything still in inbox/
        -- belongs to the fresh instance.
        if reload_requested then break end
        if reaper.time_precise() - tick_start >= DRAIN_BUDGET then break end
      end
      maybe_sweep()
      maybe_expire_preview(os.time())
    end)
    if not ok then
      log_line("loop error " .. tostring(err))
      in_flight_command = nil
    end
  end
  if reload_requested then
    reload_requested = false
    -- On success the fresh instance registered its own defer chain; this one
    -- ends by not re-deferring. On failure, fall through and keep running.
    if perform_reload() then return end
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
    numeric_bracket = numeric_bracket,
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
    render_preferences_error = render_preferences_error,
    fx_summary = fx_summary,
    find_fx = find_fx,
    set_fx_param_result = set_fx_param_result,
    batch_result = batch_result,
    capture_provenance = capture_provenance,
    folder_descendants = folder_descendants,
    snapshot_validate = snapshot_validate,
    restore_plan = restore_plan,
    preview_state_verdict = preview_state_verdict,
    preview_target_verdict = preview_target_verdict,
    baseline_target_value = baseline_target_value,
    expected_capture_scope = expected_capture_scope,
    preflight_verdict = preflight_verdict,
    velocity_plan = velocity_plan,
    position_plan = position_plan,
    velocity_summary = velocity_summary,
    send_mode_value = send_mode_value,
    send_mode_names = SEND_MODE_NAMES,
    save_target_error = save_target_error,
    split_render_target = split_render_target,
    reload_verdict = reload_verdict,
    automation = automation,
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

-- Preview crash recovery (P2-002): a preview left behind by a crash, a killed
-- bridge, or a REAPER restart restores its baseline before any new command
-- runs. Cancel semantics; the recovery is logged and surfaced in the
-- heartbeat as preview_recovered_at.
do
  local leftover = read_preview_state()
  if leftover then
    restore_preview(leftover, "recovered at startup")
    preview_recovered_at = now()
  end
end

sweep_once()
last_sweep = reaper.time_precise()

log_line("bridge started")
loop()

