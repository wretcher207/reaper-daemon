"""Fix 7 (2026-07-02 review): "short lanes loop" must actually loop.

The old tiling (`cells * (total // len(cells))`) rendered ZERO notes when a
lane was longer than the section (32 cells in bars=1) and silently dropped the
tail when lengths didn't divide (32 cells in bars=3 lost bar 3).
"""
import random

from drumgen.catalog import load_maps
from drumgen.groovekit import _tile, parse_dsl, render


def _render(dsl, seed=1):
    d = parse_dsl(dsl)
    params = {"tempo": d["tempo"], "ppq": d["ppq"], "map": d["map"], "humanize": 0}
    return render(d["sections"], params, random.Random(seed)), load_maps()[d["map"]], d


def test_tile_shorter_lane_repeats():
    assert _tile(list("x."), 8) == list("x.x.x.x.")


def test_tile_partial_repeat_when_lengths_dont_divide():
    assert _tile(list("x.."), 8) == list("x..x..x.")


def test_tile_longer_lane_truncates():
    assert _tile(list("x.x.abcd"), 4) == list("x.x.")


def test_tile_exact_length_unchanged():
    assert _tile(list("x.x."), 4) == list("x.x.")


TWO_BAR_LANE = "x...x...x...x...x...x...x...x..."[:32]  # 32 cells, 8 hits


def test_lane_longer_than_section_still_renders():
    # 32-cell kick lane in a bars=1 grid-16 section: old code rendered NOTHING.
    dsl = ("@tempo 144\n@map RS Monarch\n[v] bars=1 feel=mf\ngrid 16\n"
           f"kick | {TWO_BAR_LANE} |\n")
    evs, dm, _ = _render(dsl)
    kicks = [e for e in evs if e["pitch"] == dm["KICK_R"]]
    assert len(kicks) == 4  # first bar's worth of the 2-bar pattern


def test_remainder_tiling_fills_the_tail_bar():
    # 32-cell lane in bars=3 (total 48): old code left bar 3 silent.
    dsl = ("@tempo 144\n@map RS Monarch\n[v] bars=3 feel=mf\ngrid 16\n"
           f"kick | {TWO_BAR_LANE} |\n")
    evs, dm, d = _render(dsl)
    sec = d["sections"][0]
    ppq = d["ppq"]
    ticks_per_bar = int(ppq * 4)
    bar3 = [e for e in evs if e["pitch"] == dm["KICK_R"]
            and e["tick"] >= 2 * ticks_per_bar]
    assert len(bar3) == 4  # bar 3 replays the pattern's first bar
    assert sec["bars"] == 3


def test_one_bar_lane_still_loops_across_bars():
    dsl = ("@tempo 144\n@map RS Monarch\n[v] bars=2 feel=mf\ngrid 16\n"
           "kick | x...x...x...x... |\n")
    evs, dm, _ = _render(dsl)
    kicks = [e for e in evs if e["pitch"] == dm["KICK_R"]]
    assert len(kicks) == 8
