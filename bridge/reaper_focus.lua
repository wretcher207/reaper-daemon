-- reaper_focus.lua -- the focus envelope
--
-- What the producer is looking at, read straight from the running REAPER rather
-- than fetched over the bridge. The panel attaches this to every prompt so the
-- model does not spend a tool call (and a bridge round trip, and his patience)
-- rediscovering the selected track.
--
-- Every field here is cheap: no render, no capture, no file read. This runs on
-- the UI thread inside a defer tick, so anything expensive would show up as
-- panel jitter.
--
-- Pure helpers are exported for the self-test. The REAPER calls are reached
-- through the global `reaper` table, which the test replaces with a stub.

local M = {}

-- REAPER's play state is a bitmask, not an enum: recording is 4 but arrives as
-- 5 (play|record) whenever the transport is actually rolling.
function M.transport_name(play_state)
  play_state = tonumber(play_state) or 0
  if play_state & 4 == 4 then return "recording" end
  if play_state & 2 == 2 then return "paused" end
  if play_state & 1 == 1 then return "playing" end
  return "stopped"
end

-- TimeMap2_timeToBeats reports measures 0-based. Producers count from 1, and so
-- does every other surface in this repo (the profiler's --start-bar, the DSL,
-- the SOP). Convert once, here, so no caller has to remember.
function M.bar_number(measures_zero_based)
  return math.floor(tonumber(measures_zero_based) or 0) + 1
end

-- A time selection of zero length is REAPER's way of saying "there isn't one",
-- and it is a real state: the user cleared it. Report nil rather than a
-- degenerate 0-0 range that reads like a selection at the very top of the song.
function M.is_real_range(start_time, end_time)
  start_time, end_time = tonumber(start_time), tonumber(end_time)
  if not start_time or not end_time then return false end
  return (end_time - start_time) > 1e-9
end

-- Round to a sane number of decimals for anything that ends up in a prompt.
-- The model does not need 14 significant figures of edit-cursor position, and
-- every digit is a token.
function M.round(value, places)
  local mult = 10 ^ (places or 3)
  local n = tonumber(value)
  if not n then return nil end
  return math.floor(n * mult + 0.5) / mult
end

local function time_to_bar(project, time)
  local _, measures = reaper.TimeMap2_timeToBeats(project, time)
  return M.bar_number(measures)
end

local function selected_tracks(project, limit)
  local out, count = {}, reaper.CountSelectedTracks(project)
  for i = 0, math.min(count, limit or 8) - 1 do
    local track = reaper.GetSelectedTrack(project, i)
    if track then
      local _, name = reaper.GetSetMediaTrackInfo_String(track, "P_NAME", "", false)
      out[#out + 1] = {
        name = name ~= "" and name or ("Track " ..
          math.floor(reaper.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER"))),
        guid = reaper.GetTrackGUID(track),
        index = math.floor(reaper.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER")),
      }
    end
  end
  return out, count
end

-- The envelope itself. Nothing in here may throw: a focus read that errors
-- would take down the defer loop and the panel with it, and the prompt it was
-- decorating is worth more than the decoration.
function M.envelope(project)
  project = project or 0
  local env = {}

  local ok, err = pcall(function()
    local _, project_name = reaper.EnumProjects(-1, "")
    env.project_name = (project_name or ""):match("([^/\\]+)$") or project_name
    env.project_dirty = reaper.IsProjectDirty(project) == 1

    local tracks, selected_count = selected_tracks(project, 8)
    env.selected_tracks = tracks
    env.selected_track_count = selected_count
    env.track_count = reaper.CountTracks(project)

    local cursor = reaper.GetCursorPosition()
    env.cursor_seconds = M.round(cursor, 3)
    env.cursor_bar = time_to_bar(project, cursor)

    local range_start, range_end = reaper.GetSet_LoopTimeRange(false, false, 0, 0, false)
    if M.is_real_range(range_start, range_end) then
      env.time_selection = {
        start_bar = time_to_bar(project, range_start),
        -- End bar is the bar the selection ENDS IN. A selection covering bars
        -- 9 through 16 ends at the downbeat of 17, which would read as bar 17
        -- and send the model one bar past what he highlighted.
        end_bar = time_to_bar(project, range_end - 1e-6),
        start_seconds = M.round(range_start, 3),
        end_seconds = M.round(range_end, 3),
      }
    end

    env.tempo = M.round(reaper.Master_GetTempo(), 3)
    local play_state = reaper.GetPlayState()
    env.transport = M.transport_name(play_state)
    if play_state & 1 == 1 then
      env.play_bar = time_to_bar(project, reaper.GetPlayPosition())
    end
  end)

  if not ok then
    -- Report the failure inside the envelope instead of raising. A prompt with
    -- a broken envelope still beats no prompt.
    env.error = tostring(err)
  end
  return env
end

-- A one-line rendering for the panel's focus strip. Kept separate from the
-- envelope so the display can be terse while the model still gets everything.
function M.summary(env)
  if not env then return "no focus" end
  if env.error then return "focus unavailable" end
  local parts = {}
  local tracks = env.selected_tracks or {}
  if #tracks == 0 then
    parts[#parts + 1] = "no track selected"
  elseif #tracks == 1 then
    parts[#parts + 1] = tracks[1].name
  else
    parts[#parts + 1] = string.format("%d tracks", env.selected_track_count or #tracks)
  end
  parts[#parts + 1] = "bar " .. tostring(env.cursor_bar or "?")
  if env.time_selection then
    parts[#parts + 1] = string.format("sel %d-%d",
      env.time_selection.start_bar, env.time_selection.end_bar)
  end
  if env.tempo then parts[#parts + 1] = string.format("%g BPM", env.tempo) end
  if env.transport and env.transport ~= "stopped" then
    parts[#parts + 1] = env.transport
  end
  if env.project_dirty then parts[#parts + 1] = "unsaved" end
  return table.concat(parts, "  |  ")
end

return M
