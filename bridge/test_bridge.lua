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

rmrf(sandbox)
print(("test_bridge: OK (%d checks)"):format(checks))
