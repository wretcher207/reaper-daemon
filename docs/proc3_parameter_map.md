# FabFilter Pro-C 3 Parameter Map

`VST3: Pro-C 3 (FabFilter)`, **240 parameters**.

Captured live 2026-08-18 on REAPER 7.79 from a scratch track in
`used-claude-mix.rpp` (a fresh instance at plugin defaults, deleted afterwards).
Indices are from `get_fx_parameters` with `fx_scope: "track"`.

Every value below was measured by writing it and reading the display back, not
inferred from the manual.

## Layout

| Range | Contents |
|---|---|
| 0 – 31 | Compressor and stereo-link globals |
| 32 – 85 | **Side chain EQ, 6 bands, stride 9.** Band *n* starts at `32 + (n-1) * 9`. |
| 86 – 100 | Audition, mix, I/O, oversampling, meters |
| 101 – 239 | REAPER's own wrapper params (Internal, MIDI CC). Ignore. |

### Globals

| idx | Name | Type | Default norm | Default shown |
|----:|------|------|-------------:|---------------|
| 0 | Style | enum, 14 | 0.0 | Clean |
| 1 | Threshold | dB, linear | 0.73333335 | -16.00 dB |
| 2 | Auto Threshold | toggle | 0.0 | Off |
| 3 | Lock Auto Threshold | toggle | 0.0 | Off |
| 4 | Ratio | table | 0.56 | 3.50:1 |
| 5 | Knee | dB, linear | 0.10202699 | +7.35 dB |
| 6 | Range | dB, linear | 1.0 | +60.00 dB |
| 7 | Attack | ms, cubic | 0.14227778 | 0.725 ms |
| 8 | Release | ms, table | 0.27794313 | 100.0 ms |
| 9 | Auto Release | toggle | 0.0 | Off |
| 10 | Lookahead | ms, gated by 95 | 0.0 | 0.000 ms |
| 11 | Hold | ms, table | 0.0 | 0.000 ms |
| 12 | Character | enum, 4 | 0.0 | Off |
| 13 | Character Routing | enum, 2 | 1.0 | Post Compression |
| 14 | Character Drive | dB, linear | 0.5 | 0.00 dB |
| 15 | Wet Gain | dB, linear | 0.5 | 0.00 dB |
| 16 | Wet Pan | M/S pair | 0.5 | Mid: 0 dB / Side: 0 dB |
| 17 | Dry Gain | dB, linear | 0.0 | -INF dB |
| 18 | Dry Pan | M/S pair | 0.5 | Mid: 0 dB / Side: 0 dB |
| 19 | Auto Gain | toggle | 1.0 | On |
| 20 | Show Side Chain | toggle | 0.0 | Off |
| 21 | Side Chain Input | enum, 4 | 0.0 | Internal |
| 22 | Side Chain Level | dB, linear | 0.5 | 0.00 dB |
| 23 | Host Trigger Sync | enum | 0.5 | 1/4 Note |
| 24 | Host Trigger Offset | % | 0.5 | 100% |
| 25 | Host Trigger Length | % | 0.1 | 10.0% |
| 26 | Stereo Link | %, compound | 0.40213889 | 80% |
| 27 | Stereo Link Mode | enum, 4 | 0.0 | Mid |
| 28 | Stereo Link Center | toggle | 0.0 | Excluded |
| 29 | Stereo Link Surrounds | toggle | 1.0 | Included |
| 30 | Stereo Link Tops | toggle | 1.0 | Included |
| 31 | Stereo Link LFE | toggle | 0.0 | Excluded |

### Output and machine settings

| idx | Name | Type | Default |
|----:|------|------|---------|
| 86 | Audition Side Chain | toggle | Off |
| 87 | Audition Triggering | toggle | Off |
| 88 | Mix | %, 0 to 200 | 100.0% (0.5) |
| 89 | Input Level | dB, linear | 0.00 dB (0.5) |
| 90 | Input Pan | L/R pair | 0.5 |
| 91 | Output Level | dB, linear | 0.00 dB (0.5) |
| 92 | Output Pan | L/R pair | 0.5 |
| 93 | Bypass | toggle | Not Bypassed |
| 94 | Oversampling | enum, 6 | Off |
| 95 | Maximum Lookahead | enum, 5 | Off |
| 96 | Midi State | enum | Enabled |
| 97 | Meter Scale | enum | 72 dB |
| 98 | Knee Display Enabled | toggle | Off |
| 99 | Show Input Level Meter | toggle | Disabled |
| 100 | Host Bypass | toggle | Not Bypassed |

### The side chain EQ band block, offsets from the band's base index

`Used`, `Enabled`, `Frequency`, `Gain`, `Q`, `Shape`, `Slope`,
`Stereo Placement`, `Speakers`. Band 1 Frequency is index 34, band 4 Frequency
is `32 + 3*9 + 2 = 61`.

The frequency, gain and Q curves are **identical to Pro-Q 4's band curves** and
were re-probed here to confirm it, but `Slope` is not: in Pro-C 3 it is a
10-value enum, not a continuous control.

## Curves

**Threshold (1)**, -60 to 0 dB, linear:

```
norm = (dB + 60) / 60                   dB = norm * 60 - 60
```
Probed: 0.0 → -60.00, 0.1 → -54.00, 0.25 → -45.00, 0.5 → -30.00, 0.75 → -15.00,
0.9 → -6.00, 1.0 → 0.00 dB. Exact.

**Knee (5)**: 0 to +72 dB, linear, `norm = dB / 72`. Probed 0.25 → +18.00,
0.5 → +36.00, 0.75 → +54.00.

**Range (6)**: 0 to +60 dB, linear, `norm = dB / 60`. Probed 0.25 → +15.00,
0.5 → +30.00, 0.75 → +45.00.

**Attack (7)**, 0.005 to 250 ms, cubic:

```
ms = 250 * norm^3                       norm = (ms / 250)^(1/3)
```
Probed against the formula: 1 ms → 0.15844965 (formula 0.158740),
10 ms → 0.3419348 (0.341995), 50 ms → 0.58476847 (0.584804). norm 0.0 floors at
0.005 ms rather than 0.

**Character Drive (14)**: ±24 dB, linear, `norm = (dB + 24) / 48`.

**Wet Gain (15), Dry Gain (17), Input Level (89), Output Level (91)**: -36 to
+36 dB, linear, with **norm 0.0 meaning -INF, not -36 dB**:

```
norm > 0:   norm = (dB + 36) / 72       dB = norm * 72 - 36
norm = 0:   -INF (silence)
```
Probed: 0.25 → -18.00, 0.5 → 0.00, 0.75 → +18.00, 1.0 → +36.00 dB.
Dry Gain defaults to 0.0, which is why the dry path is silent until you raise it.

**Side Chain Level (22)**: same ±36 dB linear span but no -INF floor. 0.0 is
-36.00 dB.

**Mix (88)**: 0 to 200%, linear, `norm = pct / 200`. norm 0.5 is 100%.

**Side chain EQ Frequency, Gain, Q** (band offsets 2, 3, 4): identical to
Pro-Q 4. Re-probed here and the normalized results matched Pro-Q's to the digit.

```
Freq:  norm = log10(Hz / 10) / 3.477121        (10 Hz to 30 kHz)
Gain:  norm = (dB + 30) / 60                   (±30 dB)
Q:     norm = log10(Q / 0.025) / 3.20412       (0.025 to 40)
```

## Tables (no clean formula, use the anchors)

**Ratio (4)**. `formatted_value` works here (`"4.00:1"`), which is the easier
route; these are the measured anchors:

| norm | Ratio | norm | Ratio |
|---:|---|---:|---|
| 0.0 | 1.00:1 | 0.56 | 3.50:1 (default) |
| 0.1 | 1.10:1 | 0.6 | 4.00:1 |
| 0.2 | 1.25:1 | 0.7 | 6.00:1 |
| 0.3 | 1.50:1 | 0.8 | 8.00:1 |
| 0.399 | 2.00:1 | 0.9 | 10.00:1 |
| 0.5 | 2.75:1 | 1.0 | 100.00:1 |

**Release (8)**, 10 ms to 2.5 sec. This is the parameter whose ms-to-sec display
switch broke `formatted_value` until the 2026-08-18 fix (see Gotchas); the table
is still the fastest way to pick a value, and the only way on an older bridge:

| norm | Release | norm | Release |
|---:|---|---:|---|
| 0.00 | 10.00 ms | 0.55 | 377.7 ms |
| 0.05 | 12.90 ms | 0.60 | 456.3 ms |
| 0.10 | 21.62 ms | 0.65 | 547.7 ms |
| 0.15 | 36.14 ms | 0.70 | 655.4 ms |
| 0.20 | 56.50 ms | 0.75 | 784.9 ms |
| 0.25 | 82.74 ms | 0.80 | 944.9 ms |
| 0.30 | 115.0 ms | 0.85 | 1.151 sec |
| 0.35 | 153.3 ms | 0.90 | 1.429 sec |
| 0.40 | 198.2 ms | 0.95 | 1.835 sec |
| 0.45 | 250.0 ms | 1.00 | 2.500 sec |
| 0.50 | 309.5 ms | | |

Interpolating between neighbouring anchors is accurate to a few percent, which
is finer than the control reads. The default 0.27794313 is 100.0 ms.

**Hold (11)**, 0 to 500 ms: 0.0 → 0.000, 0.25 → 7.500 ms, 0.5 → 40.00 ms,
0.75 → 170.0 ms, 1.0 → 500.0 ms.

**Lookahead (10)** is a fraction of **Maximum Lookahead (95)**, and reads
0.000 ms at every value while 95 is `Off`. With 95 at 1.0 (Maximum 20 ms):
0.25 → 5.000 ms, 0.5 → 10.00 ms, 0.75 → 15.00 ms, 1.0 → 20.00 ms. Set 95 first
or the lookahead write silently does nothing.

**Stereo Link (26)** is compound. Below 0.5 it is a plain percentage
(0.0 → 0%, 0.25 → 50%, 0.5 → 100%); above 0.5 the display adds a second term,
0.75 → `100%, 50% S>M` and 1.0 → `100%, 100% S>M`. The default 0.40213889
is 80%.

## Enums

All evenly spaced, `norm = i / (count - 1)`, every entry verified by setting the
center and reading the name back.

**Style (0)**, 14 values, step 1/13:

| norm | Style | norm | Style |
|---:|---|---:|---|
| 0.000000 | Clean | 0.538462 | Vari-Mu |
| 0.076923 | Versatile | 0.615385 | Classic |
| 0.153846 | Smooth | 0.692308 | Opto |
| 0.230769 | Punch | 0.769231 | Vocal |
| 0.307692 | Upward | 0.846154 | Mastering |
| 0.384615 | TTM | 0.923077 | Bus |
| 0.461538 | Op-El | 1.000000 | Pumping |

**Character (12)**, step 1/3: 0.0 Off, 0.333333 Tube, 0.666667 Diode,
1.0 Bright.

**Character Routing (13)**: 0.0 Pre Compression, 1.0 Post Compression.

**Side Chain Input (21)**, step 1/3: 0.0 Internal, 0.333333 External,
0.666667 Host Sync, 1.0 MIDI.

**Stereo Link Mode (27)**, step 1/3: 0.0 Mid, 0.333333 Side, 0.666667 M -> S,
1.0 S -> M.

**Oversampling (94)**, step 1/5: 0.0 Off, 0.2 2x, 0.4 4x, 0.6 8x, 0.8 16x,
1.0 32x.

**Maximum Lookahead (95)**, step 1/4: 0.0 Off, 0.25 Maximum 1 ms,
0.5 Maximum 5 ms, 0.75 Maximum 10 ms, 1.0 Maximum 20 ms.

**Side chain EQ Shape (band offset 5)**, 10 values, step 1/9: same list and same
normalized values as Pro-Q 4 (Bell, Low Shelf, Low Cut, High Shelf, High Cut,
Notch, Band Pass, Tilt Shelf, Flat Tilt, All Pass).

**Side chain EQ Slope (band offset 6)**, 10 values, step 1/9. Unlike Pro-Q 4
this is an enum, not a continuous control:

| norm | Slope | norm | Slope |
|---:|---|---:|---|
| 0.000000 | 6 dB/oct | 0.555556 | 36 dB/oct |
| 0.111111 | 12 dB/oct | 0.666667 | 48 dB/oct |
| 0.222222 | 18 dB/oct | 0.777778 | 72 dB/oct |
| 0.333333 | 24 dB/oct | 0.888889 | 96 dB/oct |
| 0.444444 | 30 dB/oct | 1.000000 | Brickwall |

## Gotchas that cost time

- **Release used to be unreachable by `formatted_value`.** Its display switches
  units from `944.9 ms` to `1.151 sec` partway up, so the endpoints parsed as 10
  and 2.5, the search concluded the parameter ran descending, and every real
  target came back as `outside param range (10.0 .. 2.5)`. Fixed 2026-08-18: the
  display parser scales time to milliseconds and frequency to Hz before
  comparing, so `"400 ms"`, `"1.5 sec"` and `"2 sec"` all land exactly. On any
  bridge older than that commit, use the table above.
- **Lookahead is gated by Maximum Lookahead.** Writing index 10 while index 95
  is `Off` succeeds, reports success, and changes nothing.
- **Dry Gain defaults to -INF.** Parallel compression needs index 17 raised;
  the Mix control (88) is a different path.
- **Auto Gain defaults to On.** Any output-level measurement taken across a
  threshold change is measuring auto gain as much as the compressor. Turn index
  19 off before calibrating anything by ear or by meter.

## How to re-probe

```bash
python reaperd.py cmd get_fx_parameters '{"target_track_name":"Scratch",
  "fx_name_contains":"Pro-C","fx_scope":"track","limit":240}'
python reaperd.py cmd set_fx_param '{"target_track_name":"Scratch",
  "fx_name_contains":"Pro-C","fx_scope":"track","param_index":1,
  "formatted_value":"-16 dB"}'
```

`set_fx_param` writes for real, so probe on a scratch track, never on a mix
track.
