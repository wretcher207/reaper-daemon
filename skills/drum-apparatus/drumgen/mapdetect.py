"""mapdetect — turn a drum plugin's MIDI note-name dump into a groovekit map.

Pure functions. Given ``{pitch: name}`` from REAPER's ``GetTrackMIDINoteName``
(i.e. the ``.midnam`` the drum library installed), classify each note into a
groovekit ROLE and return a COMPLETE ``{role: pitch}`` map. Missing
articulations fall back to their parent piece so the engine never KeyErrors on
a sparse kit (a discovered map with no explicit "snare ghost" still routes
ghost cells to the snare pitch).

The classifier is keyword-based and deliberately liberal: it is meant to absorb
the naming variations across GGD, Superior Drummer, EZdrummer, BFD, Addictive
Drums, Kontakt drum kits, and General MIDI. It will NOT be perfect for every
library; ``match_roles`` returns a ``report`` so a human can eyeball the
result and hand-edit the saved map (``reaperd.py add-map``) when needed.

Public API:
    match_roles(notes) -> (map_dict, report)
    classify(name)     -> (family, modifier, tom_index) | None
"""

import re

from .catalog import ROLE_KEYS

# ---------------------------------------------------------------------------
# Family / modifier detection
# ---------------------------------------------------------------------------

# Order matters: more specific / rarer families first so a name like
# "Ride Bell" is not swallowed by the bare "bell" rule, and "China" is not
# mistaken for a crash.
FAMILY_PATTERNS = [
    ("china",  [r"\bchinas?\b", r"\bchinese\b"]),
    ("splash", [r"\bsplash\b"]),
    ("stack",  [r"\bstack\w*\b"]),
    ("ride",   [r"\bride\b"]),
    ("crash",  [r"\bcrash\w*\b"]),
    # hat is checked before "bell" because "hi-hat bell" is rare; ride bell
    # already routed above. Order: hat, then tom, then snare, then kick,
    # then standalone bell/cowbell.
    ("hat",    [r"hi\s*-?\s*hat", r"\bhats?\b", r"\bchh\b", r"\bohh\b",
                r"\bpedal\b", r"\bfoot\s*hat\b"]),
    ("tom",    [r"\btom\b", r"\brack\b", r"\bfloor\s*tom", r"\bft\b", r"\brt\b"]),
    ("snare",  [r"\bsnare\b", r"\bsd\b", r"\bsnr\b"]),
    ("kick",   [r"\bkick\b", r"\bbd\b", r"\bbass\s*drum\b", r"\bbdrum\b"]),
    ("bell",   [r"\bbell\b", r"\bcowbell\b"]),
]

# Modifier detection runs on the same lowercased name. Multiple modifiers can
# apply (e.g. a "rimshot" is also a snare), but we pick the single strongest
# articulation signal per family.
MODIFIERS = {
    "ghost":   r"\bghost\b",
    "flam":    r"\bflam\w*\b",
    "rim":     r"\brim\w*\b",
    "open":    r"\bopen\b",
    "closed":  r"\bclosed?\b",
    "pedal":   r"\bpedal\b",
    "bell":    r"\bbell\b",
    "crash":   r"\bcrash\b|\bedge\b|\bshank\b|\bshoulder\b",
}


def _norm(name):
    if not isinstance(name, str):
        return ""  # .midnam junk (numbers, nil) must not crash classify()
    return re.sub(r"\s+", " ", name.strip().lower())


def _has(pattern, text):
    return re.search(pattern, text) is not None


def classify(name):
    """Classify one note name.

    Returns ``(family, modifier|None, tom_index|None)`` or ``None`` if the
    name does not look like any drum piece we know. ``tom_index`` is 1..4 when
    a tom number can be read or inferred, else None (caller ranks by pitch).
    """
    t = _norm(name)
    if not t:
        return None

    family = None
    for fam, pats in FAMILY_PATTERNS:
        if any(_has(p, t) for p in pats):
            family = fam
            break
    if family is None:
        return None
    # A hi-hat articulation named after another family ("Hi-Hat Foot Splash")
    # is still a hat — prefer the hat family on co-occurrence.
    if family != "hat" and any(_has(p, t) for p in dict(FAMILY_PATTERNS)["hat"]):
        family = "hat"

    modifier = None
    tom_index = None

    if family == "tom":
        # explicit number wins: "Tom 1", "Rack Tom 2", "Floor Tom 3"
        m = re.search(r"(?:tom|rack|floor|rt|ft)\s*0*(\d)\b", t)
        if m:
            tom_index = max(1, min(4, int(m.group(1))))
        else:
            m2 = re.search(r"\b0*(\d)\s*(?:tom|rack|floor)\b", t)
            if m2:
                tom_index = max(1, min(4, int(m2.group(1))))
            elif _has(r"\bfloor\b|\bbottom\b", t):
                tom_index = 4
            elif _has(r"\blow\b|\blo\b", t):
                tom_index = 3
            elif _has(r"\bhigh\b|\bhi\b|\btop\b", t):
                tom_index = 1
            elif _has(r"\bmid\b", t):
                tom_index = 2
        # modifiers on toms are rare; ignore.

    elif family == "snare":
        if _has(MODIFIERS["ghost"], t):
            modifier = "ghost"
        elif _has(MODIFIERS["flam"], t):
            modifier = "flam"
        elif _has(MODIFIERS["rim"], t):
            modifier = "rim"
        else:
            modifier = None

    elif family == "hat":
        if _has(MODIFIERS["pedal"], t):
            modifier = "pedal"
        elif _has(MODIFIERS["open"], t):
            modifier = "open"
        elif _has(MODIFIERS["closed"], t) or _has(r"\bchh\b", t):
            modifier = "closed"
        # "edge" on a closed hat -> closed-edge variant
        if modifier == "closed" and _has(r"\bedge\b", t):
            modifier = "closed_edge"

    elif family == "ride":
        if _has(MODIFIERS["bell"], t):
            modifier = "bell"
        elif _has(MODIFIERS["crash"], t):
            modifier = "crash"
        else:
            modifier = "tip"

    return (family, modifier, tom_index)


# ---------------------------------------------------------------------------
# Role assembly
# ---------------------------------------------------------------------------

# Roles that have no direct counterpart in most libraries and are filled by
# falling back to a sibling role. Order = try in sequence.
FALLBACKS = {
    "KICK_L":          ["KICK_R"],
    "SNARE_FLAM":      ["SNARE"],
    "SNARE_GHOST":     ["SNARE"],
    "SNARE_RIM":       ["SNARE"],
    "HH_CLOSED_EDGE":  ["HH_CLOSED_TIP"],
    "HH_OPEN_2":       ["HH_OPEN_1"],
    "HH_OPEN_3":       ["HH_OPEN_1"],
    "RIDE_CRASH":      ["RIDE_TIP", "RIDE_BELL"],
    "BIG_CRASH":       ["CRASH_R", "CRASH_L"],
    "CRASH_L":         ["CRASH_R"],
    "CHINA_L":         ["CHINA_R"],
    "SPLASH_L":        ["SPLASH_R"],
    "TOM_2":           ["TOM_1"],
    "TOM_3":           ["TOM_2", "TOM_1"],
    "TOM_4":           ["TOM_3", "TOM_2", "TOM_1"],
    "BELL":            ["RIDE_BELL"],
    "STACK":           ["CHINA_R", "CRASH_R"],
    "HH_PEDAL":        ["HH_CLOSED_TIP"],
}


def _resolve_fallback(role, direct, seen):
    """Return a pitch for ``role`` via the fallback chain, or None."""
    for fb in FALLBACKS.get(role, []):
        if fb in direct:
            return direct[fb]
    return None


def match_roles(notes):
    """Classify a note-name dump into a complete groovekit role map.

    Args:
        notes: ``{pitch(int): name(str)}`` — e.g. from ``discover_drum_map``.

    Returns:
        (map_dict, report) where map_dict is ``{role: pitch}`` covering every
        ROLE_KEY that could be filled (directly or by fallback), and report is
        a dict with ``matched`` (role->pitch, direct), ``fallback`` (role->pitch,
        filled), ``unmatched`` (role list still missing), ``ignored`` (pitch->name
        that didn't classify), and ``complete`` (bool: every primary piece found).
    """
    if not isinstance(notes, dict) or not notes:
        return {}, {"matched": {}, "fallback": {}, "unmatched": list(ROLE_KEYS),
                    "ignored": {}, "complete": False, "reason": "NO_NOTES"}

    # Bucket classified notes by family.
    buckets = {
        "kick": [], "snare": [], "tom": [], "hat": [],
        "ride": [], "crash": [], "china": [], "splash": [],
        "stack": [], "bell": [],
    }
    ignored = {}
    for pitch, name in notes.items():
        try:
            p = int(pitch)
        except (TypeError, ValueError):
            continue
        if not (0 <= p <= 127):
            continue
        c = classify(name)
        if c is None:
            if name and name.strip():
                ignored[p] = name
            continue
        family, modifier, tom_index = c
        buckets[family].append({
            "pitch": p, "name": name, "mod": modifier, "tom": tom_index,
        })

    direct = {}

    def pick(bucket, keyfn):
        """Sort a bucket and return it (stable by keyfn then pitch)."""
        return sorted(bucket, key=lambda d: (keyfn(d), d["pitch"]))

    # --- kick -------------------------------------------------------------
    if buckets["kick"]:
        ks = sorted(buckets["kick"], key=lambda d: d["pitch"])
        direct["KICK_R"] = ks[0]["pitch"]
        direct["KICK_L"] = ks[-1]["pitch"] if len(ks) > 1 else ks[0]["pitch"]

    # --- snare (+ghost/flam/rim) -----------------------------------------
    if buckets["snare"]:
        ghosts = [d for d in buckets["snare"] if d["mod"] == "ghost"]
        flams = [d for d in buckets["snare"] if d["mod"] == "flam"]
        rims = [d for d in buckets["snare"] if d["mod"] == "rim"]
        mains = [d for d in buckets["snare"] if d["mod"] is None]
        main = pick(mains, lambda d: 0) if mains else pick(buckets["snare"], lambda d: 0)
        direct["SNARE"] = main[0]["pitch"]
        if ghosts:
            direct["SNARE_GHOST"] = ghosts[0]["pitch"]
        if flams:
            direct["SNARE_FLAM"] = flams[0]["pitch"]
        if rims:
            direct["SNARE_RIM"] = rims[0]["pitch"]

    # --- toms -------------------------------------------------------------
    # Assign by explicit number first, then rank the rest by pitch (highest =
    # TOM_1, descending) to fill gaps.
    toms = buckets["tom"]
    by_num = {}  # 1..4 -> pitch
    unnumbered = []
    for d in toms:
        if d["tom"] is not None:
            if d["tom"] not in by_num:
                by_num[d["tom"]] = d["pitch"]
        else:
            unnumbered.append(d)
    # rank unnumbered by pitch desc into slots 1..4 not already taken
    unnumbered.sort(key=lambda d: -d["pitch"])
    slot = 1
    for d in unnumbered:
        while slot <= 4 and slot in by_num:
            slot += 1
        if slot <= 4:
            by_num[slot] = d["pitch"]
            slot += 1
    for n in (1, 2, 3, 4):
        if n in by_num:
            direct["TOM_%d" % n] = by_num[n]

    # --- hats -------------------------------------------------------------
    closed = [d for d in buckets["hat"] if d["mod"] in ("closed", "closed_edge")]
    opens = [d for d in buckets["hat"] if d["mod"] == "open"]
    pedals = [d for d in buckets["hat"] if d["mod"] == "pedal"]
    if closed:
        edges = [d for d in closed if d["mod"] == "closed_edge"]
        tips = [d for d in closed if d["mod"] == "closed"]
        direct["HH_CLOSED_TIP"] = (tips or closed)[0]["pitch"]
        if edges:
            direct["HH_CLOSED_EDGE"] = edges[0]["pitch"]
    if opens:
        osorted = sorted(opens, key=lambda d: d["pitch"])
        direct["HH_OPEN_1"] = osorted[0]["pitch"]
        if len(osorted) > 1:
            direct["HH_OPEN_2"] = osorted[1]["pitch"]
        if len(osorted) > 2:
            direct["HH_OPEN_3"] = osorted[2]["pitch"]
    if pedals:
        direct["HH_PEDAL"] = pedals[0]["pitch"]

    # --- ride -------------------------------------------------------------
    if buckets["ride"]:
        bells = [d for d in buckets["ride"] if d["mod"] == "bell"]
        crashes = [d for d in buckets["ride"] if d["mod"] == "crash"]
        tips = [d for d in buckets["ride"] if d["mod"] == "tip"]
        direct["RIDE_TIP"] = (tips or buckets["ride"])[0]["pitch"]
        if bells:
            direct["RIDE_BELL"] = bells[0]["pitch"]
        if crashes:
            direct["RIDE_CRASH"] = crashes[0]["pitch"]

    # --- crash / china / splash / stack / bell ----------------------------
    if buckets["crash"]:
        cs = sorted(buckets["crash"], key=lambda d: d["pitch"])
        direct["CRASH_R"] = cs[0]["pitch"]
        if len(cs) > 1:
            direct["CRASH_L"] = cs[-1]["pitch"]
        if len(cs) > 2:
            direct["BIG_CRASH"] = cs[-1]["pitch"]
    if buckets["china"]:
        ch = sorted(buckets["china"], key=lambda d: d["pitch"])
        direct["CHINA_R"] = ch[0]["pitch"]
        if len(ch) > 1:
            direct["CHINA_L"] = ch[-1]["pitch"]
    if buckets["splash"]:
        sp = sorted(buckets["splash"], key=lambda d: d["pitch"])
        direct["SPLASH_R"] = sp[0]["pitch"]
        if len(sp) > 1:
            direct["SPLASH_L"] = sp[-1]["pitch"]
    if buckets["stack"]:  # sorted like the other cymbals: deterministic pick
        direct["STACK"] = sorted(buckets["stack"], key=lambda d: d["pitch"])[0]["pitch"]
    if buckets["bell"]:
        direct["BELL"] = sorted(buckets["bell"], key=lambda d: d["pitch"])[0]["pitch"]

    # --- fallbacks --------------------------------------------------------
    fallback = {}
    for role in ROLE_KEYS:
        if role in direct:
            continue
        p = _resolve_fallback(role, direct, set())
        if p is not None:
            fallback[role] = p

    full = dict(direct)
    full.update(fallback)
    unmatched = [r for r in ROLE_KEYS if r not in full]

    # "complete" = the four primary pieces (kick, snare, a hat or ride, a
    # crash) are all directly identified. That is the bar for a usable map.
    primaries = ["KICK_R", "SNARE"]
    has_time = ("HH_OPEN_1" in direct or "HH_CLOSED_TIP" in direct
                or "RIDE_TIP" in direct)
    has_crash = ("CRASH_R" in direct or "CHINA_R" in direct)
    complete = all(r in direct for r in primaries) and has_time and has_crash

    return full, {
        "matched": dict(sorted(direct.items())),
        "fallback": dict(sorted(fallback.items())),
        "unmatched": unmatched,
        "ignored": dict(sorted(ignored.items())),
        "complete": complete,
        "reason": None if complete else "PARTIAL_MAP",
    }


def format_report(notes, report, map_dict):
    """Human-readable summary for the discover-map CLI."""
    lines = []
    lines.append("Discovered %d named notes; classified into %d roles "
                 "(%d direct, %d fallback)."
                 % (len(notes), len(map_dict), len(report["matched"]),
                    len(report["fallback"])))
    if report["matched"]:
        lines.append("")
        lines.append("Direct matches:")
        for role, pitch in report["matched"].items():
            lines.append("  %-16s -> %3d" % (role, pitch))
    if report["fallback"]:
        lines.append("")
        lines.append("Filled by fallback (no direct articulation found):")
        for role, pitch in report["fallback"].items():
            lines.append("  %-16s -> %3d" % (role, pitch))
    if report["unmatched"]:
        lines.append("")
        lines.append("Still missing: " + ", ".join(report["unmatched"]))
    if report["ignored"]:
        lines.append("")
        lines.append("Unclassified notes (ignored):")
        for pitch, name in report["ignored"].items():
            lines.append("  %3d  %s" % (pitch, name))
    if not report["complete"]:
        lines.append("")
        lines.append("WARNING: partial map — primary pieces (kick/snare/hat-or-"
                     "ride) not all found directly.")
        lines.append("The library may not expose a .midnam. Try --channel, or")
        lines.append("hand-build the map with: reaperd.py add-map <name>")
    return "\n".join(lines)
