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
    ph_name = groove.get("_power_hand", p["power_hand"])
    ph_pitch_role, ph_var_roles = POWER_HAND.get(ph_name, (None, []))

    for bar in range(bars):
        bar_base_qn = bar_offset_qn + bar * blq
        limb = [0] * (steps_in_bar + 1)
        is_final = (bar == bars - 1)
        apply_fill = p["fills"] and is_final and groove.get("_fill", True)
        turnaround = steps_in_bar - steps_per_beat
        fill_zone = set(range(turnaround, steps_in_bar)) if apply_fill else set()

        for i in range(1, steps_in_bar + 1):
            si = i - 1
            if si in fill_zone:
                continue
            sidx = (si % pat_len)
            k = groove["kick"][sidx]
            s = groove["snare"][sidx]
            pos_qn = bar_base_qn + si * sq
            if k != "-":
                foot = "KICK_L" if i % 2 == 0 else "KICK_R"
                base = 127 if k == "K" else 110
                vel = humanize_velocity(base, p["velocity_mode"], p["humanize"],
                                        foot == "KICK_L", rng)
                _emit(events, pos_qn, drum_map[foot], vel, p, rng, bar, si, offset_cache)
                limb[si] += 1
            if s != "-":
                if s == "S": role, base = "SNARE", 127
                elif s == "s": role, base = "SNARE", 110
                elif s == "g": role, base = "SNARE_GHOST", rng.randint(25, 45)
                elif s == "f": role, base = "SNARE_FLAM", 115
                else: role = None
                if role:
                    vel = humanize_velocity(base, p["velocity_mode"], p["humanize"], False, rng)
                    _emit(events, pos_qn, drum_map[role], vel, p, rng, bar, si, offset_cache)
                    limb[si] += 1

        if apply_fill:
            for step in range(turnaround, steps_in_bar):
                tom = "TOM_1" if step % 2 == 0 else "TOM_2"
                vel = humanize_velocity(p["fill_velocity"], p["velocity_mode"], p["humanize"], False, rng)
                _emit(events, bar_base_qn + step * sq, drum_map[tom], vel, p, rng, bar, step, offset_cache)
            _emit(events, bar_base_qn + steps_in_bar * sq, drum_map["CRASH_R"], 127, p, rng, bar, steps_in_bar, offset_cache)

        if ph_pitch_role:
            spacing = p["ph_spacing_qn"]
            ph_steps = int(round(blq / spacing))
            for j in range(ph_steps):
                pos_qn = j * spacing
                nearest = int(round(pos_qn / sq))
                if nearest in fill_zone or limb[min(nearest, steps_in_bar)] >= 2:
                    continue
                pitch = drum_map[ph_pitch_role]
                if ph_var_roles and rng.randint(1, 100) < p["ph_variance"]:
                    pitch = drum_map[ph_var_roles[rng.randint(0, len(ph_var_roles) - 1)]]
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
