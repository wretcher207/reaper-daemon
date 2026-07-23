# Smoke test: closed-loop verify (v3.12.0)

Ten minutes, live REAPER. This proves the new measure/verify/tune loop works
on real audio on your machine — the automated tests all run against a fake
bridge, so this is the part only you can do. Run the steps in order; each one
says what "pass" looks like.

## Setup (2 min)

1. Open a project with at least one track that actually plays something —
   ideally a bass or guitar stem with an EQ (ReaEQ is fine) already on it.
   Below, replace `Bass` with your track's real name.
2. Make sure `bridge/bridge_config.json` has `"allow_risk_level_3": true`.
   If you just changed it: **restart REAPER** — the bridge reads it once at
   startup. This is the #1 gotcha.
3. Park the edit cursor somewhere the track is actually playing (or make a
   time selection over a good bar or two).
4. Sanity check from a terminal in the repo folder:

   ```powershell
   python reaperd.py status
   ```

   Pass: it says `CONNECTED`.

## Step 1 — measure (2 min)

```powershell
python reaperd.py measure Bass --seconds 5
```

REAPER will briefly show a render window (that's the capture — it blocks
REAPER for ~a second, normal).

Pass looks like:

- A `[measure]` table with a LUFS-I number that looks sane for the track
  (a mixed bass stem is usually somewhere between −25 and −10).
- `scope=isolated_track (verified)` for a normal item-holding track — and if
  it says `full_mix` instead, that's the honest fallback for some routing
  setups, not a bug; the output will say the numbers describe the mix.
- No leftover WAV files: the capture goes to your temp folder and is deleted
  after analysis.

Fail: `REFUSED CAPTURE_BLOCKED` means step 2 of setup (gate + restart).
A `SILENT` warning means the cursor is parked in dead air — move it.

## Step 2 — verify an EQ cut (3 min)

Pick an EQ band frequency where the track has energy (bass: try 100–300 Hz).
With ReaEQ on the track:

```powershell
python reaperd.py verify Bass --seconds 5 -- set_fx_param '{\"target_track_name\":\"Bass\",\"fx_name_contains\":\"ReaEQ\",\"param_name_contains\":\"Gain-Band 2\",\"formatted_value\":\"-2.5 dB\"}'
```

(If `param_name_contains` errors as ambiguous, run
`python reaperd.py cmd get_fx_parameters '{"target_track_name":"Bass","fx_name_contains":"ReaEQ"}'`
and use `"param_index": <n>` instead — the daemon will tell you the choices.)

Two renders this time (pre and post). Pass looks like:

- `[verify] mutation set_fx_param: ok`
- A `dLUFS-I` line with a small negative number (you cut energy, loudness
  drops a little).
- With Post Mortem installed: a "biggest spectrum moves" line whose largest
  move sits near the band you cut. This is the headline — the tool measured
  your EQ move in the actual audio.
- `VERDICT: VERIFIED` and exit code 0 (`echo $LASTEXITCODE` → `0`).

Also check REAPER's undo history (Ctrl+Alt+Z): the EQ change is there as a
normal undoable step.

## Step 3 — tune a gain to a LUFS target (3 min)

This one runs through the MCP server, so do it from a Claude (or other MCP)
session wired to `reaper_mcp.py`, with a prompt like:

> Use tune_param on track "Bass": FX "ReaEQ", the overall output "Gain"
> parameter, target metric lufs_i, delta -3.0, tolerance 0.5.

(Any gain-like parameter works — a utility/volume plugin's Gain is ideal.)

Pass looks like:

- The model warns you it will take several renders (it should — the tool
  description tells it to).
- Up to 5 quick renders, then a result with `"status": "CONVERGED"`, a final
  parameter display value, and a measured delta within ±0.5 of −3.0.
- `UNCONVERGED` with an honest "best value left applied" note also counts as
  the tool working — some parameters can't hit an exact target in 5 steps.

Fail worth reporting: a `NON_MONOTONE` abort on a plain gain knob (that
would mean the direction logic is wrong), or any answer that claims
convergence without numbers.

## Step 4 — undo behavior (1 min)

1. Press Ctrl/Cmd+Z once. Pass: the LAST parameter set (tune's final value)
   reverts — one undo point per set is the designed behavior.
2. Keep undoing until the EQ cut from step 2 reverts too. Pass: each verify/
   tune change is a separate, normal undo step; nothing else in your project
   was touched (check track selection, solos, time selection, render settings
   — all should be exactly as you left them).

## If something breaks

Keep the terminal output and run the failing step again with `--json` (for
`measure`/`verify`) — the JSON includes the exact error code and, on capture
failures, the path of any kept debug WAV. File it with the branch name
`feat/verify-loop`.
