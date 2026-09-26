-- FX-envelope automation for the Reaper Daemon bridge: read, snapshot, write,
-- verify and restore. reaper_agent_bridge.lua loads this file on every bridge
-- load and passes in the helpers it shares with the rest of the bridge.

local shared = ...
local finite_or_nil = shared.finite_or_nil
local bar_from_time = shared.bar_from_time
local find_fx = shared.find_fx
local find_fx_param = shared.find_fx_param
local time_from_point = shared.time_from_point
local envelope_shape = shared.envelope_shape
local resolve_position = shared.resolve_position
local fx_summary = shared.fx_summary
local AUTOMATION_MODE_NAMES = shared.AUTOMATION_MODE_NAMES

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
      -- Envelope active/visible/arm state is intentionally not restored: this
      -- REAPER build has no SetEnvelopeInfo_Value (verified against the
      -- installed API), and the write path never modifies that state, so
      -- there is nothing to undo on that axis.
    else
      -- ReaScript has no delete-envelope call, so an envelope this transaction
      -- created is restored to empty; because creation sits inside the
      -- transaction's undo block, one REAPER undo removes it.
      note = "envelope created by this transaction was emptied (ReaScript cannot delete an FX envelope); one REAPER undo deletes it"
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

function automation.apply_prepared(prepared)
  local envelope = prepared.envelope
  if not envelope then
    envelope = reaper.GetFXEnvelope(prepared.track, prepared.api_index,
      prepared.param_index, true)
    if not envelope then error("NO_ENVELOPE: Could not create FX envelope") end
    prepared.envelope = envelope
    prepared.created = true
    -- REAPER seeds a new envelope with a time-0 point at the parameter's
    -- current value. It is envelope state REAPER added, not caller data: keep
    -- it (the parameter holds its current value until the first written
    -- point) and count it in the intended state verification proves against.
    prepared.seed_points = automation.underlying_points(envelope)
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
  if prepared.snap.existed then
    for _, p in ipairs(prepared.snap.points) do
      if not prepared.deleted_lookup[p.index] then intended[#intended + 1] = p end
    end
  else
    for _, p in ipairs(prepared.seed_points or {}) do
      intended[#intended + 1] = p
    end
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
    local ok, err = pcall(automation.apply_prepared, p)
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

-- Points are stored whether or not the lane is switched on: an envelope left
-- at ACT 0 plays nothing, which reads exactly like a write that silently
-- failed. Two API gaps shape this command. There is no SetEnvelopeInfo_Value
-- on this build (see automation.restore), AND GetEnvelopeInfo_Value reads
-- B_ACTIVE/B_VISIBLE back as 0 for FX parameter envelopes regardless of the
-- real state. So the state chunk is the only trustworthy source and sink for
-- these flags, and this command both reads and verifies through it.
-- On the automation table, not a local: the bridge main chunk is at Lua's
-- 200-local limit (same reason the automation helpers are grouped there).
automation.FLAG_LINES = { "ACT", "VIS", "ARM" }

function automation.chunk_flags(text)
  -- Anchored to a line start so a flag name occurring inside another token
  -- cannot match. %f[%S] is the frontier between the leading newline's
  -- whitespace and the keyword.
  return {
    ACT = text:match("%f[%S]ACT%s+(%d)") == "1",
    VIS = text:match("%f[%S]VIS%s+(%d)") == "1",
    ARM = text:match("%f[%S]ARM%s+(%d)") == "1",
  }
end

function automation.set_state_command(command)
  local payload = command.payload or {}
  local track, track_index, api_index, fx_name, fx_scope, display_fx_index = find_fx(payload)
  local param_index, param_info = find_fx_param(track, api_index, payload)
  local envelope = reaper.GetFXEnvelope(track, api_index, param_index, false)
  if not envelope then
    error("NO_ENVELOPE: no FX envelope on that parameter; write automation points first")
  end
  local ok, chunk = reaper.GetEnvelopeStateChunk(envelope, "", false)
  if not ok or type(chunk) ~= "string" or chunk == "" then
    error("ENVELOPE_CHUNK_UNAVAILABLE: could not read the envelope state chunk")
  end

  local before_flags = automation.chunk_flags(chunk)
  local before = automation.envelope_state(envelope)
  before.active, before.visible, before.armed =
    before_flags.ACT, before_flags.VIS, before_flags.ARM

  local function requested(key, current)
    if payload[key] == nil then return current end
    return payload[key] == true
  end
  local target = {
    ACT = requested("active", before_flags.ACT),
    VIS = requested("visible", before_flags.VIS),
    ARM = requested("armed", before_flags.ARM),
  }

  -- report_only exists because every read path above is chunk-based: without
  -- it, inspecting the true flags would mean issuing a write.
  if payload.report_only == true then
    return {
      track = { index = track_index, name = select(2, reaper.GetTrackName(track, "")),
        guid = reaper.GetTrackGUID(track) },
      fx = { index = display_fx_index or api_index, scope = fx_scope or "track", name = fx_name },
      parameter = { index = param_index, name = param_info and param_info.name },
      report_only = true, state = before,
    }
  end

  -- ACT <on> <shape>, VIS <on> <lane> <inline>, ARM <on>. Only the leading
  -- flag moves; every trailing field REAPER wrote is preserved verbatim.
  local rewritten = {}
  for _, name in ipairs(automation.FLAG_LINES) do
    local on = target[name] and "1" or "0"
    local updated, count = chunk:gsub("%f[%S]" .. name .. "([^\r\n]*)", function(rest)
      local tail = rest:match("^%s+[%-%d]+(.*)$") or ""
      return name .. " " .. on .. tail
    end, 1)
    if count > 0 then
      chunk = updated
    else
      -- REAPER omits defaulted flags; add the line just after the header.
      chunk = chunk:gsub("(\n)", "%1" .. name .. " " .. on .. "\n", 1)
    end
    rewritten[name] = count
  end

  if not reaper.SetEnvelopeStateChunk(envelope, chunk, false) then
    error("ENVELOPE_CHUNK_REJECTED: REAPER refused the rewritten envelope state chunk")
  end

  local ok_after, chunk_after = reaper.GetEnvelopeStateChunk(envelope, "", false)
  if not ok_after then
    error("ENVELOPE_CHUNK_UNAVAILABLE: could not reread the envelope state chunk")
  end
  local after_flags = automation.chunk_flags(chunk_after)
  for _, name in ipairs(automation.FLAG_LINES) do
    if after_flags[name] ~= target[name] then
      error("ENVELOPE_STATE_UNCONFIRMED: " .. name .. " reads "
        .. tostring(after_flags[name]) .. " after asking for " .. tostring(target[name]))
    end
  end

  local after = automation.envelope_state(envelope)
  after.active, after.visible, after.armed =
    after_flags.ACT, after_flags.VIS, after_flags.ARM

  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()

  return {
    track = { index = track_index, name = select(2, reaper.GetTrackName(track, "")),
      guid = reaper.GetTrackGUID(track) },
    fx = { index = display_fx_index or api_index, scope = fx_scope or "track", name = fx_name },
    parameter = { index = param_index, name = param_info and param_info.name },
    before = before, after = after, lines_rewritten = rewritten,
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
  local body, prepared = automation.execute({ payload }, {})
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
  local body = automation.execute(writes, {})
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

return automation
