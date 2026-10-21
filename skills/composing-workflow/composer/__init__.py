"""Composing helpers for Reaper Daemon agents.

artic    - per-note articulation tokens -> keyswitches, lengths, velocities
           (Shreddage 3.5 Argent, Submission Audio Umansky Bass)
cymbals  - cymbal-layer builder: phrase rotation, accent alignment, chokes,
           golden-rule velocities
measure  - probe-and-FFT sounding-pitch measurement through the daemon
verify   - section-by-section replace + read-back verification

See ../SKILL.md and docs/composing-workflow.md.
"""
