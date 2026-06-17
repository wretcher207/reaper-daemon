-- Tests for bridge/json.lua — run with: lua bridge/test_json.lua
-- Covers the cases that regressed before: UTF-8 \u escapes (was "?"), surrogate
-- pairs, empty-object vs empty-array encoding, error paths.

local json = require("bridge.json")

local pass, fail = 0, 0
local function check(name, got, expect)
  if got == expect then
    pass = pass + 1
  else
    fail = fail + 1
    print(string.format("FAIL %s: got %q, expected %q", name, tostring(got), tostring(expect)))
  end
end

-- Primitives
check("decode true", json.decode("true"), true)
check("decode false", json.decode("false"), false)
check("decode null", json.decode("null"), nil)
check("decode int", json.decode("42"), 42)
check("decode float", json.decode("3.14"), 3.14)
check("decode negative", json.decode("-7"), -7)
check("decode string", json.decode('"hello"'), "hello")

-- Array decode (tables compare by identity in Lua, so check contents + length)
local arr = json.decode("[1,2,3]")
check("decode array len", #arr, 3)
check("decode array [1]", arr[1], 1)
check("decode array [2]", arr[2], 2)
check("decode array [3]", arr[3], 3)
check("decode empty array len", #json.decode("[]"), 0)

-- Encode
check("encode nil", json.encode(nil), "null")
check("encode true", json.encode(true), "true")
check("encode false", json.encode(false), "false")
check("encode int", json.encode(42), "42")
check("encode string", json.encode("hi"), '"hi"')
check("encode empty array", json.encode({}), "[]")
check("encode array", json.encode({1,2,3}), "[1,2,3]")

-- UTF-8 \u escapes (the bug: code >= 128 was replaced with "?")
check("u ascii", json.decode('"\\u0041"'), "A")
check("u 2-byte", json.decode('"\\u00e9"'), "\xc3\xa9")           -- e-acute
check("u 3-byte", json.decode('"\\u2192"'), "\xe2\x86\x92")       -- right arrow
check("surrogate pair", json.decode('"\\ud83d\\ude00"'), "\xf0\x9f\x98\x80")  -- U+1F600

-- Round-trip multilingual
local s = "caf\xc3\xa9 \xe2\x86\x92 \xf0\x9f\x98\x80"
check("round-trip utf8", json.decode(json.encode(s)), s)

-- String escapes on encode
check("encode quote", json.encode('"'), '"\\""')
check("encode backslash", json.encode("\\"), '"\\\\"')
check("encode newline", json.encode("\n"), '"\\n"')
check("encode tab", json.encode("\t"), '"\\t"')
check("encode control", json.encode("\x01"), '"\\u0001"')

-- Error paths (should throw)
local function p(fn) local ok, err = pcall(fn); return ok, err end
local ok
ok = p(function() json.decode("{") end); check("err unterminated obj", ok, false)
ok = p(function() json.decode("[1,]") end); check("err trailing comma", ok, false)
ok = p(function() json.decode('"\\u00"') end); check("err short u escape", ok, false)
ok = p(function() json.decode("42x") end); check("err trailing data", ok, false)
ok = p(function() json.encode(math.huge) end); check("err encode huge", ok, false)

print(string.format("\n%d passed, %d failed", pass, fail))
if fail > 0 then os.exit(1) end
