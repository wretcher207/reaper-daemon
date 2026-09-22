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

Every pick is made by name and pitch, never by the order the notes arrive in:
the bridge's JSON object order is not stable, so the same kit must always
produce the same map.

Public API:
    match_roles(notes) -> (map_dict, report)
    classify(name)     -> (family, modifier, tom_index) | None
"""

import re

from .catalog import ROLE_FALLBACKS, ROLE_KEYS

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
    # "Rack 2" and "Floor R1" (RS Monarch) are toms without the word "tom".
    ("tom",    [r"\btom\b", r"\brack\b", r"\bfloor\b", r"\bft\b", r"\brt\b"]),
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
    # A choke key grabs a ringing cymbal. It is a mute trigger, never a hit.
    "choke":   r"\bchoke[ds]?\b",
}

# A closed-hat stroke on the edge or with the stick's shoulder is the
# HH_CLOSED_EDGE variant of the tip stroke.
_HAT_EDGE = r"\bedge\b|\bshoulder\b|\bshank\b"

# Every word a bare hi-hat stroke name may use. RS Monarch names its hat
# strokes without the word "hat" ("Tight Closed Tip", "Small Open"). A name
# made only of these words, with "closed" or "open" among them, can only be a
# hi-hat stroke.
_BARE_HAT_WORDS = frozenset((
    "closed", "close", "open", "half", "semi", "quarter", "full",
    "tight", "loose", "normal", "small", "medium", "large",
    "tip", "shoulder", "shank", "edge", "bow", "shaft",
))


def _norm(name):
    if not isinstance(name, str):
        return ""  # .midnam junk (numbers, nil) must not crash classify()
    return re.sub(r"\s+", " ", name.strip().lower())


def _has(pattern, text):
    return re.search(pattern, text) is not None


def _hat_modifier(t):
    """Articulation of a normalized hi-hat name, or None."""
    if _has(r"\bsplash\b", t):
        return "foot_splash"  # "Pedal Splash" rings; it is not the pedal chick
    if _has(MODIFIERS["pedal"], t):
        return "pedal"
    if _has(MODIFIERS["open"], t):
        return "open"
    if _has(MODIFIERS["closed"], t) or _has(r"\bchh\b", t):
        return "closed_edge" if _has(_HAT_EDGE, t) else "closed"
    return None


def classify(name):
    """Classify one note name.

    Returns ``(family, modifier|None, tom_index|None)`` or ``None`` if the
    name does not look like any drum piece we know. ``tom_index`` is 1..4 when
    a tom number can be read or inferred, else None (caller ranks by pitch).
    A choke key returns modifier ``"choke"`` whatever its family.

    This sees one name at a time. A bare hi-hat stroke with no "hat" in its
    name ("Small Open") returns None here; ``match_roles`` adopts it when the
    rest of the kit makes it unambiguous.
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

    if _has(MODIFIERS["choke"], t):
        return (family, "choke", None)

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
        # A rim or flam stroke on a tom is not the tom itself.
        for mod in ("ghost", "flam", "rim"):
            if _has(MODIFIERS[mod], t):
                modifier = mod
                break

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
        modifier = _hat_modifier(t)

    elif family == "ride":
        if _has(MODIFIERS["bell"], t):
            modifier = "bell"
        elif _has(MODIFIERS["crash"], t):
            modifier = "crash"
        else:
            modifier = "tip"

    return (family, modifier, tom_index)


def _bare_hat_stroke(name):
    """Hat modifier for a name made only of hi-hat stroke words, else None."""
    t = _norm(name)
    words = re.findall(r"[a-z]+", t)
    if not words or not set(words) <= _BARE_HAT_WORDS:
        return None
    return _hat_modifier(t)  # None unless "closed" or "open" is present


def _stroke_class(modifier):
    return "closed" if modifier in ("closed", "closed_edge") else modifier


# ---------------------------------------------------------------------------
# Picking among candidates
# ---------------------------------------------------------------------------

def _plain_key(d):
    # Fewest words first: the plainest name is the piece's main stroke
    # ("Stack" over "Mini Stack", "Snare Flam" over "Snare Rimshot Flam").
    # Pitch breaks ties.
    return (len(re.findall(r"[a-z]+", _norm(d["name"]))), d["pitch"])


def _best(cands):
    return min(cands, key=_plain_key)


def _words(name):
    return set(re.findall(r"[a-z]+|\d+", _norm(name)))


def _closest(anchor, cands):
    """The candidate whose name shares the most words with the anchor's."""
    ref = _words(anchor["name"])
    return min(cands, key=lambda d: (-len(ref & _words(d["name"])),) + _plain_key(d))


def _side(name):
    t = _norm(name)
    if _has(r"\b(?:left|l)\d*\b", t):
        return "L"
    if _has(r"\b(?:right|r)\d*\b", t):
        return "R"
    return None


def _pick_sides(hits):
    """Split one cymbal family's hits into ``(right, left, rest)``.

    A side in the name ("Left Crash", "China R") wins. Unsided cymbals fill the
    open sides by pitch, lowest to the right and highest to the left. A kit
    whose only cymbal of the family is a left one plays it on both sides.
    ``rest`` is everything else, sorted by pitch.
    """
    rights = [d for d in hits if _side(d["name"]) == "R"]
    lefts = [d for d in hits if _side(d["name"]) == "L"]
    unsided = sorted((d for d in hits if _side(d["name"]) is None),
                     key=lambda d: d["pitch"])
    right = _best(rights) if rights else (unsided.pop(0) if unsided else None)
    left = _best(lefts) if lefts else (unsided.pop() if unsided else None)
    right = right or left
    rest = sorted((d for d in hits if d is not right and d is not left),
                  key=lambda d: d["pitch"])
    return right, left, rest


# ---------------------------------------------------------------------------
# Role assembly
# ---------------------------------------------------------------------------

# Roles missing from a library fall back to a sibling role via the shared
# ROLE_FALLBACKS table in catalog.py (order = try in sequence).

# Cymbal family -> (right role, left role, choke role).
_SIDED_CYMBALS = (
    ("crash", "CRASH_R", "CRASH_L", "CRASH_CHOKE"),
    ("china", "CHINA_R", "CHINA_L", "CHINA_CHOKE"),
    ("splash", "SPLASH_R", "SPLASH_L", "SPLASH_CHOKE"),
)


def match_roles(notes):
    """Classify a note-name dump into a complete groovekit role map.

    Args:
        notes: ``{pitch(int): name(str)}`` — e.g. from ``discover_drum_map``.

    Returns:
        (map_dict, report) where map_dict is ``{role: pitch}`` covering every
        ROLE_KEY that could be filled (directly or by fallback), plus the
        optional choke roles the kit has keys for. report is a dict with
        ``matched`` (role->pitch, direct), ``fallback`` (role->pitch, filled),
        ``unmatched`` (role list still missing), ``ignored`` (pitch->name that
        didn't classify), ``unused`` (pitch->name that classified but no role
        took), and ``complete`` (bool: every primary piece found).
    """
    if not isinstance(notes, dict) or not notes:
        return {}, {"matched": {}, "fallback": {}, "unmatched": list(ROLE_KEYS),
                    "ignored": {}, "unused": {}, "complete": False,
                    "reason": "NO_NOTES"}

    # Bucket classified notes by family. Choke keys are kept apart so they can
    # never become a cymbal's hit.
    buckets = {
        "kick": [], "snare": [], "tom": [], "hat": [],
        "ride": [], "crash": [], "china": [], "splash": [],
        "stack": [], "bell": [],
    }
    chokes = {fam: [] for fam in buckets}
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
            if isinstance(name, str) and name.strip():
                ignored[p] = name
            continue
        family, modifier, tom_index = c
        entry = {"pitch": p, "name": name, "mod": modifier, "tom": tom_index}
        (chokes if modifier == "choke" else buckets)[family].append(entry)

    # Bare hi-hat stroke names ("Small Open") join the hats only when the kit
    # names a hi-hat elsewhere ("Pedal Hat") and names none of that stroke
    # class (closed or open) outright, so there is nothing else they can be.
    if buckets["hat"]:
        named = {_stroke_class(d["mod"]) for d in buckets["hat"]}
        for p, name in sorted(ignored.items()):
            mod = _bare_hat_stroke(name)
            if mod and _stroke_class(mod) not in named:
                buckets["hat"].append({"pitch": p, "name": name, "mod": mod,
                                       "tom": None})
                del ignored[p]

    direct = {}

    # --- kick -------------------------------------------------------------
    if buckets["kick"]:
        ks = sorted(buckets["kick"], key=lambda d: d["pitch"])
        direct["KICK_R"] = ks[0]["pitch"]
        direct["KICK_L"] = ks[-1]["pitch"] if len(ks) > 1 else ks[0]["pitch"]

    # --- snare (+ghost/flam/rim) -----------------------------------------
    snares = buckets["snare"]
    if snares:
        mains = [d for d in snares if d["mod"] is None]
        direct["SNARE"] = _best(mains or snares)["pitch"]
        for mod, role in (("ghost", "SNARE_GHOST"), ("flam", "SNARE_FLAM"),
                          ("rim", "SNARE_RIM")):
            picks = [d for d in snares if d["mod"] == mod]
            if picks:
                direct[role] = _best(picks)["pitch"]

    # --- toms -------------------------------------------------------------
    # Assign by explicit number or position word first, then rank the rest by
    # pitch (highest = TOM_1, descending) to fill gaps. A slot that several
    # toms claim settles nothing (RS Monarch's "Floor L", "Floor R1" and
    # "Floor R2" all read as floor), so those toms are ranked by pitch too.
    toms = ([d for d in buckets["tom"] if d["mod"] is None]
            or buckets["tom"])
    claims = {}
    for d in toms:
        if d["tom"] is not None:
            claims.setdefault(d["tom"], []).append(d)
    by_num = {n: ds[0]["pitch"] for n, ds in claims.items() if len(ds) == 1}
    unnumbered = [d for d in toms if d["tom"] not in by_num]
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
    hats = buckets["hat"]
    tips = [d for d in hats if d["mod"] == "closed"]
    edges = [d for d in hats if d["mod"] == "closed_edge"]
    if tips or edges:
        tip = _best(tips or edges)
        direct["HH_CLOSED_TIP"] = tip["pitch"]
        others = [d for d in edges if d is not tip]
        if others:
            # The edge stroke of the same closed hat: "Tight Closed Shoulder"
            # goes with "Tight Closed Tip", not "Normal Closed Shoulder".
            direct["HH_CLOSED_EDGE"] = _closest(tip, others)["pitch"]
    opens = sorted((d for d in hats if d["mod"] == "open"),
                   key=lambda d: d["pitch"])
    for i, d in enumerate(opens[:3]):
        direct["HH_OPEN_%d" % (i + 1)] = d["pitch"]
    pedals = [d for d in hats if d["mod"] == "pedal"]
    if pedals:
        direct["HH_PEDAL"] = _best(pedals)["pitch"]

    # --- ride -------------------------------------------------------------
    rides = buckets["ride"]
    if rides:
        ride_tips = [d for d in rides if d["mod"] == "tip"]
        bells = [d for d in rides if d["mod"] == "bell"]
        crashes = [d for d in rides if d["mod"] == "crash"]
        direct["RIDE_TIP"] = _best(ride_tips or rides)["pitch"]
        bell = None
        if bells:
            # The bell's tip stroke is the ping; its shoulder stroke is the
            # bigger accent ("Ride Bell Tip" vs "Ride Bell Shoulder").
            bell = min(bells, key=lambda d: (
                _has(r"\bshoulder\b", _norm(d["name"])),) + _plain_key(d))
            direct["RIDE_BELL"] = bell["pitch"]
        # A ride without an edge crash uses the bell's shoulder stroke as its
        # crash accent, as the hand-built RS Monarch map does.
        crashes = crashes or [d for d in bells if d is not bell
                              and _has(r"\bshoulder\b", _norm(d["name"]))]
        if crashes:
            direct["RIDE_CRASH"] = _best(crashes)["pitch"]

    # --- crash / china / splash, their chokes, stack, bell ---------------
    for fam, right_role, left_role, choke_role in _SIDED_CYMBALS:
        right, left, rest = _pick_sides(buckets[fam])
        if right:
            direct[right_role] = right["pitch"]
        if left:
            direct[left_role] = left["pitch"]
        if fam == "crash" and rest:
            direct["BIG_CRASH"] = rest[-1]["pitch"]
        if chokes[fam]:
            # The choke lane falls back to the right-hand cymbal, so it grabs
            # that cymbal: "Right Crash Choke" goes with "Right Crash".
            grab = (_closest(right, chokes[fam]) if right
                    else _best(chokes[fam]))
            direct[choke_role] = grab["pitch"]
    if buckets["stack"]:
        direct["STACK"] = _best(buckets["stack"])["pitch"]
    if buckets["bell"]:
        direct["BELL"] = _best(buckets["bell"])["pitch"]

    # --- fallbacks --------------------------------------------------------
    fallback = {}
    for role in ROLE_KEYS:
        if role in direct:
            continue
        for fb in ROLE_FALLBACKS.get(role, []):
            if fb in direct:
                fallback[role] = direct[fb]
                break

    full = dict(direct)
    full.update(fallback)
    unmatched = [r for r in ROLE_KEYS if r not in full]
    used = set(full.values())
    unused = {d["pitch"]: d["name"]
              for group in (buckets, chokes) for ds in group.values()
              for d in ds if d["pitch"] not in used}

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
        "unused": dict(sorted(unused.items())),
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
    if report.get("unused"):
        lines.append("")
        lines.append("Classified but not mapped:")
        for pitch, name in report["unused"].items():
            lines.append("  %3d  %s" % (pitch, name))
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
