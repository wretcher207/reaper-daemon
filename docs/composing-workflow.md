# Composing workflow

How an agent writes a full song into a live REAPER session with sampled virtual
instruments, and the lessons behind each rule. Helpers live in
`skills/composing-workflow/composer/` (see its `SKILL.md`). Distilled from a
djent/thall session (Shreddage 3.5 Argent L/R, Submission Audio Umansky Bass,
Odeholm drums, Mysteria Voices) in which the first draft sounded stabby, the bass
was out of tune with the guitars, and the cymbals never moved.

Sources: **[A]** Shreddage 3.5 Argent manual (PDF in the library's Documentation
folder, page numbers given), **[P]** Argent product page
<https://impactsoundworks.com/product/shreddage-3-argent/>, **[U]** Umansky Bass
user manual (PDF in its Documentation folder), **[L]** live plugin parameters read
through the daemon, **[M]** measured by probe render + FFT. **UNVERIFIED** = best
guess, confirm by ear.

## 1. Look and measure before writing

- Scan tracks and FX: `get_context`, `get_fx_parameters`, `discover_drum_map`,
  `get_track_routing`, the instrument catalog and kit maps. Read manuals on disk.
  Never guess a mapping; if one is missing, stop for that instrument and report.
- **Measure the sounding pitch; don't trust labels.** `composer.measure.probe`:
  1. puts a temporary item after the song end,
  2. solos the track, plus any track its audio routes through (an instrument
     feeding a bus is silent alone; check `get_track_routing`),
  3. captures each note with `capture_track_audio`, and a no-note baseline,
  4. always unsolos and deletes the probe item.
  Then `measure.analyze(file)` (FFT) gives the peaks. Watch for other soloed
  tracks leaking in.
- Example results from one rig **[M]** (specific to those patches; re-measure):
  - Argent sounded **3 semitones below** the MIDI note (28 -> C#1) with Transpose at 0.
  - Umansky labels were real pitch (MIDI 40 = 41.2 Hz E1; 35 = B0, the default floor).
  - A Mysteria Atmosphere snapshot **ignored note pitch** (always a C#/G# chord);
    only Cluster layers follow the note.
  So that song sounded in C# although the riff letters were written in E.
- Check bass vs guitar pitch classes beat by beat after applying measured offsets
  (`artic.pitch_classes(ev, offset)`).

## 2. Plan first

**Song history (mandatory).** Before planning run `composer.history.recent()`: the
last ~10 songs, one line each (date, title, key, bpm, time_sig, tuning, structure,
riff, tricks, drums). Never read the whole `songs/history.jsonl`. Pick a clearly
different key, tempo, feel and riff concept unless the user asks for similarity.
After the song is verified (section 7), append one entry with
`history.append_song(title=..., key=..., bpm=..., ...)`; fields are clipped so a
line stays under 600 chars. `songs/` is gitignored: history is the user's data.

Tempo, sounding key, tuning, time signature, a song map with bar counts, one
line per instrument per section. Then build.

## 3. Guitars: Shreddage 3.5 Argent

**Instrument.** 9-string, C# F# B E A D G B e at concert pitch [P]; lowest note
MIDI 25. Every string sampled open to top fret [A p4]. You send pitches; the
engine picks string/fret from hand position, legato, force-string and fretting
mode [A p4].

**Keyswitches (TACT, latching by default** [A p19, p24]; may share the note's
tick [A p12]; Kontakt C-2 = 0) [A p31]:

| MIDI | Articulation |
|---|---|
| 0 | Sustain (KS vel 1-116) / Rake (117-126) / Pinch harmonic (127) |
| 1 | Mute: **note velocity = mute depth** |
| 2 | Staccato |
| 3 / 4 / 5 | Power chord sustain / mute / staccato (one note -> root+5th+8ve) |
| 6 | Tremolo (loops while held) |
| 7 | Choke |
| 8 | Tapping |
| 9 / 10 | Natural / artificial harmonic |
| 11 | FX |

Utility [A p33]: 21 slide to next note, 22 fret noise, 23 slide in/out from neck
end, **24 Thrash = repeat last note (never root a riff on 24)**. Behaviour:
104-112 force string (104 = low C#), 113 off; 102 force hand position (by KS
velocity), 101 auto; 114 legato on (default), 115 off. The manual also lists F7
as "Fretting Mode: Polyphonic" [A p16], conflicting with 101: UNVERIFIED.

**Palm-mute depth is velocity** [A p31], up to 5 layers [P]. Low = tight and
scratchy, high = half-muted and beefy. On the measured patch "Mute Vel -> Vol" was
0% [L], so accent mutes with power-chord mute or an open hit, not louder velocity.
Layer split points are unpublished; the helper uses 5 even bands (UNVERIFIED).

**Note length is the sound.** Release after note-off is only ~17-57 ms (sustain
36 ms, mute 56.6 ms) [L]; release samples trigger at note-off [A p32]. A 100 ms
palm mute is a pick tick and a squeak: **that was the stabby sound.** Rules:
muted notes hold to 12 ms before the next pick; open / let-ring notes ring to the
next onset; staccato ~55% of its slot; only legato and slides overlap.

**Legato and slides.** Overlapping notes within 12 st trigger legato [A p32];
hammer/pull needs same string and <= 2 st (Hammer-on Range) [A p22], larger
moves slide; power chords slide too. So accidental overlap = unwanted slurs, and
same-pitch overlap gets truncated to 0 length by REAPER. The helper clamps every
non-legato note to end 3 ms before the next onset.

**Other.** Sustains: 3 dynamic layers, 4 down + 4 up round robins [P]. Per-
articulation Vel->Vol (Sustain 41%, PC Mute 13% on that patch) [L]. Vibrato on
CC1 [A p10]; any knob MIDI-learnable [A p7]; no default CC for mute depth or pick
position. Same pitch on a lower string is thicker [A p4]; steer with Hand
Position / Force String. Check Multitracking: "Dbl 1+2" on both L and R makes the
pair quad-tracked; consider Guitar 1 on L, Guitar 2 on R [A p11] (report, don't
change without approval).

**Writing rules.** Tag every note. Accent with open hits and power chords, not
just velocity. Keep any one articulation <= ~60% of a section; alternate muted
and open phrases. L/R double = two renders with different seeds and timing
leans, never a copy (`verify.check_lr_differ`).

## 4. Bass: Submission Audio Umansky

Fingerstyle; default tuning B0 E1 A1 D2 G2 (set on the Settings page, not a
parameter) [U p8] [L]; playable C0-C5 (24-84) [U p10]; real floor in default
tuning is 35 (lower notes need retuning, UNVERIFIED otherwise).

Keyswitches [U p8]: 0 finger alternate (default), 1/2 index/middle, 3 ghost,
4/5 slap/ghost slap, 6/7 pop/ghost pop, 8 harmonic, 9 hammer/pull, 10 tapping,
11 slide, 12 dead double thumb, 13 thump, 14/15 double thumb / pop, 16 fret-hand
mute. Key 23 repeats the last note. Force string 122 C, 123 G, 124 D, 125 A,
126 E, 127 B [U p9-10]. **Latch vs momentary** is a per-articulation setting with
no stated default [U p8-9], so the helper re-sends the keyswitch on every
articulation change, including back to 0. Slides: program with Force String;
can't slide to an open note [U p9]. Humanize knob (12 o'clock recommended
[U p5, p11]); DOOM mode locks velocity [U p5]; pitch bend up to +-12 st.

**Writing rules.** Share the guitar's **sounding** roots (same octave or one
below, within the bass floor). On the measured rig, bass MIDI 37 = C#1 = Argent
MIDI 28. Lock to the kick and guitar accents; drop out or hold long notes now
and then so it is not just doubling.

## 5. Drums

- Keep swing, ghost snares, real fills into each section, stops with crash
  chokes, and tight straight blasts (`realize(..., straight=True)`).
- **Vary cymbals by default** every 4-8 bars (`cymbals.rotate`): closed / half-
  open / open hats, hat 16ths for builds, ride tip+bell with pedal hat, ride
  crash, china+stack, crash quarters, bell quarters.
- Crash / china / big crash on phrase downbeats (`downbeats`); splash or crash on
  the guitar's open-chord accents (`accents_from_tokens(tokens, roles, {"pc"})`);
  china on chord changes in choruses; occasional crash + choke (`choke`).
- Cymbals loud: crashes and chinas ~108-127.
- Humanize, then the golden rule: `cymbals.golden_rule(all_notes)` (wraps
  `skills/drum-apparatus/drumgen/goldenrule.py`) must return 0.
- Map roles to pitches from `discover_drum_map`, never from memory.

## 6. Atmosphere (pads, voices)

Sparing: intro, behind choruses, under the breakdown, the ending. Swells via FX
parameter automation (`write_fx_param_automation`). Measure whether the loaded
snapshot follows pitch before writing a melody.

## 7. Build and verify

- One section at a time, as undoable steps. Replace only items you created;
  clear from a few ms after the bar line so neighbours survive.
- `verify.replace_section` reads every item back with `get_midi_notes` and
  checks: note count, played-pitch range, keyswitches present, notes inside the
  section's bars, item count unchanged, no zero-length notes, lengths match the
  plan within 3 ms, golden rule for drums. Then `check_lr_differ` for doubles.
- `delete_items_in_range` takes `{"type": "time", "seconds": ...}`.
- **Don't save the project, and don't change plugin settings** without approval;
  report observations (multitracking, humanize knobs) instead.
- Append one song-history entry (section 2).
- Report the final song map with bar numbers and everything not done.
