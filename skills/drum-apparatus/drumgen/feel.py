LEFT_FOOT_STRENGTH = 92  # percent, from the Lua


def humanize_velocity(base, mode, humanize, is_left, rng):
    final = base
    if mode == 0:
        final = base * 0.85
    elif mode == 2:
        final = base * 1.1
    if is_left:
        final = final * (LEFT_FOOT_STRENGTH / 100)
    var = int(20 * (humanize / 100))  # floor
    final = final + rng.randint(-var, var)
    return max(1, min(127, int(final)))


def timing_offset_seconds(humanize, push_pull, rng):
    drift = (rng.random() - 0.5) * (humanize / 100) * 0.025
    push = (push_pull / 100) * 0.02
    return drift - push


def offset_to_ticks(offset_sec, tempo, ppq):
    return int(round(offset_sec * (tempo / 60.0) * ppq))
