#!/usr/bin/env python3
"""Record an agent's REAPER session through the MCP server and report on it.

    python tools/session.py start opus          # every MCP call is logged from now
    python tools/session.py stop                # stop logging, stamp the end time
    python tools/session.py report opus --project song.rpp [--template t.rpp]

The MCP server appends to logs/sessions/<run>.jsonl while logs/sessions/ACTIVE
names the run, so any client (Claude Code, Codex, Grok) is recorded the same way.
The report covers time, every call, every error, and checks the saved project
against the thall challenge rules (100 BPM, 68 bars, see PROMPT.md).
"""
import argparse
import collections
import json
import os
import re
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_DIR = os.path.join(ROOT, "logs", "sessions")
ACTIVE = os.path.join(SESSION_DIR, "ACTIVE")

BPM = 100
BAR_QN = 4
SECTIONS = [("Intro", 1, 8), ("Riff A", 9, 24), ("Riff B", 25, 32),
            ("Break", 33, 44), ("Build", 45, 48), ("Climax", 49, 64),
            ("Ending", 65, 68)]
HEAVY = [(9, 32), (49, 64)]
WRITE_TOOLS = re.compile(r"insert|groove|riff|band|humanize|midi|delete|item|automation")
SNARE = 26
GUITAR_RANGE = (25, 61)


def _meta_path(run):
    return os.path.join(SESSION_DIR, run + ".meta.json")


def _clock(t):
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _dur(s):
    s = int(round(s))
    return f"{s // 3600}h {s % 3600 // 60:02d}m {s % 60:02d}s" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def cmd_start(args):
    os.makedirs(SESSION_DIR, exist_ok=True)
    log = os.path.join(SESSION_DIR, args.run + ".jsonl")
    if os.path.exists(log) and not args.force:
        sys.exit(f"{log} already exists; pick another run name or pass --force")
    if args.force and os.path.exists(log):
        os.remove(log)
    with open(_meta_path(args.run), "w", encoding="utf-8") as f:
        json.dump({"run": args.run, "start": time.time(), "note": args.note}, f)
    with open(ACTIVE, "w", encoding="utf-8") as f:
        f.write(args.run)
    print(f"recording run '{args.run}' from {_clock(time.time())}")


def cmd_stop(args):
    try:
        run = open(ACTIVE, encoding="utf-8").read().strip()
    except OSError:
        sys.exit("no run is being recorded")
    os.remove(ACTIVE)
    meta = json.load(open(_meta_path(run), encoding="utf-8"))
    meta["stop"] = time.time()
    json.dump(meta, open(_meta_path(run), "w", encoding="utf-8"))
    print(f"stopped run '{run}' after {_dur(meta['stop'] - meta['start'])}")


# --------------------------------------------------------------------------
# Project parsing
# --------------------------------------------------------------------------

def parse_project(path):
    """Tracks with their FX names, VOLPAN and MIDI notes (bar-based, 1-indexed)."""
    tracks, tempo_points, header_tempo = [], 0, None
    stack, cur, item = [], None, None
    for line in open(path, encoding="utf-8", errors="replace"):
        s = line.strip()
        if s.startswith("<"):
            tag = s[1:].split(" ")[0]
            stack.append(tag)
            if tag == "TRACK":
                cur = {"name": None, "fx": [], "volpan": None, "notes": [], "items": 0}
                tracks.append(cur)
            elif tag == "ITEM" and cur is not None:
                item = {"pos": 0.0, "tpq": 960, "tick": 0, "on": {}}
                cur["items"] += 1
            elif tag.startswith("VST") and cur is not None and len(stack) == 4:
                m = re.match(r'<VST3?i? "([^"]+)"', s)
                if m:
                    cur["fx"].append(m.group(1))
            continue
        if s == ">":
            tag = stack.pop() if stack else None
            if tag == "ITEM":
                item = None
            elif tag == "TRACK":
                cur = None
            continue
        top = stack[-1] if stack else None
        if top == "REAPER_PROJECT" and s.startswith("TEMPO "):
            header_tempo = float(s.split()[1])
        elif top == "TEMPOENVEX" and s.startswith("PT "):
            tempo_points += 1
        elif top == "TRACK" and cur is not None:
            if s.startswith("NAME ") and cur["name"] is None:
                cur["name"] = s[5:].strip('"')
            elif s.startswith("VOLPAN "):
                cur["volpan"] = s
        elif top == "ITEM" and item is not None and s.startswith("POSITION "):
            item["pos"] = float(s.split()[1])
        elif top == "SOURCE" and item is not None:
            if s.startswith("HASDATA"):
                item["tpq"] = int(s.split()[2])
            elif s[:2] in ("E ", "e "):
                p = s.split()
                if len(p) < 5:
                    continue
                item["tick"] += int(p[1])
                status, pitch, vel = int(p[2], 16), int(p[3], 16), int(p[4], 16)
                qn = item["pos"] * BPM / 60 + item["tick"] / item["tpq"]
                kind = status & 0xF0
                if kind == 0x90 and vel > 0:
                    item["on"][pitch] = (qn, vel)
                elif kind == 0x80 or (kind == 0x90 and vel == 0):
                    if pitch in item["on"]:
                        start, v = item["on"].pop(pitch)
                        cur["notes"].append({"pitch": pitch, "vel": v,
                                             "start": start, "end": qn})
    return {"tracks": tracks, "header_tempo": header_tempo, "tempo_points": tempo_points}


def bar_of(qn):
    return int(qn // BAR_QN) + 1


def check_rules(proj, template):
    out, by = [], {t["name"]: t for t in proj["tracks"]}
    ok = lambda good, text: out.append(("PASS" if good else "FAIL", text))

    ok(proj["header_tempo"] == BPM and proj["tempo_points"] <= 1,
       f"Tempo stayed {BPM} BPM (found {proj['header_tempo']}, {proj['tempo_points']} tempo-map points)")

    allowed = {"argent-l", "argent-r", "rs-drums-monarch", "mysteria-voices"}
    allowed |= {t["name"] for t in proj["tracks"] if "bass" in (t["name"] or "").lower()}
    stray = [t["name"] for t in proj["tracks"] if t["items"] and t["name"] not in allowed]
    ok(not stray, "MIDI only on the five instrument tracks" + (f" (also on: {', '.join(stray)})" if stray else ""))

    if template:
        tb = {t["name"]: t for t in template["tracks"]}
        changed = []
        if [t["name"] for t in template["tracks"]] != [t["name"] for t in proj["tracks"]]:
            changed.append("track list/order")
        for name, t in tb.items():
            p = by.get(name)
            if p and (p["fx"] != t["fx"] or p["volpan"] != t["volpan"]):
                changed.append(name)
        ok(not changed, "Tracks, plugins and faders untouched" + (f" (changed: {', '.join(changed)})" if changed else ""))

    all_notes = [n for t in proj["tracks"] for n in t["notes"]]
    if all_notes:
        last = max(n["end"] for n in all_notes)
        ok(last <= 68 * BAR_QN + 0.01, f"Song ends by bar 68 (last note ends in bar {bar_of(last - 1e-6)})")
        hit = [n for n in all_notes if abs(n["start"] - 64 * BAR_QN) < 0.25]
        ok(bool(hit), f"Final hit on bar 65 beat 1 ({len(hit)} notes there)")
        late = [n for n in all_notes if n["start"] > 64 * BAR_QN + 0.25]
        ok(not late, f"Nothing new after the bar 65 hit ({len(late)} later notes)")

    for name in ("argent-l", "argent-r"):
        t = by.get(name)
        if t and t["notes"]:
            bad = [n["pitch"] for n in t["notes"] if not GUITAR_RANGE[0] <= n["pitch"] <= GUITAR_RANGE[1]]
            # notes below 25 are Argent keyswitches, not pitches
            bad = [p for p in bad if p > GUITAR_RANGE[1]]
            ok(not bad, f"{name} stays at or below MIDI {GUITAR_RANGE[1]} ({len(bad)} notes above)")

    intro_drums = [n for n in by.get("rs-drums-monarch", {}).get("notes", []) if n["start"] < 8 * BAR_QN]
    ok(not intro_drums, f"No drums in the intro ({len(intro_drums)} hits)")

    drums = sorted(by.get("rs-drums-monarch", {}).get("notes", []), key=lambda n: n["start"])
    last_vel, repeats = {}, 0
    for n in drums:
        if last_vel.get(n["pitch"]) == n["vel"]:
            repeats += 1
        last_vel[n["pitch"]] = n["vel"]
    if drums:
        ok(repeats == 0, f"No back-to-back repeated drum velocities ({repeats} repeats in {len(drums)} hits)")

    snares = [n for n in drums if n["pitch"] == SNARE
              and any(a <= bar_of(n["start"]) <= b for a, b in HEAVY)]
    off = [n for n in snares if abs(n["start"] % BAR_QN - 2) > 0.05]
    if snares:
        ok(not off, f"Snare only on beat 3 in riffs and climax ({len(off)} of {len(snares)} snares elsewhere)")

    gl = by.get("argent-l", {}).get("notes", [])
    dense = []
    for a, b in HEAVY:
        for bar in range(a, b + 1):
            slots = {int((n["start"] - (bar - 1) * BAR_QN) * 4) for n in gl
                     if bar_of(n["start"]) == bar and n["pitch"] >= GUITAR_RANGE[0]}
            if len(slots) > 16 * 2 / 3:
                dense.append(bar)
    if gl:
        ok(not dense, f"At least a third of each riff bar is silent on argent-l ({len(dense)} bars too busy)")
    return out


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def cmd_report(args):
    log = os.path.join(SESSION_DIR, args.run + ".jsonl")
    recs = [json.loads(l) for l in open(log, encoding="utf-8") if l.strip()]
    meta = json.load(open(_meta_path(args.run), encoding="utf-8")) if os.path.exists(_meta_path(args.run)) else {}
    calls = [r for r in recs if r.get("method") == "tools/call"]
    clients = {json.dumps(r["client"]) for r in recs if r.get("client")}
    start = meta.get("start", calls[0]["t"] if calls else 0)
    stop = meta.get("stop", calls[-1]["t"] + calls[-1]["ms"] / 1000 if calls else start)

    L = [f"# Session report: {args.run}", ""]
    if meta.get("note"):
        L += [meta["note"], ""]
    L += ["## Time", "",
          f"- Started {_clock(start)}, stopped {_clock(stop)}: **{_dur(stop - start)}** total"]
    if calls:
        tool_time = sum(c["ms"] for c in calls) / 1000
        writes = [c for c in calls if WRITE_TOOLS.search(c["tool"] or "")]
        L += [f"- Time inside REAPER tool calls: {_dur(tool_time)}; the rest is the model thinking and writing",
              f"- First tool call after {_dur(calls[0]['t'] - start)}",
              f"- First MIDI/edit call after {_dur(writes[0]['t'] - start)}" if writes else "- No MIDI/edit calls"]
        gaps = sorted(((calls[i + 1]["t"] - (calls[i]["t"] + calls[i]["ms"] / 1000), i)
                       for i in range(len(calls) - 1)), reverse=True)[:5]
        if gaps:
            L.append("- Longest pauses between calls: " + ", ".join(
                f"{_dur(g)} before {calls[i + 1]['tool']} at {_clock(calls[i + 1]['t'])}" for g, i in gaps))
    L += ["", "## Calls", "", f"- Client: {', '.join(clients) or 'unknown'}",
          f"- MCP server restarts/connections: {len({r['pid'] for r in recs})}",
          f"- Tool calls: **{len(calls)}**, errors: **{sum(c['is_error'] for c in calls)}**", ""]
    counts = collections.Counter(c["tool"] for c in calls)
    errs = collections.Counter(c["tool"] for c in calls if c["is_error"])
    L += ["| Tool | Calls | Errors |", "|---|---|---|"]
    L += [f"| {t} | {n} | {errs.get(t, 0)} |" for t, n in counts.most_common()]

    errors = [c for c in calls if c["is_error"]]
    if errors:
        L += ["", "## Errors", ""]
        for c in errors:
            msg = re.sub(r"\s+", " ", c["text"])[:300]
            L.append(f"- {_clock(c['t'])} `{c['tool']}`: {msg}")
        retries = sum(1 for a, b in zip(calls, calls[1:]) if a["is_error"] and b["tool"] == a["tool"])
        L.append(f"\nRetried the same tool right after an error {retries} times.")

    if args.project:
        proj = parse_project(args.project)
        tmpl = parse_project(args.template) if args.template else None
        L += ["", "## What it wrote", "", "| Track | " + " | ".join(s[0] for s in SECTIONS) + " | Total |",
              "|---|" + "---|" * (len(SECTIONS) + 1)]
        for t in proj["tracks"]:
            if not t["notes"]:
                continue
            row = [sum(1 for n in t["notes"] if a <= bar_of(n["start"]) <= b) for _, a, b in SECTIONS]
            L.append(f"| {t['name']} | " + " | ".join(map(str, row)) + f" | {len(t['notes'])} |")
        L += ["", "Notes per section (note-ons). Drum and guitar counts include keyswitches.", "",
              "## Rule check", ""]
        rules = check_rules(proj, tmpl)
        L += [f"- {r}: {text}" for r, text in rules]
        L.append(f"\n**{sum(r == 'PASS' for r, _ in rules)} of {len(rules)} rules passed.**")

    L += ["", "## Full timeline", ""]
    for c in calls:
        flag = " **ERROR**" if c["is_error"] else ""
        args_s = json.dumps(c["args"])
        L.append(f"- {_clock(c['t'])} ({c['ms']:.0f} ms) `{c['tool']}`{flag} {args_s[:160]}")

    out = args.out or os.path.join(SESSION_DIR, args.run + "-report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("run")
    s.add_argument("--note", default="")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_start)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    r = sub.add_parser("report")
    r.add_argument("run")
    r.add_argument("--project")
    r.add_argument("--template")
    r.add_argument("--out")
    r.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
