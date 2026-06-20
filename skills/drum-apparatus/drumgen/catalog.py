import json
from pathlib import Path

DRUMGEN_DIR = Path(__file__).resolve().parent.parent
_CATALOG = DRUMGEN_DIR / "catalog"
# User overlay: discovered / hand-added maps live here (gitignored), one JSON
# file per map. They merge ON TOP of the built-in catalog/maps.json, so a user
# can add a kit without touching a tracked file (and a `git pull` never
# clobbers it). Keys (map names) in the overlay win on collision.
_OVERLAY = DRUMGEN_DIR / "maps"

ROLE_KEYS = [
    "KICK_R", "KICK_L", "SNARE", "SNARE_FLAM", "SNARE_RIM", "SNARE_GHOST",
    "TOM_1", "TOM_2", "TOM_3", "TOM_4",
    "HH_CLOSED_TIP", "HH_CLOSED_EDGE", "HH_OPEN_1", "HH_OPEN_2", "HH_OPEN_3", "HH_PEDAL",
    "RIDE_TIP", "RIDE_BELL", "RIDE_CRASH",
    "CRASH_L", "CRASH_R", "BIG_CRASH",
    "CHINA_L", "CHINA_R", "STACK",
    "SPLASH_L", "SPLASH_R", "BELL",
]


def load_maps():
    """Return all available drum-kit maps: built-in catalog + user overlay.

    The overlay (skills/drum-apparatus/maps/*.json) wins on name collision,
    so discovered/added kits override built-ins of the same name. Built-in
    maps.json is the source of truth for the shipped kits and is never
    written to by tooling.
    """
    maps = {}
    built_in = _CATALOG / "maps.json"
    if built_in.is_file():
        data = json.loads(built_in.read_text())
        if isinstance(data, dict):
            maps.update(data)
    if _OVERLAY.is_dir():
        for p in sorted(_OVERLAY.glob("*.json")):
            try:
                d = json.loads(p.read_text())
            except Exception:
                continue
            if isinstance(d, dict):
                # A single map file may be either {name: {role:pitch}} or a
                # bare {role:pitch} (named after the file stem).
                if len(d) == 1 and isinstance(next(iter(d.values())), dict):
                    maps.update(d)
                else:
                    maps[p.stem] = d
    return maps


def load_grooves():
    return json.loads((_CATALOG / "grooves.json").read_text())


def find_groove(name):
    for g in load_grooves():
        if g["name"].lower() == name.lower():
            return g
    raise KeyError(f"NO_GROOVE: {name}")
