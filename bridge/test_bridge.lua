-- Self-check for the bridge's pure/atomic helpers. No live REAPER, and the only
-- shell calls (mkdir/rm of a throwaway temp sandbox) are OS-branched, so it runs
-- identically on macOS, Linux, and Windows CI. The Windows run is the one that
-- proves the atomic-write fix (C1): its rename() can't replace an existing file.
--   Run:  lua bridge/test_bridge.lua

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
local bridge_file = join(here, "reaper_agent_bridge.lua")
local tmp = os.getenv("TMPDIR") or os.getenv("TEMP") or os.getenv("TMP") or "/tmp"
local sandbox = join(tmp, "reaper_bridge_selftest")
mkdirp(join(sandbox, "bridge"))

-- Load the bridge in selftest mode. Resolve its `require "json"` from the real
-- bridge dir (no file copy) while pointing the bridge root at the sandbox; stub
-- only what the load path touches before the selftest seam returns.
package.path = join(here, "?.lua") .. ";" .. package.path
_G.reaper = {
  get_action_context = function() return true, bridge_file end,
  RecursiveCreateDirectory = function(p) mkdirp(p) end,
  EnumerateFiles = function() return nil end,
  time_precise = function() return 0 end,
}
_G.REAPER_AGENT_BRIDGE_DIR = join(sandbox, "bridge")
_G.REAPER_BRIDGE_SELFTEST = true

local B = assert(dofile(bridge_file), "bridge did not return its selftest table")
local json = require("json")

local checks = 0
local function eq(got, want, label)
  assert(got == want, ("%s: got %s, want %s"):format(label, tostring(got), tostring(want)))
  checks = checks + 1
end
local function ok(cond, label)
  assert(cond, label)
  checks = checks + 1
end

-- H4: kHz must scale to Hz, else "1.20 kHz" (1.2) never matches target "1200 Hz".
local pdn = B.parse_display_number
eq(pdn("1.20 kHz"), 1200, "kHz display scales")
eq(pdn("1200 Hz"), 1200, "Hz target")
eq(pdn("80 Hz"), 80, "plain Hz, no false kHz")
eq(pdn("-3.0 dB"), -3.0, "signed dB")
eq(pdn("50 %"), 50, "percent")
eq(pdn("inf"), 1e30, "inf endpoint")
eq(pdn("-inf"), -1e30, "-inf endpoint")
eq(pdn("Bell"), nil, "enum/string rejected")

-- Pro-C 3's Release prints "944.9 ms" low and "1.151 sec" high. Parsing those
-- as 944.9 and 1.151 made the search read the parameter as descending and
-- refuse every real target as out of range. Time scales to ms, frequency to Hz.
eq(pdn("944.9 ms"), 944.9, "ms stays ms")
eq(pdn("1.151 sec"), 1151, "sec scales to ms")
eq(pdn("2.500 sec"), 2500, "sec at the top of Pro-C Release")
eq(pdn("1 s"), 1000, "bare s is seconds")
eq(pdn("500 us"), 0.5, "microseconds scale down")
eq(pdn("500 " .. string.char(194, 181) .. "s"), 0.5, "the real micro sign, not an ascii u")
-- A unit only counts when it is attached to the number, or Pro-C's pan display
-- ("Side") reads as seconds and a 0 dB pan becomes 0 ms of something.
eq(pdn("Mid: 0 dB / Side: 0 dB"), 0, "a later word is not a unit")
eq(pdn("100%, 50% S>M"), 100, "compound stereo-link display takes the first number")
eq(pdn("1/4 Note"), 1, "no unit, no scaling")
eq(pdn("3.50:1"), 3.5, "ratio")

-- numeric_bracket: a parameter that goes non-numeric at one end is still
-- searchable over the rest. Pro-Q 4's band Threshold reads "Auto" at 1.0.
local nb = B.numeric_bracket
local function proq_threshold(n) -- dB above 0.2, "Auto" at the very top
  if n >= 0.999 then return nil end
  return 60 * (n - 1)
end
local lo, hi, lo_num, hi_num = nb(proq_threshold)
eq(lo, 0.0, "live bottom end is kept")
ok(hi < 0.999 and hi > 0.995, "dead top end narrows to just under the boundary")
eq(lo_num, -60, "bottom reads its real number")
ok(hi_num < 0 and hi_num > -0.5, "narrowed top still reads a number")

local function dead_bottom(n)
  if n <= 0.05 then return nil end
  return n * 100
end
local blo, bhi, blo_num, bhi_num = nb(dead_bottom)
ok(blo > 0.05 and blo < 0.06, "dead bottom end narrows upward")
eq(bhi, 1.0, "live top end is kept")
ok(blo_num > 5 and blo_num < 6, "narrowed bottom reads a number")
eq(bhi_num, 100, "top reads its real number")

eq(nb(function() return nil end), nil, "a genuinely non-numeric param is still refused")
eq(nb(function(n) return n end), 0.0, "both ends numeric: no probing, bracket is 0..1")
local _, whole_hi = nb(function(n) return n end)
eq(whole_hi, 1.0, "both ends numeric: top stays 1.0")

-- P1-001: discovery responses use REAPER's real FX GUID and keep the display
-- index separate from the encoded API index used for input FX.
local fx_guid_calls = {}
_G.reaper.TrackFX_GetFXGUID = function(track, api_index)
  fx_guid_calls[#fx_guid_calls + 1] = { track = track, api_index = api_index }
  return "{FX-GUID-" .. tostring(api_index) .. "}"
end
local fxs = B.fx_summary("track-object", 0x1000002, 2, "input", "VST3: Test", {
  parameter_count = 37,
})
eq(fxs.index, 2, "FX summary keeps display index")
eq(fxs.api_index, 0x1000002, "FX summary keeps encoded API index")
eq(fxs.scope, "input", "FX summary keeps scope")
eq(fxs.name, "VST3: Test", "FX summary keeps name")
eq(fxs.guid, "{FX-GUID-16777218}", "FX summary uses real REAPER GUID")
eq(fxs.parameter_count, 37, "FX summary keeps extra fields")
eq(fx_guid_calls[1].track, "track-object", "FX GUID receives the real track")
eq(fx_guid_calls[1].api_index, 0x1000002, "FX GUID receives encoded API index")

-- Stable FX GUIDs are first-class selectors for readback. This must span track
-- and input FX without forcing a caller back to a movable index.
_G.reaper.GetMasterTrack = function() return "master-object" end
_G.reaper.CountTracks = function() return 1 end
_G.reaper.GetTrack = function(_, index) return index == 0 and "track-object" or nil end
_G.reaper.GetTrackGUID = function(track)
  if track == "track-object" then return "{TRACK-GUID}" end
  if track == "master-object" then return "{MASTER-GUID}" end
end
_G.reaper.TrackFX_GetCount = function() return 1 end
_G.reaper.TrackFX_GetRecCount = function() return 1 end
_G.reaper.TrackFX_GetFXName = function(_, api_index)
  return true, api_index >= 0x1000000 and "Input Test" or "Track Test"
end
_G.reaper.TrackFX_GetFXGUID = function(_, api_index)
  return api_index >= 0x1000000 and "{INPUT-FX}" or "{TRACK-FX}"
end
local found_track, found_track_index, found_api_index, found_name, found_scope, found_index =
  B.find_fx({ target_track_guid = "{TRACK-GUID}", fx_guid = "{INPUT-FX}" })
eq(found_track, "track-object", "FX GUID lookup keeps the resolved track")
eq(found_track_index, 1, "FX GUID lookup keeps the track index")
eq(found_api_index, 0x1000000, "FX GUID lookup resolves encoded input-FX index")
eq(found_name, "Input Test", "FX GUID lookup reports the resolved plugin name")
eq(found_scope, "input", "FX GUID lookup reports input scope")
eq(found_index, 0, "FX GUID lookup reports display index")

-- A set result must carry the same stable FX identity. The console cannot
-- safely restore by an index that may now point at a different plugin.
_G.reaper.TrackFX_GetFXGUID = function(_, api_index)
  return "{FX-GUID-" .. tostring(api_index) .. "}"
end
_G.reaper.GetTrackGUID = function(track)
  return track == "track-object" and "{TRACK-GUID}" or nil
end
local set_result = B.set_fx_param_result(
  "track-object", 2, "Track 2", 1, 1, "track", "VST3: Test",
  { normalized_value = 0.64 }, { normalized_value = 0.60 })
eq(set_result.track.guid, "{TRACK-GUID}", "set result carries track GUID")
eq(set_result.fx.guid, "{FX-GUID-1}", "set result carries FX GUID")
eq(set_result.fx.api_index, 1, "set result carries FX API index")
eq(set_result.parameter.before.normalized_value, 0.64,
   "set result carries exact before value")
eq(set_result.parameter.after.normalized_value, 0.60,
   "set result carries exact after value")

-- Live GUID smoke testing exposed a Lua truthiness trap in batch results:
-- `ok and nil or tostring(data)` always selected tostring(data), so successful
-- subcommands carried a fake `error: table: ...`. Success and failure fields
-- must be mutually exclusive.
local br_ok = B.batch_result(3, "scan_fx", true, { fx_count = 1 })
eq(br_ok.ok, true, "batch success stays successful")
eq(br_ok.data.fx_count, 1, "batch success carries data")
eq(br_ok.error, nil, "batch success omits error")
local br_fail = B.batch_result(4, "get_fx_parameters", false, "NO_FX: missing")
eq(br_fail.ok, false, "batch failure stays failed")
eq(br_fail.data, nil, "batch failure omits data")
eq(br_fail.error, "NO_FX: missing", "batch failure carries error")

-- C1: writing the same path twice must succeed (this is what froze on Windows).
local p = join(sandbox, "aw_test.json")
B.atomic_write_json(p, { a = 1 })
B.atomic_write_json(p, { a = 2 })
local f = assert(io.open(p, "rb")); local body = f:read("*a"); f:close()
eq(json.decode(body).a, 2, "atomic_write_json overwrites in place")

-- M1: chunk splicer. Classify lines the way splice_fx_chain does so the tests
-- assert structural truth (is the new FX a DIRECT child of FXCHAIN?), not string
-- positions. balanced() + container_depth_of() are what a corrupt splice breaks.
local function classify(line)
  local t = line:match("^%s*(.-)%s*$")
  local opens = t:sub(1, 1) == "<"
  return opens, opens and t:sub(-1) == ">", t == ">"
end
local function balanced(lines)
  local depth = 0
  for _, line in ipairs(lines) do
    local opens, single, closes = classify(line)
    if opens and not single then depth = depth + 1 elseif closes then depth = depth - 1 end
    if depth < 0 then return false end
  end
  return depth == 0
end
local function container_depth_of(lines, needle)  -- depth of blocks enclosing the line
  local depth = 0
  for _, line in ipairs(lines) do
    if line:find(needle, 1, true) then return depth end
    local opens, single, closes = classify(line)
    if opens and not single then depth = depth + 1 elseif closes then depth = depth - 1 end
  end
  return nil
end

local splice = B.splice_fx_chain
local fxbody = { '<VST "SENTINEL_NEW"', "  newdata", ">" }

-- existing FXCHAIN: new FX lands as a direct child (depth 2: TRACK>FXCHAIN)
local c1 = B.split_lines(table.concat({
  "<TRACK", 'NAME "Drums"', "<FXCHAIN", '<VST "Existing"', "  data", ">", ">", ">",
}, "\n"))
local m1 = splice(c1, fxbody, "FXCHAIN")
ok(m1 and balanced(m1), "splice into existing FXCHAIN stays balanced")
eq(container_depth_of(m1, "SENTINEL_NEW"), 2, "new FX is a direct child of FXCHAIN")

-- no FX yet: a fresh FXCHAIN is wrapped as a child of TRACK
local c2 = B.split_lines(table.concat({ "<TRACK", 'NAME "Bass"', ">" }, "\n"))
local m2 = splice(c2, fxbody, "FXCHAIN")
ok(m2 and balanced(m2), "wrap-new stays balanced")
eq(container_depth_of(m2, "<FXCHAIN"), 1, "wrapped FXCHAIN is a child of TRACK")
eq(container_depth_of(m2, "SENTINEL_NEW"), 2, "new FX nested under the wrapped FXCHAIN")

-- single-line node '<INLINE foo>' (net-zero) is exactly what skewed the old
-- line-counter: it must NOT move the splice point off FXCHAIN.
local c3 = B.split_lines(table.concat({
  "<TRACK", "<FXCHAIN", '<VST "A"', "  data", "<INLINE foo>", "  more", ">", ">", ">",
}, "\n"))
local m3 = splice(c3, fxbody, "FXCHAIN")
ok(m3 and balanced(m3), "single-line node: merged stays balanced")
eq(container_depth_of(m3, "SENTINEL_NEW"), 2, "single-line node didn't skew the splice")

-- malformed chunk (no track close) fails loud rather than corrupting
local m4, err4 = splice(B.split_lines("DATA only\nMORE"), fxbody, "FXCHAIN")
ok(m4 == nil and err4 == "CHUNK_NO_TRACK_CLOSE", "malformed chunk returns an error code")

-- Fix 1 (2026-07-02 review): startup requeue triage. A stranded processing/
-- file must NOT re-run when it already executed (reply/archive exists) or when
-- it is stale (its CLI reported TIMEOUT long ago); a fresh crash still re-runs.
local rd = B.requeue_decision
local pca = B.parse_created_at
local tnow = os.time()
local fresh_cmd = '{"id":"a","created_at":"'
  .. os.date("%Y-%m-%dT%H:%M:%S", tnow - 60) .. '-04:00"}'
local stale_cmd = '{"id":"b","created_at":"'
  .. os.date("%Y-%m-%dT%H:%M:%S", tnow - 3600) .. '-04:00"}'
eq(rd(fresh_cmd, false, false, tnow), "requeue", "fresh crash re-runs")
eq(rd(stale_cmd, false, false, tnow), "discard", "stale command discarded")
eq(rd(fresh_cmd, true, false, tnow), "skip", "existing outbox reply skips requeue")
eq(rd(stale_cmd, false, true, tnow), "skip", "archive entry skips requeue")
eq(rd('{"id":"c"}', false, false, tnow), "requeue", "unknown age keeps old behavior")
eq(rd(nil, false, false, tnow), "requeue", "unreadable file keeps old behavior")
ok(pca(fresh_cmd) ~= nil, "created_at parses")
ok(math.abs(pca(fresh_cmd) - (tnow - 60)) <= 1, "created_at epoch is faithful")
eq(pca('{"id":"c"}'), nil, "missing created_at is nil")

-- Fix 10 (2026-07-02 review): error-code extraction vs Windows drive letters.
-- "C:\...\bridge.lua:559: NO_FX: x" used to decode as code "C".
local ecf = B.error_code_from
eq(ecf("C:\\Users\\d\\bridge.lua:559: NO_FX: no such fx", "COMMAND_FAILED"),
   "NO_FX", "Windows path does not eat the code")
eq(ecf("/u/bridge.lua:12: BAD_JSON: eof", "COMMAND_FAILED"),
   "BAD_JSON", "POSIX path still decodes")
eq(ecf("D:\\x.lua:3: AUTH_FAILED: missing token", "BATCH_FAILED"),
   "AUTH_FAILED", "underscore codes decode")
eq(ecf("something exploded with no code", "COMMAND_FAILED"),
   "COMMAND_FAILED", "no code falls back")

-- Fix 13 (2026-07-02 review): command.id names the outbox reply file, so a
-- hostile/malformed id must fall back to the inbox filename stem.
local sid = B.safe_id
eq(sid("agent-2026-07-02T10-00-00-abcd", "fb"), "agent-2026-07-02T10-00-00-abcd",
   "normal id kept")
eq(sid("../bridge/heartbeat", "fb"), "fb", "path traversal rejected")
eq(sid("a/b", "fb"), "fb", "separator rejected")
eq(sid("x..y", "fb"), "fb", "dot-dot anywhere rejected")
eq(sid(42, "fb"), "fb", "non-string rejected")
eq(sid(nil, "fb"), "fb", "missing id falls back")

-- Capture provenance is a machine-readable contract. Callers must be able to
-- distinguish a verified isolated track from a master/full-mix fallback without
-- parsing a human-facing note.
local cp = B.capture_provenance
local isolated = cp(true, false)
eq(isolated.capture_scope, "isolated_track", "isolated capture scope")
eq(isolated.isolation_verified, true, "isolated capture verified")

local full_mix = cp(false, false)
eq(full_mix.capture_scope, "full_mix", "item-track fallback scope")
eq(full_mix.isolation_verified, false, "full-mix fallback is unverified")

local master = cp(false, true)
eq(master.capture_scope, "master_output", "master capture scope")
eq(master.isolation_verified, false, "master output is not an isolated track")

-- Fix 12 (2026-07-02 review): render locks reclaim after a generous bound.
local lv = B.lock_verdict
local lnow = os.time()
eq(lv(nil, lnow), nil, "no lock proceeds")
ok(lv({ started = lnow - 30, busy = "none" }, lnow) ~= nil, "fresh lock refuses")
eq(lv({ started = lnow - 120, busy = "none" }, lnow), nil, "stale lock reclaimed")
ok(lv({ started = lnow - 3600, busy = "render" }, lnow) ~= nil,
   "hour-old render lock still refuses (long renders are real)")
eq(lv({ started = lnow - 7 * 3600, busy = "render" }, lnow), nil,
   "ancient render lock reclaimed (power loss no longer bricks the bridge)")

-- Render-dialog-hang fix: force both required renderclosewhendone preferences
-- for a render, restore the user's setting after, and fail closed when SWS
-- cannot guarantee that no first-run modal will block the bridge.
local ear = B.ensure_render_autoclose
local rar = B.restore_render_autoclose
local rpe = B.render_preferences_error

-- No SWS -> cannot force auto-close; degrade to "not guaranteed" (caller warns).
_G.reaper.SNM_GetIntConfigVar = nil
_G.reaper.SNM_SetIntConfigVar = nil
local tok = ear()
eq(tok.guaranteed, false, "no SWS -> not guaranteed")
ok(tok.restore == nil, "no SWS -> nothing to restore")
ok(tok.reason:find("Automatically close when finished", 1, true) ~= nil,
   "no SWS remediation names auto-close")
ok(tok.reason:find("Save render statistics", 1, true) ~= nil,
   "no SWS remediation names render statistics")
ok(rpe(tok):find("RENDER_PREFERENCES_UNSAFE", 1, true) ~= nil,
   "no SWS capture refuses before opening the render window")
eq(rar(tok), true, "no SWS restore is a harmless no-op")

-- SWS present: back the config var with a table so get/set round-trips.
local store = { renderclosewhendone = 2097156 } -- real on-disk value: bit0 clear (auto-close OFF)
_G.reaper.SNM_GetIntConfigVar = function(name, errval)
  local v = store[name]; if v == nil then return errval end; return v
end
_G.reaper.SNM_SetIntConfigVar = function(name, val) store[name] = val; return true end

-- bit0 clear -> force it on, return the original for restore, preserve other bits.
tok = ear()
eq(tok.guaranteed, true, "SWS + bit clear -> guaranteed")
eq(tok.restore, 2097156, "original value captured for restore")
eq(store.renderclosewhendone & 1, 1, "auto-close bit forced on for the render")
eq(store.renderclosewhendone, 2097157, "only bit0 flipped, other bits preserved")
rar(tok)
eq(store.renderclosewhendone, 2097156, "user's setting restored after render")

-- A fresh REAPER 7.75+ install can already auto-close but still has the new
-- save-render-statistics preference off. RENDER_STATS then opens a modal after
-- the render and blocks the bridge. Force that bit for the same bounded render.
store.renderclosewhendone = 5
tok = ear()
eq(tok.guaranteed, true, "fresh config -> render preferences guaranteed")
eq(tok.restore, 5, "fresh config captured for restore")
eq(store.renderclosewhendone, 2097157,
   "save-render-statistics and auto-close enabled together")
rar(tok)
eq(store.renderclosewhendone, 5, "fresh render preferences restored")

-- A failed SWS setter must never be reported as safe.
store.renderclosewhendone = 5
_G.reaper.SNM_SetIntConfigVar = function() return false end
tok = ear()
eq(tok.guaranteed, false, "failed render preference write is not guaranteed")
ok(rpe(tok):find("could not enable", 1, true) ~= nil,
   "failed render preference write refuses capture")

-- A failed exact restore is surfaced to the caller instead of silently
-- leaving the user's preferences changed.
store.renderclosewhendone = 5
_G.reaper.SNM_SetIntConfigVar = function(name, val)
  if val == 5 then return false end
  store[name] = val
  return true
end
tok = ear()
eq(tok.guaranteed, true, "preference write succeeds before restore failure")
local restored, restore_error = rar(tok)
eq(restored, false, "failed preference restore is reported")
ok(restore_error:find("restore", 1, true) ~= nil,
   "failed preference restore explains the invariant violation")

_G.reaper.SNM_SetIntConfigVar = function(name, val) store[name] = val; return true end

-- Both required bits already set -> leave the setting alone, nothing to restore.
store.renderclosewhendone = 2097157
tok = ear()
eq(tok.guaranteed, true, "SWS + required bits already set -> guaranteed")
ok(tok.restore == nil, "render preferences already safe -> no restore needed")
eq(store.renderclosewhendone, 2097157, "already-on value untouched")

-- config var missing (SNM returns the error sentinel) -> degrade, don't touch.
store.renderclosewhendone = nil
tok = ear()
eq(tok.guaranteed, false, "missing config var -> not guaranteed")
ok(tok.restore == nil, "missing config var -> nothing to restore")

-- P2-001: snapshot shape validation fails closed on anything malformed.
local sv = B.snapshot_validate
local good = {
  schema_version = 1,
  track = { guid = "{TRACK-A}", name = "Kick" },
  values = {
    volume = 0.5, pan = 0.0,
    fx = {
      { guid = "{FX-1}", api_index = 0, scope = "track", name = "EQ",
        enabled = true,
        parameters = { { index = 17, name = "Gain", normalized_value = 0.5 } } },
    },
  },
}
eq(sv(good), nil, "valid snapshot accepted")
ok(sv(nil) ~= nil, "nil snapshot rejected")
ok(sv({ schema_version = 2, track = good.track, values = good.values }) ~= nil,
   "unknown schema_version rejected")
ok(sv({ schema_version = 1, values = good.values }) ~= nil,
   "missing track.guid rejected")
ok(sv({ schema_version = 1, track = good.track }) ~= nil, "missing values rejected")
ok(sv({ schema_version = 1, track = good.track,
        values = { fx = { { name = "no guid" } } } }) ~= nil,
   "fx entry without guid rejected")
ok(sv({ schema_version = 1, track = good.track,
        values = { fx = { { guid = "{FX-1}",
                            parameters = { { index = 1 } } } } } }) ~= nil,
   "parameter without normalized_value rejected")

-- P2-001: restore planning restores what resolves, reports what does not, and
-- refuses a snapshot taken from a different track.
local rp = B.restore_plan
local live = {
  track_guid = "{TRACK-A}",
  fx_by_guid = { ["{FX-1}"] = { api_index = 5 } },
}
local plan = rp(good, live)
eq(#plan.unrestored, 0, "all snapshot state resolves")
eq(plan.ops[1].kind, "volume", "volume restore planned")
eq(plan.ops[1].value, 0.5, "raw D_VOL value round-trips")
eq(plan.ops[2].kind, "pan", "pan restore planned")
eq(plan.ops[3].kind, "fx_enabled", "fx enabled restore planned")
eq(plan.ops[3].api_index, 5, "restore targets the LIVE api index, not the recorded one")
eq(plan.ops[4].kind, "fx_param", "parameter restore planned")
eq(plan.ops[4].parameter_index, 17, "parameter index carried")
eq(plan.ops[4].value, 0.5, "parameter normalized value carried")

local missing_fx = rp(good, { track_guid = "{TRACK-A}", fx_by_guid = {} })
eq(#missing_fx.ops, 2, "volume and pan still restore when the FX is gone")
eq(#missing_fx.unrestored, 1, "missing FX is reported, not silently dropped")
eq(missing_fx.unrestored[1].reason, "FX_NOT_FOUND", "missing FX carries a typed reason")

local wrong_track, wrong_err = rp(good, { track_guid = "{TRACK-B}", fx_by_guid = {} })
ok(wrong_track == nil and wrong_err:find("SNAPSHOT_TRACK_MISMATCH", 1, true),
   "snapshot for another track refuses to plan")

-- A snapshot with no volume/pan recorded must not invent writes for them.
local sparse = {
  schema_version = 1,
  track = { guid = "{TRACK-A}" },
  values = { fx = {} },
}
eq(sv(sparse), nil, "sparse snapshot is valid")
local sparse_plan = rp(sparse, live)
eq(#sparse_plan.ops, 0, "nothing recorded, nothing written")
eq(#sparse_plan.unrestored, 0, "nothing recorded, nothing to report")

-- P2-002: preview state verdicts drive the whole lifecycle (active refusal,
-- token gating, expiry recovery).
local psv = B.preview_state_verdict
local pstate = { preview_token = "pv-1", expires_epoch = 1000 }
eq(psv(nil, nil, 500), "none", "no state file means no preview")
eq(psv({}, nil, 500), "none", "state without a token is no preview")
eq(psv(pstate, nil, 500), "active", "live preview with no token supplied is active")
eq(psv(pstate, "pv-1", 500), "active", "matching token is active")
eq(psv(pstate, "pv-2", 500), "token_mismatch", "wrong token is typed, not ignored")
eq(psv(pstate, "pv-1", 1001), "expired", "past expires_epoch is expired")
eq(psv(pstate, "pv-2", 1001), "expired", "expiry outranks token mismatch (restore first)")

-- P2-002: every identity field the diagnosis supplied must still match, or
-- the preview refuses with STALE_IDENTITY and mutates nothing.
local ptv = B.preview_target_verdict
local target = {
  track_guid = "{T}", track_name = "Kick",
  fx_guid = "{F}", fx_index = 2, fx_scope = "track", fx_name = "EQ",
  parameter_index = 17, parameter_name = "Gain",
}
local live_ok = {
  track_name = "Kick",
  fx = { index = 2, scope = "track", name = "EQ" },
  parameter_name = "Gain",
}
eq(ptv(target, live_ok), nil, "matching identities pass")
ok(ptv(target, { track_name = "Kick Copy", fx = live_ok.fx, parameter_name = "Gain" })
     :find("STALE_IDENTITY", 1, true),
   "renamed track refuses")
ok(ptv(target, { track_name = "Kick", fx = nil }):find("STALE_IDENTITY", 1, true),
   "deleted FX refuses")
ok(ptv(target, { track_name = "Kick",
                 fx = { index = 3, scope = "track", name = "EQ" },
                 parameter_name = "Gain" }):find("STALE_IDENTITY", 1, true),
   "moved FX refuses")
ok(ptv(target, { track_name = "Kick",
                 fx = { index = 2, scope = "input", name = "EQ" },
                 parameter_name = "Gain" }):find("STALE_IDENTITY", 1, true),
   "scope change refuses")
ok(ptv(target, { track_name = "Kick",
                 fx = { index = 2, scope = "track", name = "Compressor" },
                 parameter_name = "Gain" }):find("STALE_IDENTITY", 1, true),
   "renamed FX refuses")
ok(ptv(target, { track_name = "Kick", fx = live_ok.fx, parameter_name = "Q" })
     :find("STALE_IDENTITY", 1, true),
   "renamed parameter refuses")
local volume_target = { track_guid = "{T}", track_name = "Kick" }
eq(ptv(volume_target, { track_name = "Kick" }), nil,
   "track-level target needs no FX identity")

-- P2-002: commit restores the baseline value from the snapshot, then
-- re-applies inside one undo block; the baseline lookup is pure.
local btv = B.baseline_target_value
local snap = {
  values = {
    volume = 0.5, pan = -0.1,
    fx = {
      { guid = "{F}", enabled = true,
        parameters = { { index = 17, normalized_value = 0.44 } } },
    },
  },
}
eq(btv(snap, {}, "set_track_volume"), 0.5, "volume baseline from raw D_VOL")
eq(btv(snap, {}, "set_track_pan"), -0.1, "pan baseline")
eq(btv(snap, { fx_guid = "{F}" }, "set_fx_bypass"), true, "bypass baseline is enabled state")
eq(btv(snap, { fx_guid = "{F}", parameter_index = 17 }, "set_fx_param"), 0.44,
   "parameter baseline by fx guid + index")
local missing_param, mp_err = btv(snap, { fx_guid = "{F}", parameter_index = 3 }, "set_fx_param")
ok(missing_param == nil and mp_err:find("SNAPSHOT_MISSING_TARGET", 1, true),
   "unrecorded parameter refuses to commit")
local missing_fx2, mf_err = btv(snap, { fx_guid = "{GONE}" }, "set_fx_bypass")
ok(missing_fx2 == nil and mf_err:find("SNAPSHOT_MISSING_TARGET", 1, true),
   "unrecorded FX refuses to commit")

-- P3-002: expected_capture_scope mirrors command_capture_track_audio's
-- isolate decision (isolate = non-master with zero media items).
local ecs = B.expected_capture_scope
eq(ecs(true, 0), "master_output", "master is master_output regardless of items")
eq(ecs(true, 5), "master_output", "master with items still master_output")
eq(ecs(false, 0), "isolated_track", "item-less routing track isolates")
eq(ecs(false, nil), "isolated_track", "nil item count treated as zero")
eq(ecs(false, 3), "full_mix", "item track renders the full mix")

-- P3-002: preflight verdict covers every gate combination.
local pv = B.preflight_verdict
local v = pv(false, true, true)
eq(v.capture_allowed, false, "risk gate off blocks capture")
eq(v.blockers[1].code, "capture_gated", "risk gate blocker is typed")
eq(#v.warnings, 0, "autoclose on: no warning")
v = pv(true, true, true)
eq(v.capture_allowed, true, "all green allows capture")
eq(#v.blockers, 0, "all green: no blockers")
eq(#v.warnings, 0, "all green: no warnings")
v = pv(true, true, false)
eq(v.capture_allowed, true, "SWS can force autoclose: allowed")
eq(#v.warnings, 0, "SWS can force autoclose: no hang warning")
v = pv(true, false, nil)
eq(v.capture_allowed, false, "no SWS fails closed before capture")
eq(v.blockers[1].code, "render_preferences_unavailable",
   "no SWS reports the render preference blocker")
v = pv(true, true, nil)
eq(v.capture_allowed, false, "unreadable render preferences fail closed")
eq(v.blockers[1].code, "render_preferences_unavailable",
   "unreadable render preferences report a blocker")
v = pv(false, false, nil)
eq(v.capture_allowed, false, "gated + no SWS: blocked")
ok(#v.blockers == 2 and #v.warnings == 0, "gated + no SWS: both blockers")

-- offlineinact (2026-09-01). REAPER unloading media when it loses focus makes
-- every backgrounded read see unloaded sources -- and EVERY bridge command is
-- backgrounded, because the CLI, the MCP server and the console sidecar are
-- all other processes. It warns rather than blocks: it is proven to affect
-- reads, not proven to break a render.
v = pv(true, true, true, true)
eq(v.capture_allowed, true, "offline-when-inactive warns, it does not block")
eq(v.warnings[1].code, "media_offline_when_inactive",
   "offline-when-inactive warning is typed")
v = pv(true, true, true, false)
eq(#v.warnings, 0, "preference off: no warning")
-- Unreadable must not masquerade as off. nil is 'we do not know', and a
-- reassuring silence there is exactly the failure this field exists to stop.
v = pv(true, true, true, nil)
eq(#v.warnings, 0, "unknown preference stays silent rather than guessing")
-- The warning survives alongside a blocker; it is not swallowed by one.
v = pv(false, true, true, true)
eq(v.capture_allowed, false, "a blocker still blocks with the warning present")
eq(v.warnings[1].code, "media_offline_when_inactive",
   "the warning is reported even when capture is blocked")

-- Folder subtree resolution. A folder parent has zero media items, so it takes
-- the item-less isolation path; soloing it ALONE mutes the children that feed
-- it and the bus renders digital silence at every cursor position. Measured on
-- god-knows.rpp 2026-08-11: "Geets" (a folder over two guitar takes) captured
-- RMS -156 dB at bar 6 and byte-identically at bar 44.
local fd = B.folder_descendants

-- The real project that exposed this: Geets opens a folder over tracks 2 and 3,
-- track 3 closes it; Drum Buss opens over 6..13, track 13 closes it.
local god_knows = { 1, 0, -1, 0, 1, 0, 0, 0, 0, 0, 0, 0, -1 }
local geets = fd(god_knows, 1)
eq(#geets, 2, "Geets has two children")
eq(geets[1], 2, "first guitar take")
eq(geets[2], 3, "second guitar take, which also closes the folder")

local drums = fd(god_knows, 5)
eq(#drums, 8, "Drum Buss holds eight children")
eq(drums[1], 6, "Drum Buss starts at Kick")
eq(drums[8], 13, "Drum Buss ends at the track that closes it")

-- Not a folder: an ordinary track and a closing track own nobody.
eq(#fd(god_knows, 2), 0, "an ordinary track has no descendants")
eq(#fd(god_knows, 3), 0, "a closing track has no descendants")
eq(#fd(god_knows, 4), 0, "a standalone track has no descendants")

-- Nesting: one closer can shut several levels at once, which is why the walk
-- accumulates depth instead of counting a single matching close.
local nested = { 1, 1, 0, -2, 0 }
local outer = fd(nested, 1)
eq(#outer, 3, "outer folder owns the inner folder and everything in it")
eq(outer[3], 4, "the multi-level closer belongs to the outer folder")
eq(#fd(nested, 2), 2, "inner folder owns only its own children")
ok(fd(nested, 2)[2] == 4, "the shared closer also ends the inner folder")

-- A folder left open at the end of the project must not run off the array.
local unterminated = { 1, 0, 0 }
eq(#fd(unterminated, 1), 2, "an unclosed folder stops at the last track")
eq(#fd({}, 1), 0, "an empty project is not an error")
eq(#fd(god_knows, 99), 0, "an out-of-range index is not an error")

-- The provenance note must say WHICH kind of isolation happened. A folder
-- capture includes the children's FX by definition; a Kontakt-style routing
-- stem does not have children at all.
local folder_capture = cp(true, false, 2)
eq(folder_capture.capture_scope, "isolated_track", "folder capture is isolated")
eq(folder_capture.isolation_verified, true, "folder capture is verified")
eq(folder_capture.folder_children, 2, "folder capture reports its child count")
ok(folder_capture.note:find("FOLDER", 1, true) ~= nil,
   "folder capture note names the folder case")
ok(cp(true, false, 0).note:find("Kontakt", 1, true) ~= nil,
   "a childless routing stem keeps the original note")

-- MIDI velocity editing. Notes are addressed by (ppq, pitch) precisely so a
-- request built against a stale read fails loudly instead of writing a
-- velocity onto whatever note happens to sit at that index now.
local vp = B.velocity_plan
local take = {
  { index = 0, ppq = 0, pitch = 24, velocity = 127 },
  { index = 1, ppq = 0, pitch = 54, velocity = 127 },
  { index = 2, ppq = 480, pitch = 24, velocity = 127 },
}

local clean = vp(take, { { ppq = 0, pitch = 24, velocity = 112 },
                         { ppq = 480, pitch = 24, velocity = 107 } })
eq(#clean.plan, 2, "both requested notes resolve")
eq(#clean.missing, 0, "nothing missing")
eq(clean.plan[1].index, 0, "the plan carries the take index, not the row number")
eq(clean.plan[1].from, 127, "the plan records what the note was")
eq(clean.plan[2].index, 2, "the second row resolves to its own note")

-- The whole point of position addressing: a note that moved is a miss, and a
-- miss names the row so the caller can see WHICH note went.
local moved = vp(take, { { ppq = 0, pitch = 24, velocity = 112 },
                         { ppq = 481, pitch = 24, velocity = 107 } })
eq(#moved.plan, 1, "the note that is still there still resolves")
eq(#moved.missing, 1, "the moved note is a miss, not a silent skip")
eq(moved.missing[1].row, 2, "the miss names its row")
eq(moved.missing[1].ppq, 481, "the miss names the tick it looked at")

-- Pitch is half the key: same tick, wrong drum, is a miss too.
eq(#vp(take, { { ppq = 0, pitch = 26, velocity = 100 } }).missing, 1,
   "the right tick on the wrong pitch is still a miss")

-- Stacked notes are refused, not picked between. Writing to one of two notes
-- the caller never distinguished is a silent wrong answer.
local stacked = { { index = 0, ppq = 0, pitch = 24, velocity = 127 },
                  { index = 1, ppq = 0, pitch = 24, velocity = 90 } }
local amb = vp(stacked, { { ppq = 0, pitch = 24, velocity = 110 } })
eq(#amb.plan, 0, "a stacked pair produces no plan")
eq(#amb.ambiguous, 1, "a stacked pair is reported as ambiguous")

-- Two rows aiming at one note is the same failure from the other direction.
local twice = vp(take, { { ppq = 0, pitch = 24, velocity = 110 },
                         { ppq = 0, pitch = 24, velocity = 96 } })
eq(#twice.plan, 1, "the first row claims the note")
eq(#twice.ambiguous, 1, "the second row is refused rather than overwriting it")

-- MIDI velocity 0 is a note-off, not a quiet note.
local bad = vp(take, { { ppq = 0, pitch = 24, velocity = 0 },
                       { ppq = 0, pitch = 54, velocity = 128 },
                       { ppq = 480, pitch = 24, velocity = "loud" } })
eq(#bad.invalid, 3, "0, 128 and a non-number are all refused")
eq(#bad.plan, 0, "an out-of-range velocity contributes nothing to the plan")

eq(#vp(take, {}).plan, 0, "an empty request is not an error")
eq(#vp({}, { { ppq = 0, pitch = 24, velocity = 100 } }).missing, 1,
   "an empty take misses everything rather than erroring")

-- The summary is what makes a 400-note edit readable, so it has to be right
-- about the numbers it collapses.
local vs = B.velocity_summary
local summary = vs({
  { pitch = 26, velocity = 100 },
  { pitch = 24, velocity = 112 },
  { pitch = 24, velocity = 108 },
  { pitch = 24, velocity = 107 },
})
eq(#summary, 2, "one row per pitch")
eq(summary[1].pitch, 24, "rows are sorted by pitch, not by first appearance")
eq(summary[1].count, 3, "the count is the number of hits on that pitch")
eq(summary[1].min, 107, "min")
eq(summary[1].max, 112, "max")
eq(summary[1].mean, 109, "mean")
eq(summary[2].count, 1, "a single hit summarises as itself")
eq(summary[2].min, summary[2].max, "and its min equals its max")
eq(#vs({}), 0, "an empty take summarises to nothing")

-- position_plan: the timing engine's write path. Moving notes rewrites the very
-- key notes are addressed by, so this bucketing carries a hazard velocity
-- writes do not, and the destination-collision cases below are the point.
local pp = B.position_plan
local function take(...)
  local out = {}
  for i, n in ipairs({ ... }) do
    out[i] = { index = i - 1, ppq = n[1], pitch = n[2], end_ppq = n[3] or (n[1] + 120),
               velocity = n[4] or 100 }
  end
  return out
end

local base = take({ 0, 24 }, { 480, 24 }, { 960, 38 })

local r = pp(base, {
  { ppq = 0,   pitch = 24, new_ppq = 5,   new_end_ppq = 125 },
  { ppq = 480, pitch = 24, new_ppq = 474, new_end_ppq = 594 },
})
eq(#r.plan, 2, "both addressable notes resolve")
eq(#r.missing + #r.ambiguous + #r.invalid + #r.collisions, 0, "and nothing else fires")
eq(r.plan[1].index, 0, "the plan carries the take index, not the row number")
eq(r.plan[1].to_ppq, 5, "the requested start is kept")
eq(r.plan[1].to_end_ppq, 125, "and so is the requested end")
eq(r.plan[2].ppq, 480, "the source key is kept so the caller can report the move")

-- A note the take does not hold is missing, not silently skipped: the caller
-- refuses the whole pass on it, because a partial timing pass drifts in and
-- out of feel.
r = pp(base, { { ppq = 7, pitch = 24, new_ppq = 8, new_end_ppq = 128 } })
eq(#r.missing, 1, "an unmatched source lands in missing")
eq(#r.plan, 0, "and contributes nothing to the plan")

-- Two notes stacked on one tick and pitch cannot be told apart, so neither is
-- guessed at. Same stance as velocity_plan.
local stacked = take({ 0, 24 }, { 0, 24 })
r = pp(stacked, { { ppq = 0, pitch = 24, new_ppq = 10, new_end_ppq = 130 } })
eq(#r.ambiguous, 1, "a doubled source tick+pitch is ambiguous")
eq(#r.plan, 0, "and is never moved on a guess")

-- Addressing the same note twice is the caller contradicting itself.
r = pp(base, {
  { ppq = 0, pitch = 24, new_ppq = 5,  new_end_ppq = 125 },
  { ppq = 0, pitch = 24, new_ppq = 9,  new_end_ppq = 129 },
})
eq(#r.ambiguous, 1, "the second row addressing one note is ambiguous")
eq(#r.plan, 1, "the first row still planned")

-- Geometry the take cannot hold.
r = pp(base, { { ppq = 0, pitch = 24, new_ppq = -1, new_end_ppq = 100 } })
eq(#r.invalid, 1, "a negative destination is invalid")
r = pp(base, { { ppq = 0, pitch = 24, new_ppq = 100, new_end_ppq = 100 } })
eq(#r.invalid, 1, "a zero-length note is invalid")
r = pp(base, { { ppq = 0, pitch = 24, new_ppq = 100, new_end_ppq = 50 } })
eq(#r.invalid, 1, "an end before its start is invalid")
r = pp(base, { { ppq = 0, pitch = 24, new_ppq = nil, new_end_ppq = 50 } })
eq(#r.invalid, 1, "a missing destination is invalid")

-- The collision guard. Two moved notes converging on one tick would leave a
-- take no later read can address, which is exactly how a timing pass poisons
-- the next one.
r = pp(base, {
  { ppq = 0,   pitch = 24, new_ppq = 240, new_end_ppq = 360 },
  { ppq = 480, pitch = 24, new_ppq = 240, new_end_ppq = 360 },
})
eq(#r.collisions, 1, "two moved notes landing on one tick+pitch collide")

-- A moved note landing on a note the request left alone is the same hazard
-- wearing the other face, and is caught even though no row mentions the victim.
r = pp(base, { { ppq = 0, pitch = 24, new_ppq = 480, new_end_ppq = 600 } })
eq(#r.collisions, 1, "moving onto an untouched note collides")

-- Two untouched notes already stacked on one tick and pitch are a pre-existing
-- condition, not a hazard this request created. A write that leaves them alone
-- must not be refused over them: a take carrying stacked duplicates would
-- otherwise reject every timing pass, including ones that never go near them.
local with_stack = take({ 0, 24 }, { 0, 24 }, { 480, 24 }, { 960, 38 })
r = pp(with_stack, { { ppq = 960, pitch = 38, new_ppq = 950, new_end_ppq = 1070 } })
eq(#r.collisions, 0, "an untouched stacked pair never collides with itself")
eq(#r.plan, 1, "and the unrelated move plans normally")

-- But the stack is still a protected destination: a moved note landing on it
-- collides, once per note it would bury.
r = pp(with_stack, { { ppq = 480, pitch = 24, new_ppq = 0, new_end_ppq = 120 } })
eq(#r.collisions, 2, "moving onto a stacked pair collides with each buried note")
eq(#r.plan, 1, "the move itself still plans; the caller refuses on the collisions")

-- Same tick, different pitch is a chord, not a collision: a kick and a crash
-- share the downbeat on every heavy record ever made.
r = pp(base, { { ppq = 960, pitch = 38, new_ppq = 0, new_end_ppq = 120 } })
eq(#r.collisions, 0, "a different pitch on the same tick is a chord, not a collision")
eq(#r.plan, 1, "and it plans normally")

-- Swapping two notes past each other is legal: neither destination is occupied
-- once both moves are taken together.
r = pp(base, {
  { ppq = 0,   pitch = 24, new_ppq = 480, new_end_ppq = 600 },
  { ppq = 480, pitch = 24, new_ppq = 0,   new_end_ppq = 120 },
})
eq(#r.collisions, 0, "two notes trading places do not collide")
eq(#r.plan, 2, "and both are planned")

eq(#pp(base, {}).plan, 0, "an empty request plans nothing")

-- Send modes go over the wire as names because REAPER's ints skip 2; anything
-- unnamed must come back nil so create_send can refuse it rather than sending
-- post-fader by accident.
local smv = B.send_mode_value
eq(smv("post_fader"), 0, "post_fader is REAPER's 0")
eq(smv("pre_fx"), 1, "pre_fx is REAPER's 1")
eq(smv("pre_fader"), 3, "pre_fader is REAPER's 3, not 2")
eq(smv("prefader"), nil, "an unnamed mode is rejected, not guessed at")

-- get_track_routing reads the same modes back by name. Two tables holding one
-- fact drift, so assert they are exact inverses rather than trusting the eye.
local smn = B.send_mode_names
for _, name in ipairs({ "post_fader", "pre_fx", "pre_fader" }) do
  eq(smn[smv(name)], name, name .. " survives the write/read round trip")
end
local named = 0
for value, name in pairs(smn) do
  eq(smv(name), value, "the read table's " .. name .. " matches the write path")
  named = named + 1
end
eq(named, 3, "exactly three modes are named on both sides")
eq(smn[2], nil, "REAPER's unused 2 is named on neither side")

-- save_project refuses a project with no file rather than letting REAPER open a
-- Save As dialog, which would block the defer loop until a human clicked it.
-- Refusing is the whole point, so the empty cases must not fall through.
local ste = B.save_target_error
eq(ste("C:\\media\\song.rpp"), nil, "a real project path saves")
eq(ste("/home/d/song.rpp"), nil, "a POSIX project path saves")
ok(ste(""):find("SAVE_UNSAFE", 1, true) ~= nil,
   "an empty path refuses before REAPER can open a dialog")
ok(ste(nil):find("SAVE_UNSAFE", 1, true) ~= nil, "a nil path refuses too")
ok(ste(nil):find("save it once", 1, true) ~= nil,
   "the refusal says how to make it saveable")

-- RENDER_FILE is the directory and RENDER_PATTERN the filename. render used to
-- write the whole path into RENDER_FILE and never set the pattern, so it
-- reported a path REAPER had not written. Both callers now split the same way.
local srt = B.split_render_target
local d1, p1 = srt("/tmp/renders/mix_take3.wav")
eq(d1, "/tmp/renders", "a full path yields its directory")
eq(p1, "mix_take3", "and its filename, stripped of .wav")
local d2, p2 = srt("mix_take3.wav")
eq(d2, "", "a bare filename has no directory")
eq(p2, "mix_take3", "and still strips .wav")
local d3, p3 = srt([[C:\media\working\smoke.wav]])
eq(d3, [[C:\media\working]], "a Windows path splits on the backslash")
eq(p3, "smoke", "and strips .wav there too")
-- The extension comes from the sink format, so a pattern that kept .wav would
-- render "name.wav.wav"; anything else is left alone.
eq(select(2, srt("/tmp/stem.flac")), "stem.flac", "a non-wav extension is left intact")
eq(select(2, srt("/tmp/no_extension")), "no_extension", "an extensionless name is unchanged")

-- Parity with the split capture ran inline before the helper existed. Capture
-- is the one path already proven against live renders, so the helper is only
-- safe if it still agrees with it on every shape.
local function old_capture_split(output_file)
  local dir, name = output_file:match("^(.*)[/\\]([^/\\]+)$")
  if not dir then dir, name = "", output_file end
  return dir, (name:gsub("%.wav$", ""))
end
for _, sample in ipairs({
  "/tmp/renders/mix_take3.wav", "mix_take3.wav", [[C:\media\working\smoke.wav]],
  "/tmp/stem.flac", "/tmp/no_extension", "bare",
}) do
  local od, op = old_capture_split(sample)
  local nd, np = srt(sample)
  eq(nd, od, "helper matches capture's old directory for " .. sample)
  eq(np, op, "helper matches capture's old pattern for " .. sample)
end

-- reload_bridge: the refusal logic is pure and must fail toward keeping the
-- running bridge. A compile failure refuses (handing over to a broken file
-- kills the bridge for a typo); an active preview refuses (the fresh
-- instance's startup recovery would silently cancel it).
local rv = B.reload_verdict
eq(rv(true, nil, false), nil, "clean compile, no preview: reload may proceed")
local compile_refusal = rv(false, "bridge.lua:12: '=' expected near 'end'", false)
ok(compile_refusal:find("^RELOAD_COMPILE_FAILED") ~= nil, "compile failure refuses")
ok(compile_refusal:find("'=' expected", 1, true) ~= nil,
   "refusal carries the compiler's own message")
local preview_refusal = rv(true, nil, true)
ok(preview_refusal:find("^RELOAD_BLOCKED") ~= nil, "active preview refuses")
ok(preview_refusal:find("cancel_preview", 1, true) ~= nil,
   "preview refusal names the way out")
ok(rv(false, "err", true):find("^RELOAD_BLOCKED") ~= nil,
   "preview refusal wins over compile: resolve the preview first")
eq(B.error_code_from(compile_refusal, "COMMAND_FAILED"), "RELOAD_COMPILE_FAILED",
   "compile refusal parses into a structured error code")
eq(B.error_code_from(preview_refusal, "COMMAND_FAILED"), "RELOAD_BLOCKED",
   "preview refusal parses into a structured error code")

-- Musical automation phase 1: inclusive boundaries and independent neighbors
-- are pure, so prove them without creating an envelope in REAPER.
local auto = B.automation
local envelope_points = {
  { time = 9, value = 0.1, shape = 0, tension = 0, selected = false, enumeration_order = 1 },
  { time = 10, value = 0.2, shape = 0, tension = 0, selected = false, enumeration_order = 2 },
  { time = 15, value = 0.3, shape = 1, tension = 0, selected = true, enumeration_order = 3 },
  { time = 20, value = 0.4, shape = 0, tension = 0, selected = false, enumeration_order = 4 },
  { time = 21, value = 0.5, shape = 0, tension = 0, selected = false, enumeration_order = 5 },
}
local in_range, neighbor_before, neighbor_after = auto.select_range(
  envelope_points, 10, 20, true)
eq(#in_range, 3, "automation range includes both exact boundaries")
eq(in_range[1].time, 10, "inclusive automation start is retained")
eq(in_range[3].time, 20, "inclusive automation end is retained")
eq(neighbor_before.time, 9, "nearest point before range is reported")
eq(neighbor_after.time, 21, "nearest point after range is reported")

local drifted_boundary = {
  { time = 20 + 1.4e-9, value = 0.4, shape = 0, tension = 0,
    selected = false, enumeration_order = 1 },
  { time = 20 + 1e-5, value = 0.5, shape = 0, tension = 0,
    selected = false, enumeration_order = 2 },
}
local drifted_inside, _, drifted_after = auto.select_range(
  drifted_boundary, 10, 20, true)
eq(#drifted_inside, 1, "nanosecond marker/envelope drift stays on inclusive boundary")
eq(drifted_after.time, 20 + 1e-5, "a materially later point remains an outside neighbor")

local duplicate_points = {
  { time = 4, value = 0.2000000001, shape = 0, tension = 0,
    selected = false, enumeration_order = 7, automation_item_id = "underlying" },
  { time = 4, value = 0.8, shape = 1, tension = 0.25,
    selected = true, enumeration_order = 8, automation_item_id = "item:0/pool:2" },
}
local canonical_a, hash_a, duplicates_a = auto.canonicalize(duplicate_points,
  function(time) return time end)
eq(canonical_a[1].enumeration_order, 7,
   "duplicate-time response preserves REAPER enumeration order")
eq(canonical_a[1].qn_tick, 3840, "canonical time is quantized to 1/960 QN")
eq(canonical_a[1].value_tick, 200000, "canonical value is quantized to 1e-6")
eq(#duplicates_a, 1, "duplicate-time points are explicitly reported")
eq(duplicates_a[1].enumeration_order[2], 8,
   "duplicate report carries REAPER enumeration order")
local _, hash_b = auto.canonicalize({ duplicate_points[2], duplicate_points[1] },
  function(time) return time end)
eq(hash_b, hash_a, "equal-time point reordering leaves canonical hash stable")
eq(auto.hash_text(""), "fnv1a32:811c9dc5", "empty envelope hash is stable")

-- Handler-level offline proof: GetFXEnvelope's create flag stays false, an
-- absent envelope is a successful empty read, and an existing envelope merges
-- base + automation-item points without moving inclusive boundaries.
_G.reaper.GetMasterTrack = function() return "master-object" end
_G.reaper.CountTracks = function() return 1 end
_G.reaper.GetTrack = function() return "automation-track" end
_G.reaper.GetTrackGUID = function(track)
  return track == "automation-track" and "{AUTO-TRACK}" or "{MASTER}"
end
_G.reaper.GetTrackName = function() return true, "Automation Track" end
_G.reaper.GetMediaTrackInfo_Value = function(_, key)
  if key == "I_AUTOMODE" then return 1 end
  return 0
end
_G.reaper.TrackFX_GetCount = function() return 1 end
_G.reaper.TrackFX_GetRecCount = function() return 0 end
_G.reaper.TrackFX_GetFXName = function() return true, "VST3: Test FX" end
_G.reaper.TrackFX_GetFXGUID = function() return "{AUTO-FX}" end
_G.reaper.TrackFX_GetNumParams = function() return 1 end
_G.reaper.TrackFX_GetParamName = function() return true, "Cutoff" end
_G.reaper.TrackFX_GetParamNormalized = function() return 0.5 end
_G.reaper.TrackFX_GetParam = function() return 0.5, 0, 1 end
_G.reaper.TrackFX_GetFormattedParamValue = function() return true, "50 %" end
_G.reaper.TimeMap2_timeToQN = function(_, time) return time end
local create_flags = {}
_G.reaper.GetFXEnvelope = function(_, _, _, create)
  create_flags[#create_flags + 1] = create
  return nil
end
local absent = auto.read_command({ payload = {
  target_track_guid = "{AUTO-TRACK}", fx_guid = "{AUTO-FX}", param_index = 0,
} })
eq(create_flags[1], false, "automation read never creates an absent envelope")
eq(absent.envelope.exists, false, "absent envelope is reported, not refused")
eq(absent.point_count, 0, "absent envelope has zero points")
eq(absent.canonical_hash, "fnv1a32:811c9dc5", "absent envelope has empty hash")

_G.reaper.GetFXEnvelope = function(_, _, _, create)
  create_flags[#create_flags + 1] = create
  return "envelope-object"
end
_G.reaper.GetEnvelopeName = function() return true, "Cutoff" end
_G.reaper.GetEnvelopeInfo_Value = function(_, key)
  local values = { B_ACTIVE = 1, B_VISIBLE = 0, B_ARM = 0, I_TCPH = 48 }
  return values[key]
end
_G.reaper.CountEnvelopePointsEx = function(_, item_index)
  return item_index == -1 and 3 or 1
end
_G.reaper.GetEnvelopePointEx = function(_, item_index, point_index)
  if item_index == -1 then
    local points = {
      { 9, 0.1, 0, 0, false },
      { 10, 0.2, 0, 0, false },
      { 21, 0.5, 0, 0, false },
    }
    local point = points[point_index + 1]
    return true, point[1], point[2], point[3], point[4], point[5]
  end
  return true, 15, 0.7, 5, 0.25, true
end
_G.reaper.CountAutomationItems = function() return 1 end
_G.reaper.GetSetAutomationItemInfo = function(_, _, key)
  local values = {
    D_POOL_ID = 12, D_POSITION = 12, D_LENGTH = 6, D_STARTOFFS = 0,
    D_PLAYRATE = 1, D_LOOPSRC = 0, D_BASELINE = 0.5, D_AMPLITUDE = 1,
    D_UISEL = 1, D_MUTE = 0,
  }
  return values[key]
end
local present = auto.read_command({ payload = {
  target_track_guid = "{AUTO-TRACK}", fx_guid = "{AUTO-FX}", param_index = 0,
  start_time = 10, end_time = 20, include_neighbors = true,
} })
eq(create_flags[2], false, "automation read never recreates an existing envelope")
eq(present.envelope.exists, true, "existing envelope is reported")
eq(present.envelope.visible, false, "read preserves and reports hidden state")
eq(present.envelope.armed, false, "read preserves and reports unarmed state")
eq(present.automation_mode, "read", "track automation mode is named")
eq(present.point_count, 2, "base and automation-item points share one range read")
eq(present.points[1].time, 10, "handler keeps exact inclusive start")
eq(present.points[2].automation_item, 0, "handler identifies automation-item point")
eq(present.neighbor_before.time, 9, "handler reports prior neighbor")
eq(present.neighbor_after.time, 21, "handler reports following neighbor")
eq(present.automation_items[1].pool_id, 12, "automation item pool identity is reported")
eq(present.automation_items[1].muted, false, "automation item mute state is reported")

-- Musical automation phase 2: transactional writes. A mutable fake envelope
-- stands in for the real object; every claim is proved by rereading it, never
-- by trusting the write path's own bookkeeping.
local function make_fake_envelope(points)
  local env = {
    points = {},
    state = { B_ACTIVE = 1, B_VISIBLE = 0, B_ARM = 0, I_TCPH = 0 },
    items = {}, exists = true, created = false,
    fail_insert_at = nil, corrupt_value_at = nil,
  }
  for _, p in ipairs(points or {}) do
    env.points[#env.points + 1] = {
      time = p[1], value = p[2], shape = p[3] or 0,
      tension = p[4] or 0, selected = p[5] or false,
    }
  end
  return env
end

local write_track_mode = 1
local envelopes = {}            -- track guid -> fake envelope (or nil = absent)
local automation_write = auto.write_command
local automation_transaction = auto.transaction_command
local function bind_write_fakes()
  _G.reaper.GetTrack = function(_, index)
    if index == 0 then return "L-track" end
    if index == 1 then return "R-track" end
    return nil
  end
  _G.reaper.CountTracks = function() return 2 end
  _G.reaper.GetTrackGUID = function(track)
    if track == "L-track" then return "{L}" end
    if track == "R-track" then return "{R}" end
    return "{MASTER}"
  end
  _G.reaper.GetTrackName = function(track)
    if track == "L-track" then return true, "Guitar L" end
    if track == "R-track" then return true, "Guitar R" end
    return true, "Automation Track"
  end
  _G.reaper.TrackFX_GetFXGUID = function(track)
    if track == "L-track" then return "{FX-L}" end
    if track == "R-track" then return "{FX-R}" end
    return "{AUTO-FX}"
  end
  _G.reaper.GetFXEnvelope = function(track, _, _, create)
    local env = envelopes[reaper.GetTrackGUID(track)]
    if env and env.exists then return env end
    if create and env then
      env.exists, env.created = true, true
      -- REAPER seeds a new envelope with a time-0 point at the parameter's
      -- current value; modeling this is what caught the live verification
      -- mismatch on 2026-08-21.
      env.points[#env.points + 1] = { time = 0, value = 0.5, shape = 0,
                                      tension = 0, selected = false }
      return env
    end
    return nil
  end
  _G.reaper.CountEnvelopePointsEx = function(env, item)
    return item == -1 and #env.points or 0
  end
  _G.reaper.GetEnvelopePointEx = function(env, item, index)
    local p = env.points[index + 1]
    if not p then return false end
    local value = p.value
    if env.corrupt_value_at and math.abs(p.time - env.corrupt_value_at) < 1e-9 then
      value = value + 0.01
    end
    return true, p.time, value, p.shape, p.tension, p.selected
  end
  _G.reaper.InsertEnvelopePointEx = function(env, _, time, value, shape, tension, selected)
    if env.fail_insert_at and math.abs(time - env.fail_insert_at) < 1e-9 then
      return false
    end
    env.points[#env.points + 1] = {
      time = time, value = value, shape = shape,
      tension = tension, selected = selected,
    }
    return true
  end
  _G.reaper.DeleteEnvelopePointEx = function(env, _, index)
    table.remove(env.points, index + 1)
    return true
  end
  _G.reaper.Envelope_SortPoints = function(env)
    table.sort(env.points, function(a, b) return a.time < b.time end)
    return true
  end
  _G.reaper.GetEnvelopeInfo_Value = function(env, key) return env.state[key] end
  -- Deliberately NO SetEnvelopeInfo_Value: this REAPER build does not export
  -- it (verified against the installed API 2026-08-21), so the write path
  -- must not pretend to set envelope visibility or arm state.
  _G.reaper.CountAutomationItems = function(env) return #env.items end
  _G.reaper.GetSetAutomationItemInfo = function(env, index, key)
    local item = env.items[index + 1]
    return item and item[key] or 0
  end
  _G.reaper.GetMediaTrackInfo_Value = function(_, key)
    if key == "I_AUTOMODE" then return write_track_mode end
    return 0
  end
  _G.reaper.SetMediaTrackInfo_Value = function(_, key, value)
    if key == "I_AUTOMODE" then write_track_mode = math.floor(value + 0.5) end
    return true
  end
  _G.reaper.SetCursorContext = function() end
  _G.reaper.TrackList_AdjustWindows = function() end
  _G.reaper.UpdateArrange = function() end
  _G.reaper.TimeMap2_timeToBeats = function(_, time) return time, 0 end
end

local function envelope_times(env)
  local times = {}
  for _, p in ipairs(env.points) do times[#times + 1] = p.time end
  return times
end

local function write_payload(overrides)
  local payload = {
    target_track_guid = "{L}", fx_guid = "{FX-L}", param_index = 0,
    ranges = { { start_time = 10, end_time = 20 } },
    points = { { time = 12, value = 0.6 }, { time = 18, value = 0.7 } },
  }
  if overrides then
    for key, value in pairs(overrides) do payload[key] = value end
  end
  return payload
end

-- Replacement writes exactly the declared scope, inclusively, and leaves
-- neighbors untouched.
bind_write_fakes()
envelopes["{L}"] = make_fake_envelope({
  { 9, 0.1 }, { 10, 0.2 }, { 15, 0.3, 1 }, { 20, 0.4 }, { 21, 0.5 },
})
local replaced = automation_write({ payload = write_payload() })
eq(replaced.verification.requested, 2, "write reports requested point count")
eq(replaced.verification.replaced, 3, "in-range preimage points are replaced")
eq(replaced.verification.inserted, 2, "new points are inserted")
eq(replaced.verification.skipped, 0, "nothing identical to skip")
eq(replaced.verification.confirmed, 2, "reread confirms written points")
eq(replaced.verification.refused, 0, "nothing refused on success")
eq(replaced.inserted_count, 2, "legacy inserted_count agrees with verification")
eq(replaced.cleared_range.start_time, 10, "cleared range reports inclusive start")
eq(replaced.cleared_range.end_time, 20, "cleared range reports inclusive end")
eq(#envelopes["{L}"].points, 4, "neighbors survive the replacement")
eq(envelopes["{L}"].points[1].time, 9, "neighbor before range is untouched")
eq(envelopes["{L}"].points[1].value, 0.1, "neighbor before range keeps its value")
eq(envelopes["{L}"].points[4].time, 21, "neighbor after range is untouched")
eq(envelopes["{L}"].state.B_VISIBLE, 0, "write leaves envelope state untouched")
eq(replaced.envelope.visible, false, "reply reports envelope state as-is")
eq(replaced.automation_mode, "read", "reply reports track automation mode")
eq(replaced.final_hash ~= nil, true, "reply carries the live canonical hash")

-- Re-applying an identical point is idempotent: the existing point is kept,
-- counted as skipped, and never deleted and re-inserted.
envelopes["{L}"] = make_fake_envelope({
  { 9, 0.1 }, { 10, 0.2 }, { 15, 0.3, 1 }, { 20, 0.4 }, { 21, 0.5 },
})
local idempotent = automation_write({ payload = write_payload({
  points = { { time = 15, value = 0.3, shape = "square" }, { time = 16, value = 0.35 } },
}) })
eq(idempotent.verification.replaced, 2, "identical point is not counted as replaced")
eq(idempotent.verification.skipped, 1, "identical point is skipped, not rewritten")
eq(idempotent.verification.inserted, 1, "only the new point is inserted")
eq(#envelopes["{L}"].points, 4, "identical point survives as one point")

-- The 1e-7 boundary tolerance carries through the write path: nanosecond
-- drift from bar/marker conversion is inside the inclusive range.
envelopes["{L}"] = make_fake_envelope({ { 9, 0.1 }, { 20 + 1.4e-9, 0.4 }, { 21, 0.5 } })
local drifted = automation_write({ payload = write_payload({
  points = { { time = 20, value = 0.9 } },
}) })
eq(drifted.verification.replaced, 1, "nanosecond-drifted end boundary is replaced")
eq(drifted.verification.confirmed, 1, "replacement at the boundary is confirmed")
eq(envelopes["{L}"].points[2].value, 0.9, "boundary point holds the new value")

-- Append mode (no ranges, no clear flag) inserts without deleting anything.
envelopes["{L}"] = make_fake_envelope({ { 9, 0.1 }, { 10, 0.2 } })
local appended = automation_write({ payload = write_payload({
  ranges = {}, points = { { time = 30, value = 0.5 } },
}) })
eq(appended.verification.replaced, 0, "append mode replaces nothing")
eq(appended.cleared_range, nil, "append mode reports no cleared range")
eq(#envelopes["{L}"].points, 3, "append mode keeps preexisting points")

-- Legacy clear_existing_in_range infers the scope from the point extent.
envelopes["{L}"] = make_fake_envelope({ { 12, 0.2 }, { 18, 0.3 }, { 25, 0.4 } })
local legacy = automation_write({ payload = write_payload({
  ranges = {},
  clear_existing_in_range = true,
  points = { { time = 12, value = 0.6 }, { time = 18, value = 0.7 } },
}) })
eq(legacy.verification.replaced, 2, "legacy clear flag replaces the point extent")
eq(#envelopes["{L}"].points, 3, "legacy inference leaves later points alone")

-- Refusals prove the typed code and that nothing was mutated.
local function refused(payload, expected_code, mutation_message)
  envelopes["{L}"] = make_fake_envelope({ { 9, 0.1 }, { 15, 0.3 }, { 21, 0.5 } })
  local before = envelope_times(envelopes["{L}"])
  local ok, err = pcall(automation_write, { payload = payload })
  eq(ok, false, "invalid write refuses instead of partially applying")
  eq(ecf(err, "MISSING"), expected_code, mutation_message)
  eq(#envelopes["{L}"].points, #before, "refused write changes no points")
  eq(envelopes["{L}"].state.B_VISIBLE, 0, "refused write does not show the envelope")
  return err
end

refused(write_payload({ points = { { time = 25, value = 0.5 } } }),
  "POINT_OUTSIDE_RANGE", "point outside every declared range is refused")
refused(write_payload({ points = { { time = 12, value = 1.5 } } }),
  "BAD_POINT_VALUE", "out-of-range value is refused, not clamped")
refused(write_payload({ points = { { time = 12, value = 0.5 },
                                    { time = 12, value = 0.6 } } }),
  "DUPLICATE_POINT_TIME", "duplicate request times are refused")
refused(write_payload({ points = {} }), "NO_POINTS", "empty point list is refused")
refused(write_payload({ ranges = { { start_time = 20, end_time = 10 } } }),
  "BAD_AUTOMATION_RANGE", "reversed range is refused")

-- A write intersecting an automation item refuses before any mutation.
envelopes["{L}"] = make_fake_envelope({ { 9, 0.1 }, { 15, 0.3 } })
envelopes["{L}"].items = { { D_POSITION = 12, D_LENGTH = 4 } }
local item_hit = pcall(automation_write, { payload = write_payload() })
eq(item_hit, false, "automation-item intersection refuses")
envelopes["{L}"].items = { { D_POSITION = 100, D_LENGTH = 10 } }
local item_clear = automation_write({ payload = write_payload() })
eq(item_clear.verification.confirmed, 2,
   "a non-intersecting automation item does not block the write")

-- An injected insert failure restores the full preimage: points AND the
-- envelope state the write had started showing.
envelopes["{L}"] = make_fake_envelope({
  { 9, 0.1 }, { 10, 0.2 }, { 15, 0.3 }, { 20, 0.4 }, { 21, 0.5 },
})
envelopes["{L}"].fail_insert_at = 18
local rolled, rolled_err = pcall(automation_write, { payload = write_payload() })
eq(rolled, false, "insert failure refuses the write")
eq(ecf(rolled_err, "MISSING"), "AUTOMATION_ROLLED_BACK",
   "injected failure reports a rolled-back transaction")
eq(rolled_err:find("restored and reread 1", 1, true) ~= nil, true,
   "rollback reports what it restored and reread")
eq(#envelopes["{L}"].points, 5, "rollback restores every preimage point")
eq(envelopes["{L}"].points[3].time, 15, "restored points keep their positions")
eq(envelopes["{L}"].points[3].value, 0.3, "restored points keep their values")
eq(envelopes["{L}"].state.B_VISIBLE, 0, "rollback leaves envelope state untouched")
eq(envelopes["{L}"].state.B_ARM, 0, "rollback restores envelope arm state")
eq(write_track_mode, 1, "rollback leaves the track automation mode alone")

-- A reread that disagrees with the write is unconfirmed and rolls back,
-- even though every API call "succeeded".
envelopes["{L}"] = make_fake_envelope({ { 9, 0.1 }, { 15, 0.3 } })
envelopes["{L}"].corrupt_value_at = 12
local unconfirmed, unconfirmed_err = pcall(automation_write, {
  payload = write_payload({ points = { { time = 12, value = 0.6 } } }),
})
eq(unconfirmed, false, "write with corrupted readback refuses")
eq(unconfirmed_err:find("AUTOMATION_WRITE_UNCONFIRMED", 1, true) ~= nil, true,
   "corrupted readback is reported as unconfirmed, then rolled back")
eq(#envelopes["{L}"].points, 2, "unconfirmed write restores the preimage")

-- Rollback of an envelope the transaction created: ReaScript cannot delete
-- an FX envelope, so it is emptied and hidden, and the reply says so.
envelopes["{L}"] = make_fake_envelope({})
envelopes["{L}"].exists = false
envelopes["{L}"].fail_insert_at = 12
local created, created_err = pcall(automation_write, {
  payload = write_payload({ ranges = {}, points = { { time = 12, value = 0.6 } } }),
})
eq(created, false, "failed create-and-write refuses")
eq(created_err:find("emptied (ReaScript cannot delete", 1, true) ~= nil, true,
   "created-envelope rollback reports what it could not fully undo")
eq(#envelopes["{L}"].points, 0, "created envelope is emptied on rollback")
eq(envelopes["{L}"].state.B_VISIBLE, 0, "created envelope keeps its default state")

-- A created envelope keeps REAPER's time-0 seed point: it is state REAPER
-- added, the parameter holds its current value until the first written
-- point, and verification proves the seed survives alongside the write.
-- (Found live 2026-08-21: without this, every create-and-write refused as
-- unconfirmed.)
envelopes["{L}"] = make_fake_envelope({})
envelopes["{L}"].exists = false
local seeded = automation_write({
  payload = write_payload({ points = { { time = 12, value = 0.75 } } }),
})
eq(seeded.verification.confirmed, 1, "created-envelope write confirms its point")
eq(#envelopes["{L}"].points, 2, "seed point plus written point remain")
eq(envelopes["{L}"].points[1].time, 0, "REAPER seed point survives the write")
eq(envelopes["{L}"].points[2].value, 0.75, "written point follows the seed")

-- Multi-envelope transactions: one failing right-channel write restores the
-- left channel too — the Phase 2 exit gate, offline.
envelopes["{L}"] = make_fake_envelope({ { 9, 0.1 }, { 15, 0.3 } })
envelopes["{R}"] = make_fake_envelope({ { 9, 0.1 }, { 15, 0.3 } })
envelopes["{R}"].fail_insert_at = 18
local function stereo_writes()
  return {
    target_track_guid = "{L}", fx_guid = "{FX-L}", param_index = 0,
    ranges = { { start_time = 10, end_time = 20 } },
    points = { { time = 12, value = 0.6 } },
  }, {
    target_track_guid = "{R}", fx_guid = "{FX-R}", param_index = 0,
    ranges = { { start_time = 10, end_time = 20 } },
    points = { { time = 18, value = 0.7 } },
  }
end
local left_write, right_write = stereo_writes()
local stereo_fail, stereo_err = pcall(automation_transaction, {
  payload = { writes = { left_write, right_write } },
})
eq(stereo_fail, false, "right-channel failure fails the transaction")
eq(ecf(stereo_err, "MISSING"), "AUTOMATION_ROLLED_BACK",
   "stereo failure reports the rollback")
eq(#envelopes["{L}"].points, 2, "left channel is restored to its preimage")
eq(envelopes["{L}"].points[2].value, 0.3, "left channel keeps its original values")
eq(#envelopes["{R}"].points, 2, "right channel is restored to its preimage")

-- Validation happens for every write before any mutation: a bad second
-- write must not even create the first write's envelope.
envelopes["{L}"] = make_fake_envelope({})
envelopes["{L}"].exists = false
envelopes["{R}"] = make_fake_envelope({})
local bad_left, bad_right = stereo_writes()
bad_right.points = { { time = 25, value = 0.5 } }
local prevalidated = pcall(automation_transaction, {
  payload = { writes = { bad_left, bad_right } },
})
eq(prevalidated, false, "one invalid write refuses the transaction")
eq(envelopes["{L}"].exists, false, "nothing is created before every write validates")

-- A successful transaction reports identity, counts, and one touched
-- envelope per write.
envelopes["{L}"] = make_fake_envelope({ { 9, 0.1 }, { 15, 0.3 } })
envelopes["{R}"] = make_fake_envelope({ { 9, 0.1 }, { 15, 0.3 } })
local ok_left, ok_right = stereo_writes()
ok_right.points = { { time = 18, value = 0.7 }, { time = 19, value = 0.8 } }
local transaction = automation_transaction({
  id = "cli-2026-test",
  payload = { writes = { ok_left, ok_right }, transaction_id = "tx-01" },
})
eq(transaction.rolled_back, false, "successful transaction is not rolled back")
eq(transaction.transaction_id, "tx-01", "transaction id echoes the payload")
eq(#transaction.touched_envelopes, 2, "touched envelopes are enumerated")
eq(transaction.touched_envelopes[2].track_guid, "{R}", "touched envelope names its track")
eq(transaction.touched_envelopes[1].param_name, "Cutoff", "touched envelope names its parameter")
eq(transaction.writes[2].verification.confirmed, 2, "per-write counts are reported")
eq(transaction.writes[1].final_hash == transaction.writes[2].final_hash, false,
   "per-write final hashes are computed independently")

-- Two writes aiming at one envelope are a payload bug, not a merge.
local dup_a, dup_b = stereo_writes()
dup_b.target_track_guid = "{L}"
dup_b.fx_guid = "{FX-L}"
envelopes["{L}"] = make_fake_envelope({})
local _, dup_err = pcall(automation_transaction, {
  payload = { writes = { dup_a, dup_b } },
})
eq(ecf(dup_err, "MISSING"), "BAD_PAYLOAD", "duplicate envelope targets are refused")

eq(pcall(automation_transaction, { payload = { writes = {} } }), false,
   "empty transaction refuses")

-- get_items offline probe (2026-09-01). REAPER exposes NO ReaScript call for
-- media-item offline state -- the only *_GetOffline functions are TrackFX and
-- TakeFX, which are about plugins. So get_items reports candidate indirect
-- signals raw and never fabricates a boolean. These checks pin that contract:
-- the probe fields are present and passed through untouched, and take-FX
-- offline (which IS authoritative) is reported per FX.
--
-- This is also the first handler-level lua coverage in this file. Everything
-- above tests pure helpers; `handlers` was added to the selftest seam because
-- get_items had none, which is how the probe shipped untested the first time.
-- Wrapped in a function, not a do-block: this file is at Lua's 200-local
-- limit for the main chunk (the same wall reaper_agent_bridge.lua hit), and
-- do-block locals still count against it. A function gets its own budget.
;(function()
  local fake_item, fake_take, fake_src = "item-0", "take-0", "src-0"
  _G.reaper.GetMasterTrack = function() return "master-object" end
  _G.reaper.CountTracks = function() return 1 end
  _G.reaper.GetTrack = function(_, i) return i == 0 and "track-object" or nil end
  _G.reaper.GetTrackName = function() return true, "L" end
  _G.reaper.CountTrackMediaItems = function() return 1 end
  _G.reaper.GetTrackMediaItem = function() return fake_item end
  _G.reaper.GetActiveTake = function() return fake_take end
  _G.reaper.IsMediaItemSelected = function() return false end
  _G.reaper.GetMediaItemInfo_Value = function(_, key)
    if key == "D_POSITION" then return 4.0 end
    if key == "D_LENGTH" then return 2.5 end
    if key == "B_MUTE" then return 0 end
    if key == "D_VOL" then return 1.0 end
    return 0
  end
  _G.reaper.GetSetMediaItemTakeInfo_String = function() return true, "guitar L" end
  _G.reaper.GetMediaItemTake_Source = function() return fake_src end
  _G.reaper.GetMediaSourceType = function() return "WAVE" end
  _G.reaper.GetMediaSourceFileName = function() return "" end
  _G.reaper.GetMediaSourceLength = function() return 2.5, false end
  _G.reaper.GetMediaSourceNumChannels = function() return 2 end
  _G.reaper.GetMediaSourceSampleRate = function() return 48000 end
  _G.reaper.GetItemStateChunk = function()
    return true, "<ITEM\nPOSITION 4\n<SOURCE WAVE\nFILE \"guitar-L.wav\"\n>\n>"
  end
  _G.reaper.TakeFX_GetCount = function() return 2 end
  _G.reaper.TakeFX_GetFXName = function(_, i)
    return true, i == 0 and "VST3: thall amp" or "VST3: mixIR3"
  end
  _G.reaper.TakeFX_GetOffline = function(_, i) return i == 1 end
  _G.reaper.TakeFX_GetEnabled = function(_, i) return i == 0 end

  local got = B.handlers.get_items({ payload = { target_track_name = "L" } })
  eq(got.item_count, 1, "get_items counts the track's items")
  eq(got.track.name, "L", "get_items reports the resolved track name")
  local it = got.items[1]
  eq(it.take_name, "guitar L", "get_items reports the take name")
  eq(it.source_channels, 2, "offline probe reports source channel count")
  eq(it.source_sample_rate, 48000, "offline probe reports source sample rate")
  eq(it.source_chunk_line, "SOURCE WAVE",
     "offline probe extracts only the SOURCE header line, not the whole chunk")
  eq(#it.take_fx, 2, "get_items reports every take FX")
  eq(it.take_fx[1].name, "VST3: thall amp", "take FX carries its plugin name")
  eq(it.take_fx[1].offline, false, "an online take FX reports offline=false")
  eq(it.take_fx[2].offline, true, "an offline take FX reports offline=true")
  eq(it.take_fx[1].bypassed, false, "an enabled take FX reports bypassed=false")
  eq(it.take_fx[2].bypassed, true, "a disabled take FX reports bypassed=true")
  -- source_state is INFERRED (REAPER exposes no item-offline API), so every
  -- branch is pinned. The live signal these encode, measured on drones.rpp:
  -- an unloaded source reports length 0 and rate 0 while its file still opens.
  eq(it.source_state, "loaded", "a source with a real rate reports loaded")
  eq(it.source_state_inferred, true, "source_state is flagged as inferred, not read")

  -- 0 rate + 0 length + a readable file is REAPER declining to load media
  -- that is right there. That is offline, and it is the case David hit.
  _G.reaper.GetMediaSourceSampleRate = function() return 0 end
  _G.reaper.GetMediaSourceLength = function() return 0.0, false end
  -- source_readable is a REAL io.open, so the readable case needs a real file.
  -- That is the point: it tests the access REAPER's own process has.
  local present = join(sandbox, "present-source.wav")
  local pf = assert(io.open(present, "wb")); pf:write("RIFF"); pf:close()
  _G.reaper.GetMediaSourceFileName = function() return present end
  local off = B.handlers.get_items({ payload = { target_track_name = "L" } })
  eq(off.items[1].source_state, "offline",
     "0 rate + 0 length + a readable file infers offline")

  -- Same 0/0 numbers, but the file does NOT open: that is missing media, a
  -- different problem with a different fix. Collapsing the two would send
  -- someone hunting a preference when their drive is unmounted.
  _G.reaper.GetMediaSourceFileName = function() return "C:/gone/missing.wav" end
  local gone = B.handlers.get_items({ payload = { target_track_name = "L" } })
  eq(gone.items[1].source_readable, false, "a missing file is not readable")
  eq(gone.items[1].source_state, "unresolved",
     "0 rate + 0 length + an unreadable file is unresolved, NOT offline")

  -- MIDI has no sample rate by nature and must never be called offline.
  _G.reaper.GetMediaSourceType = function() return "MIDI" end
  local midi = B.handlers.get_items({ payload = { target_track_name = "L" } })
  eq(midi.items[1].source_state, "midi",
     "an in-project MIDI source is excluded, not mislabelled offline")
  _G.reaper.GetMediaSourceType = function() return "WAVE" end
  _G.reaper.GetMediaSourceSampleRate = function() return 48000 end
  _G.reaper.GetMediaSourceLength = function() return 2.5, false end
  _G.reaper.GetMediaSourceFileName = function() return "" end

  -- A source-less take (empty/MIDI item) must not crash the probe.
  _G.reaper.GetMediaItemTake_Source = function() return nil end
  local no_src = B.handlers.get_items({ payload = { target_track_name = "L" } })
  eq(no_src.items[1].source_channels, nil, "a take with no source reports no channels")
  eq(#no_src.items[1].take_fx, 2, "take FX are still reported without a source")

  -- A take-less item (empty item) must not crash either.
  _G.reaper.GetActiveTake = function() return nil end
  local no_take = B.handlers.get_items({ payload = { target_track_name = "L" } })
  eq(no_take.items[1].take_fx, nil, "an item with no take reports no take FX")
  eq(no_take.items[1].position, 4.0, "an item with no take still reports its position")
end)()

;(function()
-- set_media_offline_when_inactive (2026-09-01). The risk gate and the
-- read-back are the safety story: this writes a persistent REAPER preference,
-- not project state, so a silent no-op must never look like success.
local h = B.handlers.set_media_offline_when_inactive
local function err(payload, dry)
  local ok, e = pcall(h, { payload = payload, dry_run = dry })
  eq(ok, false, "expected a refusal")
  return tostring(e)
end

B.config.allow_risk_level_3 = true
ok(err({}):find("BAD_PAYLOAD", 1, true) ~= nil,
   "a missing `enabled` is refused rather than assumed")
ok(err({ enabled = "false" }):find("BAD_PAYLOAD", 1, true) ~= nil,
   "the string \"false\" is refused, not treated as a boolean")

B.config.allow_risk_level_3 = false
ok(err({ enabled = false }):find("PREFERENCE_BLOCKED", 1, true) ~= nil,
   "writing a REAPER preference is gated at risk level 3")

-- The gate is checked BEFORE dry_run on purpose: a dry run of a blocked
-- operation must report that it is blocked, not preview a write that cannot
-- happen.
ok(err({ enabled = false }, true):find("PREFERENCE_BLOCKED", 1, true) ~= nil,
   "dry_run does not bypass the gate")

B.config.allow_risk_level_3 = true
local saved_set = _G.reaper.SNM_SetIntConfigVar
_G.reaper.SNM_SetIntConfigVar = nil
ok(err({ enabled = false }):find("SWS_REQUIRED", 1, true) ~= nil,
   "no SWS is a typed refusal, not a crash")

-- offlineinact is treated as a whole value, not a bitfield: REAPER's encoding
-- is not documented in the ReaScript surface, and an earlier cut that assumed
-- bit 0 was the enable made the writer disagree with the reader. Off is 0.
local written = nil
local cfg = "3"
_G.reaper.SNM_SetIntConfigVar = function(_, v) written = v; cfg = tostring(v) end
_G.reaper.get_config_var_string = function() return true, cfg end
local res = h({ payload = { enabled = false } })
eq(written, 0, "turning it off writes 0, with no guess about a bit layout")
eq(res.before, true, "the result reports the value it found")
eq(res.after, false, "the result reports the value it read back")
eq(res.raw_before, 3, "raw_before is returned so the prior value can be restored")
-- Restoring by raw is what makes an off/on round trip lossless.
local res2 = h({ payload = { enabled = true, raw = 3 } })
eq(written, 3, "enabling with an explicit raw restores that exact value")
eq(res2.after, true, "re-enabling reads back as on")
local res3 = h({ payload = { enabled = false } })
eq(res3.after, false, "off again")
local res4 = h({ payload = { enabled = true } })
eq(written, 1, "enabling from 0 with no raw falls back to 1")
eq(res4.after, true, "the fallback still reads back as on")

-- dry_run previews without writing.
written = nil
local dry = h({ payload = { enabled = false }, dry_run = true })
eq(dry.dry_run, true, "dry_run is reported")
eq(dry.would_set, false, "dry_run reports what it would set")
eq(written, nil, "dry_run writes nothing")

-- A setter that silently no-ops must be caught by the read-back, not returned
-- as success. This is the failure the read-back exists for.
_G.reaper.SNM_SetIntConfigVar = function() end
ok(err({ enabled = false }):find("PREFERENCE_NOT_APPLIED", 1, true) ~= nil,
   "a silent no-op setter is caught by reading the value back")
_G.reaper.SNM_SetIntConfigVar = saved_set
end)()

rmrf(sandbox)
print(("test_bridge: OK (%d checks)"):format(checks))
