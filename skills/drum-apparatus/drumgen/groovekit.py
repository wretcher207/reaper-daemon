"""groovekit — a creative drum-MIDI engine.

Two layers:
  1. The caller authors NOTES in a step-grid DSL (see parse_dsl).
  2. The engine applies HUMANITY (velocity model + fatigue envelope + timing
     jitter with unison lock) automatically in render().

This is a NEW parallel path. It does NOT use grooves.json / find_groove /
render.py / generate.py. It only reuses smf.write_smf / smf.parse_smf and
catalog.load_maps, plus a handful of behaviours ported from feel.py/render.py:
  - ROLE_LIMB map
  - CLOSED_TO_OPEN (hat falls open when the left foot kicks anywhere in the bar)
  - LEFT_FOOT_STRENGTH = 92 (left-foot kick velocity * 0.92)
  - kick R/L alternation on consecutive kick hits
  - a per-(bar,step) timing offset cache so simultaneous hits share one offset
"""

import math
import random

from .catalog import load_maps
from .smf import write_smf

# ---------------------------------------------------------------------------
# Ported constants / behaviour
# ---------------------------------------------------------------------------

LEFT_FOOT_STRENGTH = 92  # percent; left-foot kick velocity * 0.92

NOTE_DUR_QN = 0.12  # like the old NOTE_DUR_QN

# Which limb plays each role (ported from render.py ROLE_LIMB).
ROLE_LIMB = {
    "KICK_R": "right_foot",
    "KICK_L": "left_foot",
    "HH_PEDAL": "left_foot",
    "SNARE": "left_hand",
    "SNARE_GHOST": "left_hand",
    "SNARE_FLAM": "left_hand",
    "SNARE_RIM": "left_hand",
    "HH_CLOSED_TIP": "right_hand",
    "HH_CLOSED_EDGE": "right_hand",
    "HH_OPEN_1": "right_hand",
    "HH_OPEN_2": "right_hand",
    "HH_OPEN_3": "right_hand",
    "RIDE_TIP": "right_hand",
    "RIDE_BELL": "right_hand",
    "RIDE_CRASH": "right_hand",
    "CRASH_R": "right_hand",
    "CRASH_L": "left_hand",
    "CHINA_R": "right_hand",
    "CHINA_L": "left_hand",
    "STACK": "right_hand",
    "SPLASH_R": "right_hand",
    "SPLASH_L": "left_hand",
    "BELL": "right_hand",
    "TOM_1": "right_hand",
    "TOM_2": "left_hand",
    "TOM_3": "left_hand",
    "TOM_4": "left_hand",
}

# Closed hihat needs the left foot on the pedal. If the left foot kicks anywhere
# in the bar, the pedal lifts and the hat rings open (ported from render.py).
CLOSED_TO_OPEN = {
    "HH_CLOSED_TIP": "HH_OPEN_1",
    "HH_CLOSED_EDGE": "HH_OPEN_1",
}

# ---------------------------------------------------------------------------
# Feel centers + DSL vocab
# ---------------------------------------------------------------------------

FEEL_CENTER = {
    "ppp": 30, "pp": 45, "p": 60, "mp": 72,
    "mf": 88, "f": 104, "ff": 117, "fff": 125,
}

# lane name -> (role, kind). kind drives special handling.
#   "kick"     -> alternating KICK_R/KICK_L
#   "kick_l"   -> always KICK_L
#   "snare"    -> SNARE with ghost/flam/accent variants
#   "hat_c"    -> closed hat that can fall open
#   plain      -> a fixed role
LANE_ROLE = {
    "kick": ("KICK_R", "kick"),
    "kick_l": ("KICK_L", "kick_l"),
    "snare": ("SNARE", "snare"),
    "hat_o": ("HH_OPEN_1", "hat_open"),
    "hat_c": ("HH_CLOSED_TIP", "hat_closed"),
    "hat_pedal": ("HH_PEDAL", "plain"),
    "ride": ("RIDE_TIP", "plain"),
    "ride_bell": ("RIDE_BELL", "plain"),
    "crash": ("CRASH_R", "plain"),
    "crash_r": ("CRASH_R", "plain"),
    "crash_l": ("CRASH_L", "plain"),
    "china": ("CHINA_R", "plain"),
    "splash": ("SPLASH_R", "plain"),
    "stack": ("STACK", "plain"),
    "bell": ("BELL", "plain"),
    "tom1": ("TOM_1", "plain"),
    "tom2": ("TOM_2", "plain"),
    "tom3": ("TOM_3", "plain"),
    "tom4": ("TOM_4", "plain"),
}

HAT_OPEN_VARS = ["HH_OPEN_1", "HH_OPEN_2", "HH_OPEN_3"]

# A cell is one of these characters.
CELL_REST = "."
CELL_HIT = "x"
CELL_ACCENT = "X"
CELL_GHOST = "o"
CELL_FLAM = "f"
VALID_CELLS = set(".xXof")


# ---------------------------------------------------------------------------
# DSL parsing
# ---------------------------------------------------------------------------

class DSLError(ValueError):
    pass


def parse_dsl(text):
    """Parse the step-grid DSL into a structured dict.

    Returns:
        {
          "tempo": int, "map": str, "ppq": int, "seed": int|None,
          "sections": [
            {"name": str, "bars": int, "feel": str, "grid": int,
             "lanes": [{"lane": str, "role": str, "kind": str,
                        "cells": [str, ...]}]},  # cells length == grid*bars
            ...
          ]
        }
    """
    tempo = None
    map_name = None
    ppq = 480
    seed = None
    sections = []
    cur = None
    cur_grid = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("@"):
            parts = line.split(None, 1)
            key = parts[0][1:].lower()
            val = parts[1].strip() if len(parts) > 1 else ""
            if key == "tempo":
                tempo = int(val)
            elif key == "map":
                map_name = val
            elif key == "ppq":
                ppq = int(val)
            elif key == "seed":
                seed = int(val)
            else:
                raise DSLError(f"unknown directive @{key}")
            continue

        if line.startswith("["):
            # [section_name] bars=N feel=mf
            close = line.index("]")
            name = line[1:close].strip()
            rest = line[close + 1:].strip()
            attrs = {}
            for tok in rest.split():
                if "=" in tok:
                    k, v = tok.split("=", 1)
                    attrs[k.strip().lower()] = v.strip()
            bars = int(attrs.get("bars", 1))
            feel = attrs.get("feel", "mf").lower()
            if feel not in FEEL_CENTER:
                raise DSLError(f"unknown feel '{feel}' in [{name}]")
            cur = {"name": name, "bars": bars, "feel": feel,
                   "grid": None, "lanes": []}
            cur_grid = None
            sections.append(cur)
            continue

        if line.lower().startswith("grid"):
            if cur is None:
                raise DSLError("grid before any [section]")
            cur_grid = int(line.split(None, 1)[1])
            cur["grid"] = cur_grid
            continue

        if "|" in line:
            if cur is None:
                raise DSLError("lane row before any [section]")
            if cur_grid is None:
                raise DSLError(f"lane row before grid in [{cur['name']}]")
            # <lane> | <cells> |
            head, _, tail = line.partition("|")
            lane = head.strip().lower()
            if lane not in LANE_ROLE:
                raise DSLError(f"unknown lane '{lane}' in [{cur['name']}]")
            # cells are everything up to the trailing pipe; spaces are visual.
            cellblock = tail.rsplit("|", 1)[0] if "|" in tail else tail
            cells = [c for c in cellblock if not c.isspace()]
            for c in cells:
                if c not in VALID_CELLS:
                    raise DSLError(
                        f"bad cell '{c}' in lane '{lane}' of [{cur['name']}]")
            n = len(cells)
            if n == 0 or n % cur_grid != 0:
                raise DSLError(
                    f"lane '{lane}' in [{cur['name']}] has {n} cells; "
                    f"must equal grid ({cur_grid}) or a multiple of it")
            role, kind = LANE_ROLE[lane]
            cur["lanes"].append(
                {"lane": lane, "role": role, "kind": kind, "cells": cells})
            continue

        raise DSLError(f"unparseable line: {raw!r}")

    if tempo is None:
        raise DSLError("missing @tempo")
    if map_name is None:
        raise DSLError("missing @map")
    # validate the map name against the catalog so a bad kit fails like every
    # other directive (clean DSLError) instead of leaking a bare KeyError out
    # of render().
    available_maps = load_maps()
    if map_name not in available_maps:
        known = ", ".join(sorted(available_maps))
        raise DSLError(f"unknown @map '{map_name}' (known maps: {known})")
    if not sections:
        raise DSLError("no [section] defined")
    for s in sections:
        if s["grid"] is None:
            raise DSLError(f"[{s['name']}] has no grid")
        if not s["lanes"]:
            raise DSLError(f"[{s['name']}] has no lanes")

    return {
        "tempo": tempo, "map": map_name, "ppq": ppq, "seed": seed,
        "sections": sections,
    }


# ---------------------------------------------------------------------------
# Humanize engine
# ---------------------------------------------------------------------------

def _is_run_lane(grid, steps_fired):
    """A RUN = >=6 consecutive hits on a lane at fast spacing.

    Fast spacing = grid>=16 and the lane fires on consecutive (or 32nd) steps.
    Any 32nd run qualifies. We detect the longest consecutive-step streak.
    """
    if grid < 16:
        return False
    longest = 0
    cur = 0
    prev = None
    for s in steps_fired:
        if prev is not None and s == prev + 1:
            cur += 1
        else:
            cur = 1
        longest = max(longest, cur)
        prev = s
    return longest >= 6


def _fatigue_factor(pos, run_len):
    """Ease-in decay across a run: lose up to ~20% of (base-1) by the end.

    pos in [0, run_len-1]. Returns a multiplicative fraction in (0,1] applied
    to (base-1). Uses (pos/(N-1))**1.5 so it sags rather than dropping
    immediately.
    """
    if run_len <= 1:
        return 0.0
    frac = (pos / (run_len - 1)) ** 1.5
    return 0.20 * frac  # fraction of (base-1) to subtract


def render(sections, params, rng):
    """Render parsed sections into MIDI events with full humanization.

    params keys: tempo, ppq, map (name), humanize (0..100 amount).
    Returns a list of {tick, pitch, vel, dur} suitable for write_smf.
    """
    ppq = params["ppq"]
    humanize = params.get("humanize", 20)
    drum_map = load_maps()[params["map"]]

    ticks_per_16th = ppq / 4.0
    dur_ticks = int(round(NOTE_DUR_QN * ppq))

    # Per-lane timeline of placed hits, so we can run fatigue + golden rule in
    # time order per lane after the per-hit passes. Keyed by (lane, role-channel
    # identity); we key by lane name so e.g. snare ghosts and accents share a
    # single fatigue/golden timeline (it is one physical drum).
    events = []
    # absolute bar index across the whole arrangement, for kick alternation +
    # bar-recovery in fatigue.
    abs_bar = 0
    cursor_qn = 0.0

    # global offset cache: (abs_bar, step_nominal_qn_key) -> offset ticks.
    # Hits that land on the same nominal absolute step share ONE offset
    # (unison lock) regardless of lane.
    offset_cache = {}

    def sample_offset(bar_idx, step_key):
        key = (bar_idx, step_key)
        if key not in offset_cache:
            if rng.random() < 0.15:
                off = 0.0
            else:
                bias = -0.01 * ticks_per_16th  # tiny pull bias
                sigma = 0.07 * ticks_per_16th
                off = rng.gauss(bias, sigma)
            offset_cache[key] = off
        return offset_cache[key]

    # We collect "hit intents" first (with base/articulation/metrical velocity),
    # grouped per lane in time order, then apply fatigue + jitter + golden rule,
    # then resolve to events with timing.
    for sec in sections:
        grid = sec["grid"]
        feel = sec["feel"]
        bars = sec["bars"]
        base_center = FEEL_CENTER[feel]
        step_qn = 4.0 / grid  # quarter-notes per step in 4/4
        steps_per_beat = grid / 4.0

        # left-foot bar detection: which bars have any left-foot kick.
        # A bar has a left kick if the kick lane alternates onto KICK_L within
        # the bar, or if a kick_l lane fires in the bar.
        # We compute per-lane expanded cells first.
        lane_cells = []
        for lane in sec["lanes"]:
            cells = lane["cells"]
            total = grid * bars
            if len(cells) == bars * grid:
                full = cells
            else:
                # repeat the one-bar (or multi) pattern across bars
                reps = total // len(cells)
                full = cells * reps
            lane_cells.append((lane, full))

        # Determine kick alternation per absolute step, and which bars get a
        # left kick. Kick alternates R/L on CONSECUTIVE kick hits across the
        # whole section stream.
        kick_lane_idx = None
        for idx, (lane, full) in enumerate(lane_cells):
            if lane["kind"] == "kick":
                kick_lane_idx = idx
                break
        kick_foot_at = {}  # global step index -> "KICK_R"/"KICK_L"
        bar_has_left = [False] * bars
        if kick_lane_idx is not None:
            _, full = lane_cells[kick_lane_idx]
            count = 0
            for gstep, c in enumerate(full):
                if c != CELL_REST:
                    foot = "KICK_R" if count % 2 == 0 else "KICK_L"
                    kick_foot_at[gstep] = foot
                    if foot == "KICK_L":
                        bar_has_left[gstep // grid] = True
                    count += 1
        # explicit kick_l lanes also lift the pedal in their bar
        for lane, full in lane_cells:
            if lane["kind"] == "kick_l":
                for gstep, c in enumerate(full):
                    if c != CELL_REST:
                        bar_has_left[gstep // grid] = True

        # Build per-lane intent lists.
        for lane, full in lane_cells:
            kind = lane["kind"]
            base_role = lane["role"]

            steps_fired = [g for g, c in enumerate(full) if c != CELL_REST]
            run_active = _is_run_lane(grid, steps_fired)

            # Precompute run grouping for fatigue: walk consecutive streaks.
            # We tag each fired step with (run_id, pos_in_run, run_len) for
            # streaks of consecutive steps; isolated hits get run_len 1.
            run_meta = {}
            streak = []
            def _flush(streak):
                n = len(streak)
                for pos, g in enumerate(streak):
                    run_meta[g] = (pos, n)
            prev = None
            for g in steps_fired:
                if prev is not None and g == prev + 1:
                    streak.append(g)
                else:
                    _flush(streak)
                    streak = [g]
                prev = g
            _flush(streak)

            intents = []  # ordered: dict(gstep, role, vel, is_left)
            for gstep, c in enumerate(full):
                if c == CELL_REST:
                    continue
                bar_idx = gstep // grid
                step_in_bar = gstep % grid

                # ----- resolve role + base velocity -----
                role = base_role
                is_left = False
                vel = float(base_center)

                if kind == "kick":
                    role = kick_foot_at.get(gstep, "KICK_R")
                    is_left = (role == "KICK_L")
                elif kind == "kick_l":
                    role = "KICK_L"
                    is_left = True
                elif kind == "snare":
                    if c == CELL_GHOST:
                        role = "SNARE_GHOST"
                    elif c == CELL_FLAM:
                        role = "SNARE_FLAM"
                    else:
                        role = "SNARE"
                elif kind == "hat_closed":
                    role = "HH_CLOSED_TIP"
                    if bar_has_left[bar_idx] and role in CLOSED_TO_OPEN:
                        role = CLOSED_TO_OPEN[role]
                elif kind == "hat_open":
                    role = "HH_OPEN_1"
                    # occasional variance to OPEN_2/OPEN_3
                    if rng.randint(1, 100) <= 18:
                        role = HAT_OPEN_VARS[rng.randint(1, 2)]

                # ----- (2) articulation -----
                if c == CELL_ACCENT:
                    vel += 18
                elif c == CELL_GHOST:
                    vel = float(rng.randint(28, 40))
                elif c == CELL_FLAM:
                    vel += 18 - 6  # slightly under an accent

                # ----- (3) metrical weight -----
                if c != CELL_GHOST:  # ghosts stay quiet
                    on_beat = abs(step_in_bar % steps_per_beat) < 1e-6
                    if on_beat:
                        vel += 6
                    if step_in_bar == 0:
                        vel += 10

                # ----- (4) left-foot kick scaling -----
                if is_left:
                    vel *= (LEFT_FOOT_STRENGTH / 100.0)

                intents.append({
                    "gstep": gstep, "bar": bar_idx, "step_in_bar": step_in_bar,
                    "role": role, "vel": vel, "is_left": is_left,
                    "cell": c,
                })

            # ----- (5) FATIGUE ENVELOPE on runs -----
            if run_active:
                # track first-hit-of-bar within the run for recovery
                seen_bar = set()
                for it in intents:
                    g = it["gstep"]
                    pos, rlen = run_meta.get(g, (0, 1))
                    if rlen >= 6:
                        # ease-in decay loses up to 20% of (base-1)
                        frac = _fatigue_factor(pos, rlen)
                        base_minus_1 = it["vel"] - 1.0
                        it["vel"] -= base_minus_1 * frac
                        # per-bar recovery: first run-hit of each new bar
                        b = it["bar"]
                        if b not in seen_bar:
                            it["vel"] += rng.uniform(8, 10)
                            seen_bar.add(b)
                        # small gaussian noise on top
                        it["vel"] += rng.gauss(0, 2.0)

            # ----- (6) gaussian jitter scaled by humanize -----
            jit = humanize / 100.0 * 14.0  # sigma scaling
            for it in intents:
                if it["cell"] == CELL_GHOST:
                    it["vel"] += rng.gauss(0, jit * 0.4)
                else:
                    it["vel"] += rng.gauss(0, jit)

            # ----- (7) GOLDEN RULE pass (per OUTPUT PITCH, time order) -----
            # No hit within 4 of the immediately preceding hit on the same
            # physical PITCH. This must be enforced on the stream the parser
            # actually sees (grouped by MIDI pitch), not on the interleaved
            # intent list, because one DSL lane can scatter across several
            # roles/pitches AND several roles can collapse onto one pitch:
            #   - hat_open -> HH_OPEN_1/2/3 are THREE distinct pitches, so two
            #     same-pitch hits separated by a different-pitch hat hit could
            #     otherwise land within 4 of each other.
            #   - kick -> KICK_R/KICK_L are TWO roles but ONE pitch (e.g. 24),
            #     so per-role grouping would leave adjacent same-pitch hits
            #     unprotected. Keying on the resolved pitch handles both.
            for it in intents:
                it["_pitch"] = drum_map[it["role"]]
            prev_v_by_pitch = {}
            for it in intents:
                pitch = it["_pitch"]
                prev_v = prev_v_by_pitch.get(pitch)
                v = it["vel"]
                if prev_v is not None and abs(v - prev_v) < 4:
                    if v >= prev_v:
                        v = prev_v + 4
                    else:
                        v = prev_v - 4
                # clamp here too so the neighbor relationship is on clamped vals
                v = max(1.0, min(127.0, v))
                # if clamping collapsed the gap (e.g. both at 127), push the
                # other way.
                if prev_v is not None and abs(v - prev_v) < 4:
                    if prev_v >= 124:
                        v = prev_v - 4
                    else:
                        v = prev_v + 4
                    v = max(1.0, min(127.0, v))
                it["vel"] = v
                prev_v_by_pitch[pitch] = v

            # ----- (8) resolve to events with timing -----
            for it in intents:
                pos_qn = cursor_qn + it["gstep"] * step_qn
                # unison lock key: nominal absolute step in 16th-units rounded
                step_key = round(pos_qn / step_qn)
                off = sample_offset(abs_bar + it["bar"], (step_key,))
                tick = int(round(pos_qn * ppq)) + int(round(off))
                pitch = it["_pitch"]
                events.append({
                    "tick": max(0, tick),
                    "pitch": pitch,
                    "vel": int(round(max(1, min(127, it["vel"])))),
                    "dur": dur_ticks,
                })

        cursor_qn += bars * 4.0
        abs_bar += bars

    return events


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------

def build(text, seed=None):
    """Parse DSL text and render events. Returns (events, info_dict).

    Seed precedence: explicit `seed` arg > @seed in DSL > random.
    """
    parsed = parse_dsl(text)
    if seed is None:
        seed = parsed["seed"]
    if seed is None:
        seed = random.randrange(1, 2**31 - 1)
    rng = random.Random(seed)
    params = {
        "tempo": parsed["tempo"],
        "ppq": parsed["ppq"],
        "map": parsed["map"],
        "humanize": 20,
    }
    events = render(parsed["sections"], params, rng)
    total_bars = sum(s["bars"] for s in parsed["sections"])
    info = {
        "tempo": parsed["tempo"],
        "ppq": parsed["ppq"],
        "map": parsed["map"],
        "seed": seed,
        "bars": total_bars,
        "notes": len(events),
        "sections": [s["name"] for s in parsed["sections"]],
    }
    return events, info


def build_midi(text, seed=None):
    """Parse + render + serialize to MIDI bytes. Returns (bytes, info)."""
    events, info = build(text, seed=seed)
    data = write_smf(events, ppq=info["ppq"], tempo=info["tempo"])
    return data, info
