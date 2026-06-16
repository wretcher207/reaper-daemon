# drum-apparatus

A stdlib-only Python generator that ports the Dead Pixel Drum Apparatus groove
vocabulary (36 grooves across 10 metal subgenres, 4 drum-VST MIDI maps,
velocity/timing humanization, power-hand layering, auto tom fills) into an
agent-callable form. `drumgen/` loads a JSON catalog and renders a single groove
or a multi-section arrangement into a channel-1 Standard MIDI File; `generate.py`
is the CLI. The MIDI is inserted into a live REAPER session via the
`reaper-agent-bridge` `insert_midi_file` command and verified by ear (see
`SKILL.md`). `catalog/` is data; `tests/` pin the catalog, SMF I/O, feel math,
render, and CLI.

Run the tests: `python -m pytest -v` (pytest is the only dev dependency; the
generator itself uses the standard library only).
