# FabFilter Pro-Q 4 Parameter Map

`VST3: Pro-Q 4 (FabFilter)`, **740 parameters**.

Captured live 2026-08-18 on REAPER 7.79 from a scratch track in
`used-claude-mix.rpp` (a fresh instance at plugin defaults, deleted afterwards).
Indices are from `get_fx_parameters` with `fx_scope: "track"`.

Every value below was measured by writing it and reading the display back, not
inferred from the manual.

## Layout

The parameter list is three blocks. Nothing here is discoverable by name search
alone, because 24 bands repeat the same 23 names.

| Range | Contents |
|---|---|
| 0 – 551 | **24 bands, stride 23.** Band *n* (1-based) starts at `(n-1) * 23`. |
| 552 – 575 | Globals (processing, output, analyzer). |
| 576 – 599 | `Band n Spectral Tilt`, one per band, **detached from the band block**. |
| 600 – 739 | REAPER's own wrapper params (Host Bypass, Internal, MIDI CC). Ignore. |

### The band block, offsets from the band's base index

| Off | Name | Type |
|----:|------|------|
| 0 | Used | enum, `Unused` / `Used` |
| 1 | Enabled | enum, `Enabled` / `Disabled` |
| 2 | Frequency | Hz, log |
| 3 | Gain | dB, linear |
| 4 | Q | log |
| 5 | Shape | enum, 10 |
| 6 | Slope | **continuous**, see below |
| 7 | Stereo Placement | enum, 5 |
| 8 | Speakers | enum, 15 |
| 9 | Dynamic Range | dB, linear |
| 10 | Dynamics Enabled | enum |
| 11 | Dynamics Auto | enum |
| 12 | Threshold | dB, piecewise, `Auto` at the top |
| 13 | Attack | %, linear |
| 14 | Release | %, linear |
| 15 | External Side Chain | enum |
| 16 | Side Chain Filtering | enum |
| 17 | Side Chain Low Frequency | Hz, log (different range from offset 2) |
| 18 | Side Chain High Frequency | Hz, log |
| 19 | Side Chain Audition | enum |
| 20 | Spectral Enabled | enum |
| 21 | Spectral Density | %, linear |
| 22 | Solo | enum |

So Band 1 Frequency is index 2, Band 5 Frequency is `4*23 + 2 = 94`, and Band 24
Frequency is `23*23 + 2 = 531`.

**`Used` is the band's real on switch.** A fresh instance has all 24 bands
`Unused` but `Enabled`; setting a frequency and gain on an unused band does
nothing audible until offset 0 is set to `Used` (1.0). `Enabled` is the per-band
bypass a human clicks, which is a different thing.

## Curves

Verified by writing a display value and reading the normalized result back. The
residuals below are the bridge's binary-search error, not curve error.

**Frequency (band offset 2)**, 10 Hz to 30 kHz, log:

```
norm = log10(Hz / 10) / 3.477121        Hz = 10 * 10^(norm * 3.477121)
```
Probed: 20 Hz → 0.0865714 (formula 0.086568), 100 → 0.28759366 (0.287597),
1000 → 0.57518792 (0.575188), 10000 → 0.86278212 (0.862779), 20000 → 0.94935417
(0.949350). Exact to the search residual across the whole range.

**Gain (offset 3) and Dynamic Range (offset 9)**, ±30 dB, linear:

```
norm = (dB + 30) / 60                   dB = norm * 60 - 30
```
Probed on both: +6 → 0.5999167, -12 → 0.29991672, -30 → 0.0, +30 → 0.99991673.

**Q (offset 4)**, 0.025 to 40, log:

```
norm = log10(Q / 0.025) / 3.20412       Q = 0.025 * 10^(norm * 3.20412)
```
Probed: 0.025 → 0.0, 0.1 → 0.18722242 (formula 0.187810), 1.0 → 0.5,
10 → 0.81209141 (0.812190), 40 → 0.99998307. Good to about 0.0006 normalized,
which is well under a display step.

**Side Chain Low / High Frequency (offsets 17, 18)**, 10 Hz to **20 kHz**, log.
A different range from the band frequency, so do not reuse that formula:

```
norm = log10(Hz / 10) / 3.30103         Hz = 10 * 10^(norm * 3.30103)
```
Probed: 100 → 0.30293512, 5000 → 0.81761324.

**Attack, Release, Spectral Density (offsets 13, 14, 21)**: plain percent,
`norm = pct / 100`. 0 → 0.00%, 0.25 → 25.0%, 1.0 → 100%.

**Threshold (offset 12)**, piecewise linear, and the one parameter here that
`formatted_value` cannot set (see Gotchas):

```
norm >= 0.2:   norm = 1 + dB / 60       dB = 60 * (norm - 1)
```
Measured anchors: 0.2 → -48.0, 0.25 → -45.0, 0.5 → -30.0, 0.666667 → -20.0,
0.75 → -15.0, 0.9 → -6.0, 0.95 → -3.0, 0.99 → -0.6 dB.
Below 0.2 the curve steepens into a tail nothing needs: 0.0 → -90.0,
0.05 → -81.0, 0.1 → -72.0, 0.15 → -60.0 dB.
**norm 1.0 displays `Auto`**, not 0 dB. Anything from 0.999 up reads `Auto`.

**Output Level (index 556)**, ±36 dB, linear:

```
norm = (dB + 36) / 72
```
Probed: -6 → 0.41659725, +6 → 0.58326393.

**Gain Scale (index 555)**: 0 to 200%, linear. norm 0.5 = 100%.

**Slope (offset 6) is continuous in Pro-Q 4, not an enum.** Intermediate values
really do display 3.2 dB/oct, 39.8 dB/oct and so on. The standard slopes sit on
clean normalized values:

| norm | Slope | norm | Slope |
|---:|---|---:|---|
| 0.00 | 0 dB/oct | 0.60 | 36 dB/oct |
| 0.05 | 3 dB/oct | 0.65 | 42 dB/oct |
| 0.10 | 6 dB/oct | 0.70 | 48 dB/oct |
| 0.20 | 12 dB/oct | 0.75 | 60 dB/oct |
| 0.30 | 18 dB/oct | 0.80 | 72 dB/oct |
| 0.40 | 24 dB/oct | 0.85 | 84 dB/oct |
| 0.50 | 30 dB/oct | 0.90 | 96 dB/oct |
| | | 0.95 – 1.00 | Brickwall |

Linear at 60 dB/oct per unit up to norm 0.6; above that it expands. Use the
table, not a formula.

## Enums

Every enum here is evenly spaced: `norm = i / (count - 1)`. Verified by setting
each center and reading the name back.

**Shape (offset 5)**, 10 values, step 1/9:

| norm | Shape | norm | Shape |
|---:|---|---:|---|
| 0.000000 | Bell | 0.555556 | Notch |
| 0.111111 | Low Shelf | 0.666667 | Band Pass |
| 0.222222 | Low Cut | 0.777778 | Tilt Shelf |
| 0.333333 | High Shelf | 0.888889 | Flat Tilt |
| 0.444444 | High Cut | 1.000000 | All Pass |

**Stereo Placement (offset 7)**, 5 values, step 1/4: 0.0 Left, 0.25 Right,
0.5 Stereo, 0.75 Mid, 1.0 Side.

**Speakers (offset 8)**, 15 values, step 1/14: 0.0 All Speakers,
0.071429 All (excl. LFE), 0.142857 LFE, 0.214286 Center, 0.285714 L/R (Front),
then surround positions up to 1.0 Ltr/Rtr. Stereo work only ever needs the first
two; the default is `All (excl. LFE)`.

**Processing Mode (552)**, step 1/2: 0.0 Zero Latency, 0.5 Natural Phase,
1.0 Linear Phase.

**Processing Resolution (553)**, step 1/4: 0.0 Low, 0.25 Medium, 0.5 High,
0.75 Very High, 1.0 Maximum.

**Character (554)**, step 1/2: 0.0 Clean, 0.5 Subtle, 1.0 Warm.

## Fresh-instance defaults

Worth knowing, because a new instance is what an agent inserts. Per band:
`Unused`, `Enabled`, 1000.0 Hz (0.575188), 0.00 dB (0.5), Q 1.000 (0.5), Bell,
12 dB/oct (0.2), Stereo (0.5), All excl. LFE (0.071429), Dynamic Range 0.00 dB,
Threshold -20.0 dB (0.666667), Attack/Release 50%, Spectral disabled.
Globals: Zero Latency, Medium resolution, Clean, Gain Scale 100%, Output 0.00 dB,
Auto Gain Off, Analyzer Range 90 dB, Tilt 4.5 dB/oct, Display Range 12 dB.

## Gotchas that cost time

- **`formatted_value` fails on Threshold** with `FORMATTED_VALUE_UNSUPPORTED:
  parameter is not numeric`. The bridge's binary search samples the range, hits
  the `Auto` display at the top, and rejects the whole parameter. Use
  `normalized_value` with the formula above. The same trap will hit any
  parameter whose display goes non-numeric at an endpoint.
- **A failed `formatted_value` search leaves the parameter moved.** The refusal
  is reported after the probe has already written, so the value can be parked
  wherever the search last tried. Read the parameter back after any `ok:false`
  from `set_fx_param` and restore it.
- Band and Spectral Tilt live in **different blocks**. `Band 3 Spectral Tilt` is
  index 578, not part of band 3's stride-23 block.
- 500 is the largest `limit` `get_fx_parameters` returns, so a full dump of this
  plugin takes two calls (`offset: 0` and `offset: 500`).

## How to re-probe

```bash
python reaperd.py cmd get_fx_parameters '{"target_track_name":"Scratch",
  "fx_name_contains":"Pro-Q","fx_scope":"track","offset":0,"limit":500}'
python reaperd.py cmd set_fx_param '{"target_track_name":"Scratch",
  "fx_name_contains":"Pro-Q","fx_scope":"track","param_index":2,
  "formatted_value":"100 Hz"}'
```

Two points fit a log curve, three confirm it. `set_fx_param` writes for real, so
probe on a scratch track, never on a mix track. Use forward slashes in any path
inside these payloads.
