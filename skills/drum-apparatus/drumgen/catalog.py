import json
from pathlib import Path

DRUMGEN_DIR = Path(__file__).resolve().parent.parent
_CATALOG = DRUMGEN_DIR / "catalog"

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
    return json.loads((_CATALOG / "maps.json").read_text())


def load_grooves():
    return json.loads((_CATALOG / "grooves.json").read_text())


def find_groove(name):
    for g in load_grooves():
        if g["name"].lower() == name.lower():
            return g
    raise KeyError(f"NO_GROOVE: {name}")
