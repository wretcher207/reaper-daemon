# Instrument reference — this rig's guitars & bass

Ground-truth specs behind the `guitargen` maps, confirmed against each product's
own manual/site and David's live instrument UI (2026-08-27). MIDI numbers are
absolute; note names in parentheses use Kontakt's C3=60 convention (so C-2 = MIDI
0), which is what the Argent UI shows.

## Shreddage 3.5 Argent (tracks `argent-l`, `argent-r`)

- **Instrument:** 9-string, sampled from a Schecter Damien Platinum (a Mick Gordon
  DOOM guitar). Lowest string tuned to **C#**. Plays at concert pitch; an
  algorithm picks string/fret per incoming MIDI note.
- **Lowest playable note:** MIDI **25** (low C# string open). **MIDI 24 (C0) is the
  "Thrash note" keyswitch** (retriggers the last note) — do NOT root a riff there.
- **Palm mute:** the **Mute** keyswitch (C#-2 = **1**). The mute *degree*
  (very-muted → half-muted) is set by the **played note's velocity**, not the mod
  wheel. So harder hits ring a little more — which is musically what you want.
- **Power chords:** **Power Chord Mute** (E-2 = **4**) and **Power Chord Sustain**
  (D#-2 = **3**) auto-voice root+5th+octave from a single root note.
- **Articulation keyswitches** (MIDI note): Sustain 0 *(vel 1-116)*, Rake 0
  *(vel 117-126)*, Pinch Harmonic 0 *(vel 127)*, Mute 1, Staccato 2,
  Pwr Sustain 3, Pwr Mute 4, Pwr Staccato 5, Tremolo 6, Choke 7, Tapping 8,
  Natural Harmonic 9, Artificial Harmonic 10, FX 11. (Argent's TACT tab lets the
  user remap these; the above is David's/default layout.)
- **Behavioral keyswitches** live high (Force String G#7–E8, Force Hand F#7, etc.)
  and FX/utility low (Slide A-1, Fret noise A#-1, Slide-note B-1, Thrash C0).
- **Sources:** [manual PDF](https://impactsoundworks.com/manuals/Shreddage%203.5%20Argent%20Manual.pdf),
  [product page](https://impactsoundworks.com/product/shreddage-3-argent/).

## GGD The Nolly Bass Library (track `nolly-bass-library`)

- **Instrument:** Adam "Nolly" Getgood's **Dingwall NG2 6-string** — the bass tone
  on *Periphery V: Djent Is Not A Genre*. Kontakt Player library.
- **Range:** MIDI **28–79** (E0–G4). **Floor is E0 = 28** — it will not sound below
  that (why a first attempt at A0 = 21 was silent).
- **Articulations:** pick-based with an **intelligent alternate-picking** system;
  no slaps, taps, bends, pops, or slides. Has three tone options, pitch-shifting,
  MIDI transposition, and variable string positions. Driven here as plain notes.
- **Sources:** [product page](https://ggd.co/products/the-nolly-bass-library),
  [Sweetwater](https://www.sweetwater.com/store/detail/GGDNollyBass--getgood-drums-the-nolly-bass-library).

## Why the riff is in E

Neither instrument is natively "Drop A." The Argent's lowest string is C# (25) and
the Nolly floors at E (28). Rooting the riff at **E (MIDI 28)** puts guitar and
bass on the same low note so they lock, uses the low strings, and is the heaviest
choice both can reach. That is the `low_string` in maps `argent_e` / `nolly_e`;
change it to move the key.
