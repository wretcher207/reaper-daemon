-- Self-check for the bridge's pure/atomic helpers. No live REAPER needed:
-- stub just enough of `reaper` to load the file in selftest mode, then assert.
--   Run:  lua bridge/test_bridge.lua
-- Catches the two regressions a hostile review found (kHz parse, Windows
-- heartbeat freeze) at the source instead of in a live session.

local here = (arg[0] or ""):match("^(.*)[/\\][^/\\]+$") or "."
local bridge_file = here .. "/reaper_agent_bridge.lua"
local tmp = (os.getenv("TMPDIR") or "/tmp"):gsub("/$", "")
local root = tmp .. "/reaper_bridge_selftest"

os.execute("mkdir -p '" .. root .. "/bridge'")
os.execute("cp '" .. here .. "/json.lua' '" .. root .. "/bridge/json.lua'")

-- Minimal stub: the load path before the selftest seam only touches these.
_G.reaper = {
  get_action_context = function() return true, root .. "/bridge/reaper_agent_bridge.lua" end,
  RecursiveCreateDirectory = function(p) os.execute("mkdir -p '" .. p .. "'") end,
  EnumerateFiles = function() return nil end,
  time_precise = function() return 0 end,
}
_G.REAPER_AGENT_BRIDGE_DIR = root .. "/bridge"
_G.REAPER_BRIDGE_SELFTEST = true

local B = assert(dofile(bridge_file), "bridge did not return its selftest table")
local pdn, atomic_write_json = B.parse_display_number, B.atomic_write_json

local checks = 0
local function eq(got, want, label)
  assert(got == want, ("%s: got %s, want %s"):format(label, tostring(got), tostring(want)))
  checks = checks + 1
end

-- H4: kHz must scale to Hz, else "1.20 kHz" (1.2) never matches target "1200 Hz".
eq(pdn("1.20 kHz"), 1200, "kHz display scales")
eq(pdn("1200 Hz"), 1200, "Hz target")
eq(pdn("80 Hz"), 80, "plain Hz, no false kHz")
eq(pdn("-3.0 dB"), -3.0, "signed dB")
eq(pdn("50 %"), 50, "percent")
eq(pdn("inf"), 1e30, "inf endpoint")
eq(pdn("-inf"), -1e30, "-inf endpoint")
eq(pdn("Bell"), nil, "enum/string rejected")

-- C1: writing the same path twice must succeed (this is what froze on Windows).
package.path = root .. "/bridge/?.lua;" .. package.path
local json = require("json")
local p = root .. "/aw_test.json"
atomic_write_json(p, { a = 1 })
atomic_write_json(p, { a = 2 })
local f = assert(io.open(p, "rb")); local body = f:read("*a"); f:close()
eq(json.decode(body).a, 2, "atomic_write_json overwrites in place")

os.execute("rm -rf '" .. root .. "'")
print(("test_bridge: OK (%d checks)"):format(checks))
