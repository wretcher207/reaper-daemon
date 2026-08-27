"""guitargen — humanized virtual-guitar/bass MIDI for the Reaper Daemon.

The guitar-side counterpart to drumgen: tuning + keyswitch maps, a compact riff
notation, a performance engine that voices palm-mute chugs with the mod wheel and
gives double-tracked takes their width via independent seeds, and a CC-aware SMF
writer. Rendered files go onto REAPER tracks through the bridge's
`insert_midi_file`, the same path the drum groove uses.
"""
from .maps import get_map, GUITAR_MAPS, BASS_MAPS, SHREDDAGE3_KS, MODWHEEL
from .perform import perform
from .smf import write_smf, parse_smf
from . import riffs

__all__ = ["get_map", "GUITAR_MAPS", "BASS_MAPS", "SHREDDAGE3_KS", "MODWHEEL",
           "perform", "write_smf", "parse_smf", "riffs"]
