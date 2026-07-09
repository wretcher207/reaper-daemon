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

-- Render-dialog-hang fix: force renderclosewhendone bit0 (auto-close) on for a
-- render, restore the user's setting after. Needs SWS (SNM_*); must degrade,
-- never hang, when SWS is absent.
local ear = B.ensure_render_autoclose
local rar = B.restore_render_autoclose

-- No SWS -> cannot force auto-close; degrade to "not guaranteed" (caller warns).
_G.reaper.SNM_GetIntConfigVar = nil
_G.reaper.SNM_SetIntConfigVar = nil
local tok = ear()
eq(tok.guaranteed, false, "no SWS -> not guaranteed")
ok(tok.restore == nil, "no SWS -> nothing to restore")
rar(tok) -- must be a harmless no-op, not an error

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

-- bit0 already set -> leave it alone, nothing to restore.
store.renderclosewhendone = 2097157
tok = ear()
eq(tok.guaranteed, true, "SWS + bit already set -> guaranteed")
ok(tok.restore == nil, "already auto-closing -> no restore needed")
eq(store.renderclosewhendone, 2097157, "already-on value untouched")

-- config var missing (SNM returns the error sentinel) -> degrade, don't touch.
store.renderclosewhendone = nil
tok = ear()
eq(tok.guaranteed, false, "missing config var -> not guaranteed")
ok(tok.restore == nil, "missing config var -> nothing to restore")

rmrf(sandbox)
print(("test_bridge: OK (%d checks)"):format(checks))
