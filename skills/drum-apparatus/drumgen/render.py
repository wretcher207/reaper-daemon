import random
from .feel import humanize_velocity, timing_offset_seconds, offset_to_ticks
from .catalog import load_maps, find_groove

NOTE_DUR_QN = 0.12

POWER_HAND = {
    "hh_closed": ("HH_CLOSED_TIP", ["HH_CLOSED_EDGE"]),
    "hh_open":   ("HH_OPEN_1", ["HH_OPEN_2", "HH_OPEN_3"]),
    "ride":      ("RIDE_TIP", ["RIDE_BELL"]),
    "crash":     ("CRASH_R", []),
    "china":     ("CHINA_R", []),
    "stack":     ("STACK", []),
    "none":      (None, []),
}

# Which limb plays each role. Used for anatomical conflict detection.
ROLE_LIMB = {
    "KICK_R":        "right_foot",
    "KICK_L":        "left_foot",
    "HH_PEDAL":      "left_foot",
    "SNARE":         "left_hand",
    "SNARE_GHOST":   "left_hand",
    "SNARE_FLAM":    "left_hand",
    "SNARE_RIM":     "left_hand",
    "HH_CLOSED_TIP": "right_hand",
    "HH_CLOSED_EDGE":"right_hand",
    "HH_OPEN_1":     "right_hand",
    "HH_OPEN_2":     "right_hand",
    "HH_OPEN_3":     "right_hand",
    "RIDE_TIP":      "right_hand",
    "RIDE_BELL":     "right_hand",
    "RIDE_CRASH":    "right_hand",
    "CRASH_R":       "right_hand",
    "CRASH_L":       "left_hand",
    "CHINA_R":       "right_hand",
    "CHINA_L":       "left_hand",
    "STACK":         "right_hand",
    "SPLASH_R":      "right_hand",
    "SPLASH_L":      "left_hand",
    "BELL":          "right_hand",
    "TOM_1":         "right_hand",
    "TOM_2":         "left_hand",
    "TOM_3":         "left_hand",
    "TOM_4":         "left_hand",
}

# Closed hihat requires the left foot holding the pedal.
# When left_foot is on KICK_L, the pedal is up: hat falls open.
CLOSED_TO_OPEN = {
    "HH_CLOSED_TIP":  "HH_OPEN_1",
    "HH_CLOSED_EDGE": "HH_OPEN_1",
}

# Authored cymbal/accent lane: char -> (map role, base velocity). Used by
# breakdown grooves to place a cymbal on specific accents (the big stab, the
# snare smash) instead of running a grid ostinato. Char map mirrors the
# breakdown vocabulary David approved 2026-06-16.
CYMBAL_CHARS = {
    "X": ("CRASH_R", 125),
    "C": ("CHINA_R", 118),
    "p": ("SPLASH_R", 110),
    "r": ("RIDE_TIP", 95),
    "b": ("RIDE_BELL", 105),
}


def _emit(events, abs_qn, pitch, vel, p, rng, bar_idx, step_idx, offset_cache):
    key = (bar_idx, step_idx)
    if key not in offset_cache:
        offset_cache[key] = timing_offset_seconds(p["humanize"], p["push_pull"], rng)
    off_ticks = offset_to_ticks(offset_cache[key], p["tempo"], p["ppq"])
    tick = int(round(abs_qn * p["ppq"])) + off_ticks
    dur = int(round(NOTE_DUR_QN * p["ppq"]))
    events.append({"tick": max(0, tick), "pitch": pitch, "vel": vel, "dur": dur})


def render_section(groove, drum_map, bars, params, rng, bar_offset_qn=0.0):
    p = params
    events = []
    offset_cache = {}
    blq = p["bar_length_qn"]; sq = p["step_qn"]
    steps_in_bar = int(round(blq / sq))
    steps_per_beat = int(round(1.0 / sq))
    pat_len = len(groove["kick"])
    has_cymbal = bool(groove.get("cymbal"))
    cymbal_lane = groove.get("cymbal", "")
    ph_name = groove.get("_power_hand") or groove.get("power_hand") or p["power_hand"]
    ph_pitch_role, ph_var_roles = POWER_HAND.get(ph_name, (None, []))
    ph_is_hat = ph_name in ("hh_closed", "hh_open", "hh_pedal")
    if has_cymbal:
        ph_pitch_role = None  # authored cymbal lane replaces the grid ostinato

    for bar in range(bars):
        bar_base_qn = bar_offset_qn + bar * blq
        limb = [set() for _ in range(steps_in_bar + 1)]
        is_final = (bar == bars - 1)
        apply_fill = p["fills"] and is_final and groove.get("_fill", True)
        turnaround = steps_in_bar - steps_per_beat
        fill_zone = set(range(turnaround, steps_in_bar)) if apply_fill else set()

        for i in range(1, steps_in_bar + 1):
            si = i - 1
            if si in fill_zone:
                continue
            sidx = ((bar * steps_in_bar + si) % pat_len)  # continuous across bars
            k = groove["kick"][sidx]
            s = groove["snare"][sidx]
            cym = cymbal_lane[sidx] if has_cymbal else "-"
            pos_qn = bar_base_qn + si * sq
            if k != "-":
                foot = "KICK_L" if i % 2 == 0 else "KICK_R"
                base = 127 if k == "K" else 110
                vel = humanize_velocity(base, p["velocity_mode"], p["humanize"],
                                        foot == "KICK_L", rng)
                _emit(events, pos_qn, drum_map[foot], vel, p, rng, bar, si, offset_cache)
                limb[si].add(ROLE_LIMB.get(foot, foot))
            if s != "-":
                if s == "S": role, base = "SNARE", 127
                elif s == "s": role, base = "SNARE", 110
                elif s == "g": role, base = "SNARE_GHOST", rng.randint(25, 45)
                elif s == "f": role, base = "SNARE_FLAM", 115
                else: role = None
                if role:
                    vel = humanize_velocity(base, p["velocity_mode"], p["humanize"], False, rng)
                    _emit(events, pos_qn, drum_map[role], vel, p, rng, bar, si, offset_cache)
                    limb[si].add(ROLE_LIMB.get(role, role))
            if has_cymbal and cym != "-" and cym in CYMBAL_CHARS:
                role, base = CYMBAL_CHARS[cym]
                vel = humanize_velocity(base, p["velocity_mode"], p["humanize"], False, rng)
                _emit(events, pos_qn, drum_map[role], vel, p, rng, bar, si, offset_cache)
                limb[si].add(ROLE_LIMB.get(role, role))
                density = p.get("cymbal_density", 1)
                # Decay multiplier: first hit is the power slam, repeats are the ring/choke.
                decay = p.get("cymbal_decay", 0.72)
                for d in range(1, density):
                    rep_si = si + d * 2  # 8th-note spacing
                    if rep_si < steps_in_bar:
                        rep_vel = max(50, int(vel * (decay ** d)))
                        _emit(events, bar_base_qn + rep_si * sq, drum_map[role], rep_vel, p, rng, bar, rep_si, offset_cache)

        accent_role = p.get("accent_cymbal", "CRASH_R")
        accent_every = p.get("accent_every_bars", 1)
        if accent_role and accent_role != "none" and not has_cymbal and accent_role in drum_map:
            if bar % accent_every == 0:
                vel = humanize_velocity(127, p["velocity_mode"], p["humanize"], False, rng)
                _emit(events, bar_base_qn, drum_map[accent_role], vel, p, rng, bar, 0, offset_cache)

        if apply_fill:
            for step in range(turnaround, steps_in_bar):
                tom = "TOM_1" if step % 2 == 0 else "TOM_2"
                vel = humanize_velocity(p["fill_velocity"], p["velocity_mode"], p["humanize"], False, rng)
                _emit(events, bar_base_qn + step * sq, drum_map[tom], vel, p, rng, bar, step, offset_cache)
            _emit(events, bar_base_qn + steps_in_bar * sq, drum_map["CRASH_R"], 127, p, rng, bar, steps_in_bar, offset_cache)

        # Anatomical check: if left foot kicks at all in this bar, the hihat
        # pedal cannot be held closed. Left foot alternates every 16th in
        # double-kick runs so its steps are always adjacent to the power hand
        # grid, not coincident. Checking the whole bar is the correct model.
        bar_has_left_kick = any("left_foot" in l for l in limb)

        if ph_pitch_role:
            spacing = p["ph_spacing_qn"]
            ph_steps = int(round(blq / spacing))
            for j in range(ph_steps):
                pos_qn = j * spacing
                nearest = int(round(pos_qn / sq))
                step_limbs = limb[min(nearest, steps_in_bar)]
                if nearest in fill_zone or (ph_is_hat and len(step_limbs) >= 2):
                    continue
                # Closed hihat requires left foot on pedal.
                # If left foot kicks anywhere in this bar, hat falls open.
                ph_role = ph_pitch_role
                if ph_role in CLOSED_TO_OPEN and bar_has_left_kick:
                    ph_role = CLOSED_TO_OPEN[ph_role]
                pitch = drum_map[ph_role]
                if ph_var_roles and rng.randint(1, 100) < p["ph_variance"]:
                    var_role = ph_var_roles[rng.randint(0, len(ph_var_roles) - 1)]
                    var_role = CLOSED_TO_OPEN.get(var_role, var_role) if bar_has_left_kick else var_role
                    pitch = drum_map[var_role]
                _emit(events, bar_base_qn + pos_qn, pitch, p["ph_velocity"], p, rng, bar, 0, offset_cache)

    return events


def render_arrangement(sections, params):
    rng = random.Random(params["seed"])
    drum_map = load_maps()[params["map_name"]]
    events = []
    cursor_qn = 0.0
    for sec in sections:
        g = dict(find_groove(sec["groove"]))
        if "power_hand" in sec: g["_power_hand"] = sec["power_hand"]
        if "fill" in sec: g["_fill"] = sec["fill"]
        bars = sec["bars"]
        events += render_section(g, drum_map, bars, params, rng, bar_offset_qn=cursor_qn)
        cursor_qn += bars * params["bar_length_qn"]
    return events
