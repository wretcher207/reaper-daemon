# Odeholm Audio Thall Parameter Map

`VST3: thall amp (Odeholm Audio)`, 33 parameters.

Captured live 2026-08-14 from `continental.rpp`, tracks `rhythm-1` / `rhythm-2`
(chain: DIFIX → thall amp → mixIR3). Indices are from `get_fx_parameters` with
`fx_scope: "track"`.

The "example" column is the value that track was sitting at when the scan ran.
It is a real preset for a modern high-gain rhythm tone, not a plugin default.

| idx | Name | Type | Example norm | Example shown |
|----:|------|------|-------------:|---------------|
| 0 | Power | toggle | 1.0 | On |
| 1 | Host Bypass | toggle | 0.0 | Off |
| 2 | Input Gain | dB | 0.5 | 0.0dB |
| 3 | Output Gain | dB | 0.5 | 0.0dB |
| 4 | Lo-Fi | toggle | 0.0 | Off |
| 5 | Tone Matching Power | toggle | 1.0 | On |
| 6 | Tone Matching Amount | % | 0.30 | 30% |
| 7 | Tone Matching Smooth | % | 0.80 | 80% |
| 8 | Shape Power | toggle | 1.0 | On |
| 9 | Tighten Gate | dB | 0.29215711 | -50dB |
| 10 | Tighten Chug | % | 0.42 | 42% |
| 11 | Tighten Frequency | Hz (log) | 0.52310783 | 249Hz |
| 12 | Pitch Power | toggle | 1.0 | On |
| 13 | Pitch Whammy | semitones | 0.45833334 | -2.0st |
| 14 | Pitch Thicken | % | 0.21 | 21% |
| 15 | Pitch Hi-Cut | Hz (log) | 0.56142741 | 966Hz |
| 16 | Pitch Cleanse | toggle | 0.0 | Off |
| 17 | Pitch Latency | toggle | 0.0 | Off |
| 18 | Thicken Amp Parallel | toggle | 1.0 | On |
| 19 | Low Dirt | % | 0.0 | 0% |
| 20 | Amplifier Power | toggle | 1.0 | On |
| 21 | Amp Drive | % | 0.55 | 55% |
| 22 | Amp Lo | dB | 0.52083331 | +0.5dB |
| 23 | Amp Mid | dB | 0.5 | 0.0dB |
| 24 | Amp Hi | dB | 0.57916671 | +1.9dB |
| 25 | Amp Presence | dB | 0.45833334 | -1.0dB |
| 26 | Cab Power | toggle | 0.0 | Off |
| 27 | Lo-Cut | Hz | 0.11034482 | 42Hz |
| 28 | Hi-Cut | Hz | 0.94022995 | 13.2kHz |
| 29 | Mono/Stereo Toggle | toggle | 1.0 | On |
| 30 | Bypass | enum | 0.0 | normal |
| 31 | Wet | 0-100 | 1.0 | 100 |
| 32 | Delta | enum | 0.0 | normal |

## Verified range formulas

Needed for `write_fx_param_automation`, which takes normalized values only and
cannot binary-search a formatted target the way `set_fx_param` can.

**Percent params** (6, 7, 10, 14, 19, 21) are linear: `norm = pct / 100`.
Confirmed on four of them against their own displays.

**13 Pitch Whammy**, linear over ±24 semitones:

```
norm = (semitones + 24) / 48        semitones = norm * 48 - 24
```
0.5 is unison. Useful stops: -12 = 0.25, -5 = 0.395833, 0 = 0.5,
+7 = 0.645833, +12 = 0.75, +19 = 0.895833, +24 = 1.0.

**15 Pitch Hi-Cut**, log over 20Hz to 20kHz:

```
norm = log10(Hz / 20) / 3           Hz = 20 * 1000^norm
```
Probed: 4kHz → 0.765189, 8kHz → 0.866446. Formula predicts 0.7670 and 0.8672,
so it is right to about 0.002 normalized.

**11 Tighten Frequency**, log over 20Hz to roughly 2.5kHz:

```
norm = 0.4767 * log10(Hz / 20)      Hz = 20 * 10^(norm / 0.4767)
```
Probed: 400Hz → 0.620451, 800Hz → 0.764010, 1500Hz → 0.887181. The constant
0.4767 was fitted to those three and reproduces the 249Hz example to 0.001.
The top of the range was never probed directly; the formula puts norm 1.0 at
about 2.5kHz.

Params 2, 3, 22 to 25, 27 and 28 were not probed. Use `set_fx_param` with
`formatted_value` for one-shot moves on those, or probe them the same way
before automating.

## How to re-probe

`set_fx_param` binary-searches a formatted target, so the cheap method is to
set a known display value and read the normalized result back:

```
python reaperd.py cmd set_fx_param '{"target_track_name":"rhythm-1",
  "fx_name_contains":"thall","fx_scope":"track","param_index":11,
  "formatted_value":"400 Hz"}'
python reaperd.py cmd get_fx_parameters '{"target_track_name":"rhythm-1",
  "fx_name_contains":"thall","fx_scope":"track","limit":300}'
```

Three points across the range are enough to fit a log curve. Restore the
original normalized value afterwards; the probe writes for real.

## Notes for automating this plugin

- **Pitch Hi-Cut gates the whole pitch section.** At the example value of 966Hz
  the shifted voice is inaudible under anything played above the 12th fret. Open
  it to 4kHz or higher in any section where the Whammy is meant to be heard, and
  put it back afterwards.
- **Pitch Thicken is the wet mix for the shifted voice.** Whammy automation with
  Thicken left at 21% reads as a detune shimmer, not a pitch move.
- **Square shape for intervals, linear for dives.** Linear on Whammy is a
  continuous glide; stepped intervals need `"shape": "square"` or every jump
  turns into a portamento smear.
- `write_fx_param_automation` creates the envelope armed and visible
  (`B_ARM 1`). Playback in an automation write mode will overwrite it.
