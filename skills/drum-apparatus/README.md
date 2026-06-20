# drum-apparatus

A stdlib-only Python drum-MIDI engine encoding David's heavy-metal programming
SOP (kicks-from-the-riff first, then humanized velocity/timing/fatigue baked in
at placement time). `drumgen/groovekit.py` renders a hand-authored step-grid DSL
into a channel-1 Standard MIDI File; `drumgen/riff.py` reads a guitar stem's
transients into a kick grid; `catalog/` ships the drum-kit maps plus a reference
groove vocabulary (`grooves.json`). Drive it through the agent CLI — `python3
reaperd.py groove <dsl> --track <name>` renders, inserts into the live REAPER
session, and verifies — then confirm by ear (see `SKILL.md`). `tests/` pin the
catalog, SMF I/O, and the groove engine.

Run the tests: `python -m pytest -v` (pytest is the only dev dependency; the
engine itself uses the standard library only).
