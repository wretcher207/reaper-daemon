"""groovekit — a creative drum-MIDI engine.

Two layers:
  1. The caller authors NOTES in a step-grid DSL (see parse_dsl).
  2. The engine applies HUMANITY (velocity model + fatigue envelope + timing
     jitter with unison lock) automatically in render().

It authors via the step-grid DSL, not a named-groove catalog: it does NOT use
grooves.json / find_groove. It reuses smf.write_smf / smf.parse_smf and
catalog.load_maps, plus a handful of behaviours (originally ported from the
now-removed feel/render modules):
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

# Fallbacks for sparse maps (e.g. a discovered map with no explicit snare
# ghost articulation). render() resolves each role through this table so a
# missing role routes to its parent piece instead of KeyError-ing.
ROLE_FALLBACKS = {
    "KICK_L": "KICK_R",
    "SNARE_FLAM": "SNARE", "SNARE_GHOST": "SNARE", "SNARE_RIM": "SNARE",
    "HH_CLOSED_EDGE": "HH_CLOSED_TIP",
    "HH_OPEN_2": "HH_OPEN_1", "HH_OPEN_3": "HH_OPEN_1",
    "HH_PEDAL": "HH_CLOSED_TIP",
    "RIDE_CRASH": "RIDE_TIP", "RIDE_BELL": "RIDE_TIP",
    "BIG_CRASH": "CRASH_R", "CRASH_L": "CRASH_R",
    "CHINA_L": "CHINA_R",
    "SPLASH_L": "SPLASH_R",
    "TOM_2": "TOM_1", "TOM_3": "TOM_2", "TOM_4": "TOM_3",
    "BELL": "RIDE_BELL",
    "STACK": "CHINA_R",
}

# ---------------------------------------------------------------------------
# Ported constants / behaviour
# ---------------------------------------------------------------------------

LEFT_FOOT_STRENGTH = 92  # percent; left-foot kick velocity * 0.92

NOTE_DUR_QN = 0.12  # like the old NOTE_DUR_QN

# Which limb plays each role.
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
# in the bar, the pedal lifts and the hat rings open.
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

# David keeps kicks punchy, not maxed — under ~114. General default, overridable
# per render with params["kick_vel_max"].
KICK_VEL_MAX = 114

# Per-kit voice preferences, keyed by map name. A kit can say "the snare I reach
# for is the rimshot, held in this band." Unset -> plain role, full 1-127 range.
VOICE_PROFILE = {
    # Monarch: David plays the rimshot snare (fuller, louder), 90-110. He played
    # 91/99/108/110 here and confirmed loud-as-fuck is the intent.
    "RS Monarch": {"snare_role": "SNARE_RIM", "snare_vel": (90, 110)},
}

# Hi-hat velocity curve: closed is played softer, open is louder. Multiplies the
# resolved hat velocity by role.
HAT_CURVE = {
    "HH_CLOSED_TIP": 0.8, "HH_CLOSED_EDGE": 0.8, "HH_PEDAL": 0.8,
    "HH_OPEN_1": 1.0, "HH_OPEN_2": 1.0, "HH_OPEN_3": 1.0,
}
# A cymbal struck together with a shell (kick/snare/tom) lands harder — a human
# pushes more force on the cymbal while also hitting the shell. Verified from
# David's own part (every crash/china landed on a kick, louder than the body).
CYMBAL_SHELL_BOOST = 1.12
_CYMBAL_PREFIXES = ("CRASH", "CHINA", "RIDE", "SPLASH", "STACK")
_SHELL_PREFIXES = ("KICK", "SNARE", "TOM")


def _is_cymbal(role):
    return role.startswith(_CYMBAL_PREFIXES) or role == "BELL"

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


def parse_dsl(text, default_map="GM Standard"):
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

    `@map` is optional: it defaults to `default_map` ("GM Standard") when the
    DSL omits it, so a beat works out of the box on any GM-compatible kit. An
    explicit `@map` in the DSL always wins.
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
                # Out-of-range tempi used to surface as raw tracebacks (0 =
                # ZeroDivisionError, negative = struct.error) or, below ~3.6
                # BPM, silently truncate the SMF tempo meta. Fail at parse.
                if not 20 <= tempo <= 999:
                    raise DSLError(f"@tempo {tempo} out of range (20-999)")
            elif key == "map":
                map_name = val
            elif key == "ppq":
                ppq = int(val)
                if not 24 <= ppq <= 32767:
                    raise DSLError(f"@ppq {ppq} out of range (24-32767)")
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
            if bars < 1:
                # bars=-1 used to rewind cursor_qn and corrupt positions.
                raise DSLError(f"bars={bars} in [{name}] (must be >= 1)")
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
        map_name = default_map  # @map is optional; fall back to the default kit
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

def _tile(cells, total):
    """Tile a lane's cells to exactly `total` steps ("short lanes loop").

    Handles every length mismatch: shorter lanes repeat (with a partial
    repeat at the end when the lengths don't divide evenly), longer lanes
    truncate to the section. The old `cells * (total // len(cells))` rendered
    ZERO steps when the lane was longer than the section (32 cells in a
    bars=1 grid-16 section) and silently dropped the tail bar when they
    didn't divide (32 cells in bars=3)."""
    return (cells * (total // len(cells) + 1))[:total]


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
    profile = VOICE_PROFILE.get(params["map"], {})
    kick_max = params.get("kick_vel_max", KICK_VEL_MAX)

    def pitch_for(role):
        r = role
        seen = set()
        while r not in drum_map and r in ROLE_FALLBACKS and r not in seen:
            seen.add(r)
            r = ROLE_FALLBACKS[r]
        if r not in drum_map:
            # Truly unmapped (no fallback chain reaches a real pitch). Drop
            # the note rather than crash — render() is best-effort on a
            # partial map and the caller's report flags what's missing.
            return None
        return drum_map[r]

    # Per-pitch velocity bounds: David's kick ceiling + the kit's snare band.
    # Keyed by resolved MIDI pitch so the golden-rule + final clamps respect them.
    bounds = {}
    for _kr in ("KICK_R", "KICK_L"):
        _kp = pitch_for(_kr)
        if _kp is not None:
            bounds[_kp] = (1, kick_max)
    if profile.get("snare_vel"):
        _sp = pitch_for(profile.get("snare_role", "SNARE"))
        if _sp is not None:
            bounds[_sp] = tuple(profile["snare_vel"])

    def clamp_v(pitch, v):
        lo, hi = bounds.get(pitch, (1, 127))
        return max(lo, min(hi, v))

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
    # Kick R/L alternation carries ACROSS sections (it's one pair of feet):
    # a per-section counter put two consecutive right-foot hits at every seam.
    kick_alt_count = 0
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
            total = grid * bars
            lane_cells.append((lane, _tile(lane["cells"], total)))

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
            count = kick_alt_count
            for gstep, c in enumerate(full):
                if c != CELL_REST:
                    foot = "KICK_R" if count % 2 == 0 else "KICK_L"
                    kick_foot_at[gstep] = foot
                    if foot == "KICK_L":
                        bar_has_left[gstep // grid] = True
                    count += 1
            kick_alt_count = count
        # explicit kick_l lanes also lift the pedal in their bar
        for lane, full in lane_cells:
            if lane["kind"] == "kick_l":
                for gstep, c in enumerate(full):
                    if c != CELL_REST:
                        bar_has_left[gstep // grid] = True

        # Steps where a shell (kick/snare/tom) fires — a cymbal landing on one of
        # these gets the with-shell velocity boost (#2).
        shell_steps = set()
        for lane, full in lane_cells:
            if lane["role"].startswith(_SHELL_PREFIXES):
                shell_steps.update(g for g, c in enumerate(full) if c != CELL_REST)

        # Build per-lane intent lists; they merge into one section stream for
        # the golden-rule pass below (two lanes can share one output pitch).
        section_intents = []
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
                        # kit's preferred main snare (Monarch -> rimshot)
                        role = profile.get("snare_role", "SNARE")
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

                # ----- (4) weak-foot kick drop (fast double-kick only) -----
                # The off (left) foot lands 7-9 lower than the right, but ONLY in
                # a genuine fast run (consecutive kicks = both feet truly used). An
                # isolated kick the alternation happened to label left stays full.
                if is_left:
                    _, _rlen = run_meta.get(gstep, (0, 1))
                    if _rlen >= 2:
                        vel -= rng.randint(7, 9)

                # ----- (4b) cymbal physics -----
                if role in HAT_CURVE:                          # hi-hat curve (#3)
                    vel *= HAT_CURVE[role]
                elif _is_cymbal(role) and gstep in shell_steps:  # cymbal+shell (#2)
                    vel *= CYMBAL_SHELL_BOOST

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
                        # per-bar recovery: first run-hit of each NEW bar. The
                        # very first hit of a run has no fatigue to recover
                        # from — boosting it (~9 over feel center) was a bug.
                        b = it["bar"]
                        if b not in seen_bar:
                            if pos > 0:
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

            section_intents.extend(intents)

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
        # Runs over ALL lanes merged in gstep order: two lanes sharing a pitch
        # (crash + crash_r) used to each get an independent pass, so cross-lane
        # neighbors could land at machine-gun gap 1.
        for it in section_intents:
            it["_pitch"] = pitch_for(it["role"])
        section_intents.sort(key=lambda it: it["gstep"])  # stable: lane order kept on ties
        prev_v_by_pitch = {}
        for it in section_intents:
            pitch = it["_pitch"]
            if pitch is None:
                continue  # unmapped role on a sparse/discovered map
            lo, hi = bounds.get(pitch, (1, 127))
            prev_v = prev_v_by_pitch.get(pitch)
            v = it["vel"]
            if prev_v is not None and abs(v - prev_v) < 4:
                v = prev_v + 4 if v >= prev_v else prev_v - 4
            # clamp to this pitch's band so the neighbor relationship holds
            # on bounded values (kick ceiling, snare band).
            v = max(lo, min(hi, v))
            # if clamping collapsed the gap (e.g. both pinned at the ceiling),
            # push the other way, still within the band.
            if prev_v is not None and abs(v - prev_v) < 4:
                v = prev_v - 4 if prev_v >= hi - 3 else prev_v + 4
                v = max(lo, min(hi, v))
            it["vel"] = v
            prev_v_by_pitch[pitch] = v

        # ----- (8) resolve to events with timing -----
        for it in section_intents:
            pitch = it["_pitch"]
            if pitch is None:
                continue
            pos_qn = cursor_qn + it["gstep"] * step_qn
            # unison lock key: nominal absolute step in 16th-units rounded
            step_key = round(pos_qn / step_qn)
            off = sample_offset(abs_bar + it["bar"], (step_key,))
            tick = int(round(pos_qn * ppq)) + int(round(off))
            events.append({
                "tick": max(0, tick),
                "pitch": pitch,
                "vel": int(round(clamp_v(pitch, it["vel"]))),
                "dur": dur_ticks,
            })

        cursor_qn += bars * 4.0
        abs_bar += bars

    return events


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------

# Focal voices that should land WITH something (a shell or another cymbal). A
# bare one is usually the bug David flags (#7: "you don't hear one snare hit
# alone"). Ostinato voices (hats, ride) play alone legitimately -> excluded.
FOCAL_LANES = ("snare", "crash", "crash_r", "crash_l", "china", "splash", "stack")


def exposed_focal_hits(parsed, limit=8):
    """Warnings for focal snare/cymbal hits that fire with nothing else on the
    same step. A flag for review, not an error."""
    warnings = []
    for sec in parsed["sections"]:
        grid, bars = sec["grid"], sec["bars"]
        total = grid * bars
        per_beat = max(1, grid // 4)
        step_pop = {}                       # gstep -> how many lanes fire there
        lanes_full = []
        for lane in sec["lanes"]:
            full = _tile(lane["cells"], total)
            steps = [g for g, c in enumerate(full) if c != CELL_REST]
            lanes_full.append((lane["lane"], steps))
            for g in steps:
                step_pop[g] = step_pop.get(g, 0) + 1
        for name, steps in lanes_full:
            if name not in FOCAL_LANES:
                continue
            for g in steps:
                if step_pop.get(g, 0) < 2:  # this focal hit is alone at its step
                    warnings.append(
                        f"[{sec['name']}] bare {name} at bar {g // grid + 1} "
                        f"beat {g % grid // per_beat + 1}.{g % per_beat + 1} "
                        f"— nothing struck with it (#7)")
    if len(warnings) > limit:
        warnings = warnings[:limit] + [f"... and {len(warnings) - limit} more bare focal hits"]
    return warnings


def build(text, seed=None, default_map="GM Standard"):
    """Parse DSL text and render events. Returns (events, info_dict).

    Seed precedence: explicit `seed` arg > @seed in DSL > random.
    default_map is the kit used when the DSL omits `@map` (an explicit `@map`
    in the DSL always wins).
    """
    parsed = parse_dsl(text, default_map=default_map or "GM Standard")
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
        "warnings": exposed_focal_hits(parsed),
    }
    return events, info


def build_midi(text, seed=None, default_map="GM Standard"):
    """Parse + render + serialize to MIDI bytes. Returns (bytes, info)."""
    events, info = build(text, seed=seed, default_map=default_map)
    data = write_smf(events, ppq=info["ppq"], tempo=info["tempo"])
    return data, info
