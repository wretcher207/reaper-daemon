-- Self-check for the Daemon Console panel's pure helpers and the focus
-- envelope. No live REAPER, no ImGui, no sidecar: the panel file returns its
-- helper table early when REAPER_CONSOLE_SELFTEST is set, before it touches
-- ReaImGui, and the focus module reaches REAPER only through the global that
-- this file replaces with a stub.
--
-- The Windows run is the one that matters for atomic_write_json: rename()
-- there cannot replace an existing file, so the remove+retry fallback is only
-- ever exercised on that platform.
--   Run:  lua bridge/test_console.lua

local sep = package.config:sub(1, 1)
local win = sep == "\\"
local function join(...) return table.concat({ ... }, sep) end
local function mkdirp(p)
  if win then os.execute('mkdir "' .. p .. '" 2>nul')
  else os.execute("mkdir -p '" .. p .. "'") end
end
local function rmrf(p)
  if win then os.execute('rmdir /s /q "' .. p .. '" 2>nul')
  else os.execute("rm -rf '" .. p .. "'") end
end

local here = (arg[0] or ""):match("^(.*)[/\\][^/\\]+$") or "."
package.path = join(here, "?.lua") .. ";" .. package.path

local tmp = os.getenv("TMPDIR") or os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
local sandbox = join(tmp, "reaper_console_selftest")
rmrf(sandbox)
mkdirp(sandbox)

local passed, failed = 0, 0
local function ok(label, condition, detail)
  if condition then
    passed = passed + 1
    print("  ok   " .. label)
  else
    failed = failed + 1
    print("  FAIL " .. label .. (detail and ("  (" .. tostring(detail) .. ")") or ""))
  end
end
local function eq(label, got, want)
  ok(label, got == want, string.format("got %s, want %s", tostring(got), tostring(want)))
end

_G.REAPER_CONSOLE_SELFTEST = true
local M = assert(dofile(join(here, "reaper_daemon_console.lua")),
                 "panel did not return its selftest table")
local json = require("json")

-- ---------------------------------------------------------------------------
print("atomic_write_json")
-- ---------------------------------------------------------------------------
do
  local path = join(sandbox, "state.json")
  assert(M.atomic_write_json(path, { a = 1 }))
  eq("round trips", M.read_json_file(path).a, 1)

  -- The overwrite is the real test: on Windows the first rename fails because
  -- the destination exists, and only the remove+retry fallback saves it.
  assert(M.atomic_write_json(path, { a = 2, b = "two" }))
  local again = M.read_json_file(path)
  eq("overwrite replaced the file", again.a, 2)
  eq("overwrite kept new keys", again.b, "two")

  local scratch = io.open(path .. ".tmp", "rb")
  ok("no .tmp left behind", scratch == nil)
  if scratch then scratch:close() end

  ok("missing file reads as nil", M.read_json_file(join(sandbox, "nope.json")) == nil)

  local bad = join(sandbox, "bad.json")
  local fh = io.open(bad, "wb"); fh:write("{not json"); fh:close()
  ok("undecodable file reads as nil, not an error", M.read_json_file(bad) == nil)
end

-- ---------------------------------------------------------------------------
print("tail_read")
-- ---------------------------------------------------------------------------
do
  local path = join(sandbox, "events.jsonl")
  local state = { offset = 0, carry = "" }
  local got = {}
  local function collect(line) got[#got + 1] = line end

  local fh = io.open(path, "ab"); fh:write("one\ntwo\n"); fh:close()
  eq("reads both lines", M.tail_read(state, path, collect), 2)
  eq("first line", got[1], "one")
  eq("second line", got[2], "two")

  eq("no new bytes reads nothing", M.tail_read(state, path, collect), 0)

  -- A line caught mid-append must be held in the carry buffer, not decoded as
  -- garbage and not dropped. This is the failure the spike was built to rule
  -- out; forcing it live depends on Windows write timing, so force it here.
  fh = io.open(path, "ab"); fh:write("thr"); fh:close()
  eq("partial line yields nothing yet", M.tail_read(state, path, collect), 0)
  fh = io.open(path, "ab"); fh:write("ee\n"); fh:close()
  eq("partial line completes on the next poll", M.tail_read(state, path, collect), 1)
  eq("torn line reassembled intact", got[3], "three")

  -- A file that shrank was rotated (new session, same path). Reading from the
  -- old offset would splice two sessions into one unparseable line.
  local rotated = io.open(path, "wb"); rotated:write("fresh\n"); rotated:close()
  eq("rotation is detected and re-read", M.tail_read(state, path, collect), 1)
  eq("rotated content", got[4], "fresh")

  eq("absent file is not an error", M.tail_read({ offset = 0, carry = "" },
     join(sandbox, "gone.jsonl"), collect), 0)
end

-- ---------------------------------------------------------------------------
print("apply_event")
-- ---------------------------------------------------------------------------
do
  local blocks = {}
  M.apply_event(blocks, { t = "user", text = "profile the drums" })
  eq("user block", blocks[1].role, "user")

  -- Streaming, then the authoritative final text. The final `text` event must
  -- REPLACE the streamed block, not append to it, or every finished answer
  -- appears twice.
  M.apply_event(blocks, { t = "text_delta", text = "It is " })
  M.apply_event(blocks, { t = "text_delta", text = "a half-time" })
  eq("deltas accumulate into one block", blocks[2].text, "It is a half-time")
  eq("only two blocks so far", #blocks, 2)
  M.apply_event(blocks, { t = "text", text = "It is a half-time feel." })
  eq("final text replaces the stream", blocks[2].text, "It is a half-time feel.")
  eq("still one assistant block", #blocks, 2)
  ok("block is closed", blocks[2].open == false)

  -- A second answer after the first closed must not reopen the old block.
  M.apply_event(blocks, { t = "text_delta", text = "More." })
  eq("a new stream starts a new block", #blocks, 3)

  -- Tool call then result, matched by id.
  M.apply_event(blocks, { t = "tool", id = "t1", name = "mcp__reaper-daemon__measure",
                          input = '{"track":"Bass"}' })
  eq("tool block", blocks[4].role, "tool")
  eq("tool starts as running", blocks[4].verdict, "running")
  M.apply_event(blocks, { t = "tool_result", id = "t1", verdict = "ok", ok = true,
                          text = "LUFS-I -14.2" })
  eq("result attaches to its call", blocks[4].verdict, "ok")
  eq("result text lands on the call block", blocks[4].text, "LUFS-I -14.2")
  eq("a result creates no block of its own", #blocks, 4)

  -- An unmatched result must not crash or invent a block.
  M.apply_event(blocks, { t = "tool_result", id = "nope", verdict = "ok", ok = true })
  eq("orphan result is ignored", #blocks, 4)

  -- An unknown kind from a newer sidecar must not break the view.
  M.apply_event(blocks, { t = "some_future_kind", text = "?" })
  eq("unknown kind ignored", #blocks, 4)

  M.apply_event(blocks, { t = "error", code = "TURN_TIMEOUT", details = "no result" })
  eq("error block", blocks[5].role, "error")
  ok("error text carries the code", blocks[5].text:find("TURN_TIMEOUT", 1, true) ~= nil)
end

do
  -- The cap drops from the FRONT: the newest turn is the one being read.
  local blocks = {}
  for i = 1, 20 do
    M.apply_event(blocks, { t = "user", text = "m" .. i }, 5)
  end
  eq("cap holds", #blocks, 5)
  eq("oldest dropped, newest kept", blocks[5].text, "m20")
  eq("front is the 16th", blocks[1].text, "m16")
end

-- ---------------------------------------------------------------------------
print("formatting")
-- ---------------------------------------------------------------------------
do
  eq("money rounds", M.money(0.12571), "$0.13")
  eq("money floors to a visible minimum", M.money(0.0004), "<$0.01")
  eq("money handles zero", M.money(0), "$0.00")
  eq("money survives nil", M.money(nil), "$0.00")
  eq("duration under a second", M.duration(430), "430ms")
  eq("duration over a second", M.duration(9280), "9.3s")
  eq("short leaves short text alone", M.short("abc", 10), "abc")
  eq("short collapses whitespace", M.short("a\n  b", 10), "a b")
  eq("short truncates to a character count", utf8.len(M.short(string.rep("x", 100), 20)), 20)
  -- Byte slicing would cut a multi-byte character in half and ImGui renders
  -- the orphaned halves as tofu, which reads as corruption in the transcript.
  local wide = string.rep("\u{00e9}", 100)
  eq("truncation counts characters, not bytes", utf8.len(M.short(wide, 20)), 20)
  ok("truncated non-ASCII stays valid UTF-8", utf8.len(M.short(wide, 20)) ~= nil)
  local torn = "abc\xff\xfedef"
  ok("invalid UTF-8 still renders rather than erroring", #M.short(torn, 4) > 0)
end

-- ---------------------------------------------------------------------------
print("freshness")
-- ---------------------------------------------------------------------------
do
  -- Liveness is "the stamp changed recently", never a parsed clock: state.json
  -- is republished every poll (console_sidecar.py:1885), so a frozen stamp is
  -- a dead sidecar even though the file is still perfectly readable.
  local tracker = {}
  eq("first sight is fresh", M.freshness(tracker, "A", 100), 0)
  eq("same stamp ages", M.freshness(tracker, "A", 104), 4)
  eq("a new stamp resets", M.freshness(tracker, "B", 106), 0)
  eq("nil stamp still ages rather than erroring", M.freshness(tracker, nil, 110), 0)
end

-- ---------------------------------------------------------------------------
print("focus envelope")
-- ---------------------------------------------------------------------------
local focus = dofile(join(here, "reaper_focus.lua"))
do
  eq("stopped", focus.transport_name(0), "stopped")
  eq("playing", focus.transport_name(1), "playing")
  eq("paused", focus.transport_name(2), "paused")
  -- Recording arrives as play|record, not as a bare 4.
  eq("recording is the bitmask, not the enum", focus.transport_name(5), "recording")
  eq("bare record bit", focus.transport_name(4), "recording")

  eq("bars count from one", focus.bar_number(0), 1)
  eq("bar 9", focus.bar_number(8), 9)

  ok("a zero-length range is no range", not focus.is_real_range(4.0, 4.0))
  ok("a real range is real", focus.is_real_range(4.0, 8.0))
  ok("nil is no range", not focus.is_real_range(nil, 8.0))

  eq("round trims", focus.round(1.23456789, 3), 1.235)
end

do
  -- A stub REAPER: one selected track, cursor in bar 9, bars 9-16 selected at
  -- 146 BPM, transport rolling, project dirty.
  local tracks = { { name = "guitar-di", guid = "{ABC}", number = 2 } }
  _G.reaper = {
    EnumProjects = function() return 0, "C:\\Users\\wretc\\Documents\\claude-test.rpp" end,
    IsProjectDirty = function() return 1 end,
    CountSelectedTracks = function() return #tracks end,
    CountTracks = function() return 4 end,
    GetSelectedTrack = function(_, i) return tracks[i + 1] end,
    GetTrackGUID = function(t) return t.guid end,
    GetSetMediaTrackInfo_String = function(t) return true, t.name end,
    GetMediaTrackInfo_Value = function(t) return t.number end,
    GetCursorPosition = function() return 13.150684931 end,
    -- 146 BPM, 4/4: one bar is 1.643835s. Bar 9 starts at 13.1506849.
    GetSet_LoopTimeRange = function() return 13.150684931, 26.301369863 end,
    TimeMap2_timeToBeats = function(_, t)
      local bar_seconds = 60.0 / 146.0 * 4
      local measures = math.floor(t / bar_seconds + 1e-9)
      return 0, measures, 0, 0, 4
    end,
    Master_GetTempo = function() return 146.0 end,
    GetPlayState = function() return 1 end,
    GetPlayPosition = function() return 13.150684931 end,
  }

  local env = focus.envelope(0)
  ok("no error", env.error == nil, env.error)
  eq("project name is the basename", env.project_name, "claude-test.rpp")
  ok("dirty flag", env.project_dirty == true)
  eq("selected track name", env.selected_tracks[1].name, "guitar-di")
  eq("selected track guid", env.selected_tracks[1].guid, "{ABC}")
  eq("cursor bar", env.cursor_bar, 9)
  eq("time selection starts at bar 9", env.time_selection.start_bar, 9)
  -- The selection ends at the downbeat of 17. Reporting 17 would send the model
  -- one bar past what he highlighted.
  eq("time selection ends IN bar 16", env.time_selection.end_bar, 16)
  eq("tempo", env.tempo, 146.0)
  eq("transport", env.transport, "playing")

  local line = focus.summary(env)
  ok("summary names the track", line:find("guitar-di", 1, true) ~= nil)
  ok("summary carries the bar", line:find("bar 9", 1, true) ~= nil)
  ok("summary flags unsaved", line:find("unsaved", 1, true) ~= nil)
end

do
  -- A focus read that throws must degrade to a usable envelope, never take the
  -- defer loop down with it: the prompt is worth more than the decoration.
  _G.reaper = {
    EnumProjects = function() error("boom") end,
  }
  local env = focus.envelope(0)
  ok("a throwing REAPER call is captured", env.error ~= nil)
  eq("summary degrades", focus.summary(env), "focus unavailable")
  eq("nil envelope degrades", focus.summary(nil), "no focus")
end

do
  -- No selection at all is a normal state, not an error.
  _G.reaper = {
    EnumProjects = function() return 0, "untitled.rpp" end,
    IsProjectDirty = function() return 0 end,
    CountSelectedTracks = function() return 0 end,
    CountTracks = function() return 0 end,
    GetSelectedTrack = function() return nil end,
    GetCursorPosition = function() return 0 end,
    GetSet_LoopTimeRange = function() return 0, 0 end,
    TimeMap2_timeToBeats = function() return 0, 0, 0, 0, 4 end,
    Master_GetTempo = function() return 120 end,
    GetPlayState = function() return 0 end,
  }
  local env = focus.envelope(0)
  ok("empty project is not an error", env.error == nil, env.error)
  eq("no tracks selected", #env.selected_tracks, 0)
  ok("no time selection reported", env.time_selection == nil)
  ok("summary says so", focus.summary(env):find("no track selected", 1, true) ~= nil)
end

-- ---------------------------------------------------------------------------
print("prompt payload shape")
-- ---------------------------------------------------------------------------
do
  -- The sidecar reads `text` and `focus` (console_sidecar.py:poll_prompts) and
  -- serializes the envelope into the prompt. Prove the envelope survives a
  -- JSON round trip: a NaN or an inf from a REAPER float would poison it.
  _G.reaper = {
    EnumProjects = function() return 0, "x.rpp" end,
    IsProjectDirty = function() return 0 end,
    CountSelectedTracks = function() return 0 end,
    CountTracks = function() return 1 end,
    GetSelectedTrack = function() return nil end,
    GetCursorPosition = function() return 1.5 end,
    GetSet_LoopTimeRange = function() return 0, 0 end,
    TimeMap2_timeToBeats = function() return 0, 0, 0, 0, 4 end,
    Master_GetTempo = function() return 120 end,
    GetPlayState = function() return 0 end,
  }
  local env = focus.envelope(0)
  local encoded = json.encode({ text = "hi", source = "panel", focus = env })
  local decoded = json.decode(encoded)
  eq("text survives", decoded.text, "hi")
  eq("source survives", decoded.source, "panel")
  eq("envelope survives", decoded.focus.project_name, "x.rpp")
  eq("cursor survives", decoded.focus.cursor_seconds, 1.5)
end

rmrf(sandbox)
print(string.format("\n%d passed, %d failed", passed, failed))
os.exit(failed == 0 and 0 or 1)
