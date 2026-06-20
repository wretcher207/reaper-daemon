def humanize_velocity(base, mode, humanize, is_left, rng):
    final = base
    if mode == 0:
        final = base * 0.85
    elif mode == 2:
        final = base * 1.1
    if is_left:
        # Weak-foot rule: the off (left) foot in a fast double-kick run lands
        # 7-9 lower than the right. Callers only pass is_left for genuine fast
        # runs (see render.py foot assignment), so no slow single kick is hit.
        final = final - rng.randint(7, 9)
    var = int(20 * (humanize / 100))  # floor
    final = final + rng.randint(-var, var)
    return max(1, min(127, int(final)))


def unique_velocity(pitch, vel, last_vel, rng):
    """Golden rule: no two consecutive hits in a lane (same pitch) ever share a
    velocity. If this hit matches the previous one on the same pitch, nudge it by
    1 — toward whichever side has headroom. Mutates last_vel."""
    if last_vel.get(pitch) == vel:
        if vel >= 127:
            vel -= 1
        elif vel <= 1:
            vel += 1
        else:
            vel += 1 if rng.random() < 0.5 else -1
    last_vel[pitch] = vel
    return vel


def timing_offset_seconds(humanize, push_pull, rng):
    drift = (rng.random() - 0.5) * (humanize / 100) * 0.025
    push = (push_pull / 100) * 0.02
    return drift - push


def offset_to_ticks(offset_sec, tempo, ppq):
    return int(round(offset_sec * (tempo / 60.0) * ppq))
