"""Section-by-section replace + read-back verification.

    from composer.verify import replace_section, check_lr_differ
    rep = replace_section(send, "Argent 3.5-L", events, bpm=140, start_bar=9,
                          nbars=8, ks_pitches=set(range(0, 25)), play_range=(25, 64),
                          expect_lengths=[e["duration"] for e in ev if e.get("tag")])
    assert rep["ok"], rep

Rules it enforces: only replace the ONE item that starts at the section (refuses
otherwise), delete from a few ms after the bar line so neighbours survive, item
count unchanged afterwards, note count equal to what was sent, played pitches
inside range, keyswitches present (if ks_pitches given and expected), all notes
inside the section's bars, lengths matching the plan within 3 ms, and optionally
drum golden-rule violations == 0. Insert with replace_existing=False and
start_bar for a fresh section. Never saves the project.
"""
import statistics


def _items(send, track):
    return send("get_items", {"target_track_name": track})["data"]["items"]


def replace_section(send, track, events, *, bpm, start_bar, nbars, beats_per_bar=4,
                    ks_pitches=(), play_range=None, expect_lengths=None,
                    replace_existing=True, require_ks=None, drum_golden_rule=False,
                    len_tol_ms=3.0):
    spb = 60.0 / bpm
    start_s = (start_bar - 1) * beats_per_bar * spb
    L = nbars * beats_per_bar * spb
    ks_pitches = set(ks_pitches)
    before = _items(send, track)
    mine = [i for i in before if abs(i["position"] - start_s) < 0.01]
    if replace_existing:
        if len(mine) != 1:
            return dict(ok=False, stage="precheck", items_at_start=len(mine))
        d = send("delete_items_in_range", {"target_track_name": track,
                 "range": {"type": "time", "seconds": start_s + 0.005, "length_seconds": L - 0.01},
                 "length_seconds": L - 0.01})
        mid = _items(send, track)
        if not d.get("ok") or len(mid) != len(before) - 1:
            return dict(ok=False, stage="delete", resp=d, before=len(before), after=len(mid))
        expected_items = len(before)
    else:
        expected_items = len(before) + 1
    r = send("insert_midi_events", {"target_track_name": track, "time_unit": "beats",
                                    "start_bar": start_bar, "length_beats": nbars * beats_per_bar,
                                    "events": events}, timeout_ms=60000)
    if not r.get("ok"):
        return dict(ok=False, stage="insert", resp=r)
    items = _items(send, track)
    idx = [i for i, it in enumerate(items) if abs(it["position"] - start_s) < 0.01]
    if len(idx) != 1:
        return dict(ok=False, stage="locate", matches=len(idx))
    rb = send("get_midi_notes", {"target_track_name": track, "item_index": idx[0],
                                  "max_notes": 8000, "include_bars": True,
                                  "include_note_names": False})["data"]
    q = rb["ppq_per_quarter"]
    notes = rb["notes"]
    ks = [n for n in notes if n["pitch"] in ks_pitches]
    pl = [n for n in notes if n["pitch"] not in ks_pitches]
    rep = dict(track=track, bars=f"{start_bar}-{start_bar + nbars - 1}",
               items_on_track=len(items), expected_items=expected_items,
               sent=len(events), read=rb["note_count"], keyswitches=len(ks))
    if pl:
        rep["played_range"] = f"{min(n['pitch'] for n in pl)}-{max(n['pitch'] for n in pl)}"
    bad = []
    if play_range:
        lo, hi = play_range
        rep["bad_pitches"] = sorted({n["pitch"] for n in pl if not lo <= n["pitch"] <= hi})
        bad += rep["bad_pitches"]
    rep["out_of_section"] = sum(1 for n in notes if not start_bar <= n["bar"] < start_bar + nbars)
    rl = sorted((n["end_ppq"] - n["ppq"]) / q for n in pl)
    if rl:
        rep["len_ms_median"] = round(statistics.median(rl) * spb * 1000)
        rep["len_ms_min"] = round(rl[0] * spb * 1000)
        rep["zero_length"] = sum(1 for x in rl if x <= 0)
    if expect_lengths is not None:
        el = sorted(expect_lengths)
        rep["max_len_mismatch_ms"] = (round(max(abs(a - b) for a, b in zip(rl, el)) * spb * 1000, 2)
                                      if len(rl) == len(el) and rl else None)
    if drum_golden_rule:
        from .cymbals import golden_rule  # noqa: F401 (path setup)
        from drumgen import goldenrule
        gn = [dict(index=i, pitch=n["pitch"], ppq=n["ppq"], velocity=n["velocity"]) for i, n in enumerate(notes)]
        rep["golden_rule_violations"] = len(goldenrule.violations(gn, {}))
    need_ks = bool(ks_pitches) if require_ks is None else require_ks
    rep["ok"] = (rb["note_count"] == len(events) and not bad and rep["out_of_section"] == 0
                 and len(items) == expected_items and (not need_ks or len(ks) > 0)
                 and rep.get("zero_length", 0) == 0
                 and (expect_lengths is None or (rep["max_len_mismatch_ms"] is not None
                                                 and rep["max_len_mismatch_ms"] < len_tol_ms))
                 and rep.get("golden_rule_violations", 0) == 0)
    rep["signature"] = [(n["ppq"], n["velocity"]) for n in pl]
    return rep


def check_lr_differ(rep_l, rep_r):
    """A double-tracked pair must be two takes, never a copy."""
    return rep_l["signature"] != rep_r["signature"]
