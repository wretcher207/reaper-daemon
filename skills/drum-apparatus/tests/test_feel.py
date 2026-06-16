import random
from drumgen.feel import humanize_velocity, timing_offset_seconds, offset_to_ticks


def test_velocity_clamped_and_centered():
    rng = random.Random(7)
    vals = [humanize_velocity(110, 1, 45, False, rng) for _ in range(500)]
    assert all(1 <= v <= 127 for v in vals)
    # var = floor(20*45/100) = 9 -> within +/-9 of 110
    assert min(vals) >= 110 - 9 and max(vals) <= 110 + 9


def test_left_foot_quieter():
    rng = random.Random(1)
    base = [humanize_velocity(110, 1, 0, False, rng) for _ in range(50)]
    rng = random.Random(1)
    left = [humanize_velocity(110, 1, 0, True, rng) for _ in range(50)]
    assert sum(left) < sum(base)  # x0.92


def test_humanize_zero_is_deterministic():
    rng = random.Random(99)
    assert timing_offset_seconds(0, 0, rng) == 0.0  # no drift, no push


def test_push_pull_shifts_negative():
    rng = random.Random(99)
    off = timing_offset_seconds(0, 100, rng)
    assert off == -0.02  # push = (100/100)*0.02, drift 0


def test_offset_to_ticks():
    # 0.0125s at 120bpm, ppq480 -> 0.0125 * 2 * 480 = 12
    assert offset_to_ticks(0.0125, 120, 480) == 12


def test_reproducible_with_seed():
    a = [timing_offset_seconds(45, 0, random.Random(5)) for _ in range(3)]
    b = [timing_offset_seconds(45, 0, random.Random(5)) for _ in range(3)]
    assert a == b
