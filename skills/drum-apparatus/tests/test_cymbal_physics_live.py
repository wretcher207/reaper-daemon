"""#2 cymbal-with-shell + #3 hat curve, in the LIVE groovekit.render engine
(the one `reaperd.py groove` uses). Rendered at humanize=0 so the multipliers
are exact, not buried under jitter."""
import random

from drumgen.groovekit import parse_dsl, render
from drumgen.catalog import load_maps


def _render(dsl, humanize=0, seed=1):
    d = parse_dsl(dsl)
    params = {"tempo": d["tempo"], "ppq": d["ppq"], "map": d["map"], "humanize": humanize}
    return render(d["sections"], params, random.Random(seed)), load_maps()[d["map"]]


HAT = "@tempo 144\n@map RS Monarch\n[v] bars=1 feel=mf\ngrid 8\n{lane} | x x x x x x x x |\n"


def test_hat_closed_softer_than_open_live():
    closed, dm = _render(HAT.format(lane="hat_c"))   # no kick -> stays closed
    opn, _ = _render(HAT.format(lane="hat_o"))
    c = [e["vel"] for e in closed if e["pitch"] in (dm["HH_CLOSED_TIP"], dm["HH_CLOSED_EDGE"])]
    o = [e["vel"] for e in opn if e["pitch"] in (dm["HH_OPEN_1"], dm["HH_OPEN_2"], dm["HH_OPEN_3"])]
    assert c and o
    assert sum(c) / len(c) < sum(o) / len(o)         # closed band sits below open


# crash at step 2 lands with a kick; crash at step 6 is alone. Same metrical
# weight (both on-beat, neither the downbeat), so any vel gap is the shell boost.
CYM = ("@tempo 144\n@map RS Monarch\n[v] bars=1 feel=mf\ngrid 8\n"
       "kick  | . . x . . . . . |\n"
       "crash | . . x . . . x . |\n")


def test_cymbal_louder_with_shell_live():
    evs, dm = _render(CYM)
    crashes = sorted((e["tick"], e["vel"]) for e in evs if e["pitch"] == dm["CRASH_R"])
    assert len(crashes) == 2
    with_shell, alone = crashes[0][1], crashes[1][1]   # step2 (with kick), step6 (alone)
    assert with_shell > alone
