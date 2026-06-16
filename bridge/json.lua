local json = {}

local function escape_string(value)
  local replacements = {
    ['"'] = '\\"',
    ["\\"] = "\\\\",
    ["\b"] = "\\b",
    ["\f"] = "\\f",
    ["\n"] = "\\n",
    ["\r"] = "\\r",
    ["\t"] = "\\t",
  }
  return '"' .. tostring(value):gsub('[%z\1-\31\\"]', function(char)
    return replacements[char] or string.format("\\u%04x", char:byte())
  end) .. '"'
end

local function is_array(value)
  local max = 0
  local count = 0
  for key, _ in pairs(value) do
    if type(key) ~= "number" or key < 1 or key % 1 ~= 0 then
      return false
    end
    if key > max then max = key end
    count = count + 1
  end
  return max == count
end

function json.encode(value)
  local kind = type(value)
  if kind == "nil" then
    return "null"
  elseif kind == "boolean" then
    return value and "true" or "false"
  elseif kind == "number" then
    if value ~= value or value == math.huge or value == -math.huge then
      error("Cannot encode non-finite number")
    end
    return tostring(value)
  elseif kind == "string" then
    return escape_string(value)
  elseif kind == "table" then
    local parts = {}
    if is_array(value) then
      for i = 1, #value do
        parts[#parts + 1] = json.encode(value[i])
      end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    for key, item in pairs(value) do
      if type(key) ~= "string" then
        error("Cannot encode object with non-string key")
      end
      parts[#parts + 1] = escape_string(key) .. ":" .. json.encode(item)
    end
    return "{" .. table.concat(parts, ",") .. "}"
  end
  error("Cannot encode " .. kind)
end

local Parser = {}
Parser.__index = Parser

function Parser:new(text)
  return setmetatable({ text = text, pos = 1, len = #text }, self)
end

function Parser:peek()
  return self.text:sub(self.pos, self.pos)
end

function Parser:next()
  local char = self:peek()
  self.pos = self.pos + 1
  return char
end

function Parser:skip_ws()
  while self.pos <= self.len do
    local char = self:peek()
    if char ~= " " and char ~= "\n" and char ~= "\r" and char ~= "\t" then
      break
    end
    self.pos = self.pos + 1
  end
end

function Parser:expect(text)
  if self.text:sub(self.pos, self.pos + #text - 1) ~= text then
    error("Expected " .. text .. " at byte " .. self.pos)
  end
  self.pos = self.pos + #text
end

function Parser:parse_string()
  self:expect('"')
  local out = {}
  while self.pos <= self.len do
    local char = self:next()
    if char == '"' then
      return table.concat(out)
    elseif char == "\\" then
      local esc = self:next()
      if esc == '"' or esc == "\\" or esc == "/" then
        out[#out + 1] = esc
      elseif esc == "b" then
        out[#out + 1] = "\b"
      elseif esc == "f" then
        out[#out + 1] = "\f"
      elseif esc == "n" then
        out[#out + 1] = "\n"
      elseif esc == "r" then
        out[#out + 1] = "\r"
      elseif esc == "t" then
        out[#out + 1] = "\t"
      elseif esc == "u" then
        local hex = self.text:sub(self.pos, self.pos + 3)
        if not hex:match("^%x%x%x%x$") then
          error("Invalid unicode escape at byte " .. self.pos)
        end
        local code = tonumber(hex, 16)
        self.pos = self.pos + 4
        if code < 128 then
          out[#out + 1] = string.char(code)
        else
          out[#out + 1] = "?"
        end
      else
        error("Invalid escape at byte " .. self.pos)
      end
    else
      out[#out + 1] = char
    end
  end
  error("Unterminated string")
end

function Parser:parse_number()
  local start = self.pos
  local char = self:peek()
  if char == "-" then self.pos = self.pos + 1 end
  while self:peek():match("%d") do self.pos = self.pos + 1 end
  if self:peek() == "." then
    self.pos = self.pos + 1
    while self:peek():match("%d") do self.pos = self.pos + 1 end
  end
  char = self:peek()
  if char == "e" or char == "E" then
    self.pos = self.pos + 1
    char = self:peek()
    if char == "+" or char == "-" then self.pos = self.pos + 1 end
    while self:peek():match("%d") do self.pos = self.pos + 1 end
  end
  local raw = self.text:sub(start, self.pos - 1)
  local value = tonumber(raw)
  if value == nil then error("Invalid number at byte " .. start) end
  return value
end

function Parser:parse_array()
  self:expect("[")
  local out = {}
  self:skip_ws()
  if self:peek() == "]" then
    self.pos = self.pos + 1
    return out
  end
  while true do
    out[#out + 1] = self:parse_value()
    self:skip_ws()
    local char = self:next()
    if char == "]" then
      return out
    elseif char ~= "," then
      error("Expected , or ] at byte " .. (self.pos - 1))
    end
  end
end

function Parser:parse_object()
  self:expect("{")
  local out = {}
  self:skip_ws()
  if self:peek() == "}" then
    self.pos = self.pos + 1
    return out
  end
  while true do
    self:skip_ws()
    if self:peek() ~= '"' then
      error("Expected object key at byte " .. self.pos)
    end
    local key = self:parse_string()
    self:skip_ws()
    self:expect(":")
    out[key] = self:parse_value()
    self:skip_ws()
    local char = self:next()
    if char == "}" then
      return out
    elseif char ~= "," then
      error("Expected , or } at byte " .. (self.pos - 1))
    end
  end
end

function Parser:parse_value()
  self:skip_ws()
  local char = self:peek()
  if char == '"' then
    return self:parse_string()
  elseif char == "{" then
    return self:parse_object()
  elseif char == "[" then
    return self:parse_array()
  elseif char == "t" then
    self:expect("true")
    return true
  elseif char == "f" then
    self:expect("false")
    return false
  elseif char == "n" then
    self:expect("null")
    return nil
  elseif char == "-" or char:match("%d") then
    return self:parse_number()
  end
  error("Unexpected character at byte " .. self.pos)
end

function json.decode(text)
  local parser = Parser:new(text)
  local value = parser:parse_value()
  parser:skip_ws()
  if parser.pos <= parser.len then
    error("Trailing data at byte " .. parser.pos)
  end
  return value
end

return json
