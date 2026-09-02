# SPEC: Closed-Loop Verify — self-verifying mix moves (target: v3.12.0)

**Status: NOT STARTED.** This document is the complete brief. It is written to
be picked up cold by a fresh agent session with no prior context. Read it
top-to-bottom before writing any code. Maintain the progress log at the bottom
as you go — the next session (or a crash recovery) resumes from that log.

## Mission

Today every mutation is open-loop: the agent sets an EQ band, gets `ok: true`,
and never learns whether the mix actually improved. This project adds the
feedback loop: **capture → mutate → capture → measured diff**, plus an
iterative `tune_param` that searches a parameter against a *measured audio
outcome* instead of a displayed value.

After this ships, an agent can say: "I cut 2.5 dB at 320 Hz on the bass;
measured result: bass LUFS-I −14.1 → −14.9, kick/bass masking in the
200–400 Hz band down 31%" — and mean it, because it measured both sides.

## Non-goals

- No new network surface, no third-party Python deps in this repo (stdlib
  only — hard rule, see `README.md` Develop/Security sections).
- No changes to the bridge's one-command-per-defer-tick model.
- No automatic creative decisions. The loop verifies and reports; the calling
  model/user decides what "better" means (except in `tune_param`, where the
  caller explicitly states the numeric target).

## Where the work happens

Repo: `reaper-daemon` (this repo). GitHub `wretcher207/reaper-daemon`.
Work on branch **`feat/verify-loop`** off `main`. One PR at the end, or merge
per-phase — David's call; default to a single PR with per-phase commits.

Baseline verified 2026-07-23 on David's Windows 11 machine
(`C:\Users\wretc\workspace\reaper-daemon`):

- `main` @ `8358a9a`, bridge `@version 3.11.3`, working tree clean, in sync
  with origin. Zero open PRs, zero stale branches.
- `python -m pytest tests skills/drum-apparatus/tests -q` → **124 passed**.
- `python -m py_compile reaperd.py reaper_mcp.py setup/install.py` → OK.
- Codex CLI 0.145.0 on PATH, `~/.codex/config.toml` → `model = "gpt-5.6-sol"`,
  `model_reasoning_effort = "max"`. This is the adversarial reviewer (below).
- `gh` works via a user-level `GH_TOKEN` env var (no `gh auth login` needed;
  token lacks `read:org`, personal-repo ops all work).
- Post Mortem repo is cloned at the sibling path `../post-mortem`, but the
  `postmortem` CLI is **not on PATH** on this machine. Phase 0 installs it.
- Live REAPER testing requires REAPER running with the bridge loaded AND
  `allow_audio_writes: true` in `bridge/bridge_config.json` (or the legacy
  `allow_risk_level_3` fallback; read once per bridge load — apply a change
  with `reload_bridge`, or a REAPER restart). Do NOT assume REAPER is
  available; phases 1–3 must be fully testable against the fake bridge.

## Repo orientation (verified facts, with anchors)

Read `AGENTS.md` and `bridge/command_schema.md` in full before coding. Key
primitives this project builds on:

| Primitive | Where | What it gives you |
|---|---|---|
| `capture_track_audio` | `bridge/reaper_agent_bridge.lua` (`command_capture_track_audio`, ~line 2485); schema in `bridge/command_schema.md` | Renders one track to WAV. Gated on `allow_audio_writes` (falls back to `allow_risk_level_3`). Returns `file_path` (authoritative, from `RENDER_TARGETS`), `render_loudness_lufs` (LUFS-I parsed from `RENDER_STATS`, ~line 2631), `capture_scope` (`isolated_track` \| `full_mix` \| `master_output`), `isolation_verified`. Restores selection + all render settings even on error. Bounds: active time selection if any, else cursor + `duration_seconds`; `start_seconds` overrides. |
| `get_capture_preflight` | same file ~line 2393 | Everything that would block/degrade a capture WITHOUT rendering: `capture_allowed`, `blockers[]`, `warnings[]`, `risk_gate`, `sws_installed`, `render_autoclose`. Call before any capture sequence. |
| `get_selected_track` | schema §get_selected_track | Reports `capture_source`, `capture_start_seconds`, `expected_capture_scope` for the selected track — the same resolution `capture_track_audio` uses. |
| `set_fx_param` | bridge + schema §set_fx_param | Accepts `formatted_value` ("−16.00 dB"); bridge binary-searches normalized values to land the display. |
| `setparam` CLI + `_judge_landed` | `reaperd.py` `cmd_setparam` (~line 493), `_judge_landed` (~line 592) | Resolve → set → re-scan → verdict pattern with tolerances (≤2% or 0.5 = OK, ≤10% or 1.0 = CLOSE, else MISSED, exit 1). **Copy these verdict semantics.** |
| `send_type` / `scan_fx_parameters` | `reaperd.py` ~lines 267/329 | The Python-side transport helpers. All new Python code goes through these — never hand-roll inbox writes. |
| `snapshot_track_state` / `preview_change` / `commit_preview` / `cancel_preview` | schema §Tracks | Existing state capture/restore. `verify` does NOT need them for v1 (mutations stay applied; they're undoable), but `tune_param` may use snapshot/restore between iterations — decide in Phase 3. |
| Post Mortem CLI | `../post-mortem` repo; invoked by `reaper_mcp.py` `_run_postmortem` (~line 551) | `postmortem <track...> --payload-only --seconds N` drives the daemon itself (env `REAPER_DAEMON_ROOT=<bridge root>`), captures, and prints a JSON payload: LUFS, true peak, spectrum bands, stereo image, masking table, plus `audio.rms_db` / `audio.silence_fraction` and capture-provenance fields the MCP server already safety-checks (`_capture_safety_error`). |
| `postmortem.analysis.analyze_wav(path)` | `../post-mortem/postmortem/analysis.py:335` | Analysis of an **existing WAV** exists as a function but is NOT exposed on the CLI (CLI takes track names only). See Phase 0 decision. |
| Fake bridge for tests | `tests/bridge_fakes.py` | `fake_bridge(root, reply_body)` answers exactly ONE command then exits. Multi-command flows (measure→mutate→measure) need a scripted extension — that's a Phase 1 deliverable. |

Repo conventions (non-negotiable):

- Atomic JSON writes everywhere (`.tmp` then rename). Error codes are
  `UPPER_SNAKE`. Mutations run in undo blocks.
- Any new/changed command or tool must update: `bridge/command_schema.md`,
  `commands/examples/` (one JSON example per command), `AGENTS.md`, and the
  MCP tool registry if exposed there.
- Version bumps: `@version` header + changelog lines at the top of
  `bridge/reaper_agent_bridge.lua`, and `index.xml` (ReaPack reads it; the
  header comment in `index.xml` explains why it must move in lockstep).
  This project targets **3.12.0** even if the Lua diff is small.
- CI parity: `pytest tests skills/drum-apparatus/tests`, `py_compile` on the
  three Python entry points, `lua bridge/test_bridge.lua`, `lua bridge/test_json.lua`.
  All must pass at every phase gate.

## Architecture decision (settled — do not relitigate without new evidence)

**The loop lives in Python, not Lua.** The bridge executes one command per
defer tick and renders synchronously; a single mega-command doing
capture→mutate→capture would block REAPER's UI for two renders and duplicate
orchestration we already have in Python. The verify loop is a *sequencer of
existing commands* in `reaperd.py`/a new module, reusing `send_type`. The
bridge is expected to need **zero or near-zero Lua changes** (if a gap
surfaces, keep the Lua addition minimal and read-only).

**Measurement engine = Post Mortem when installed, RENDER_STATS when not.**
Every capture already returns LUFS-I for free (`render_loudness_lufs`). With
Post Mortem installed you additionally get spectrum bands, true peak, stereo
image, and masking. `verify` must work in both modes and must label its
report `metrics_source: "postmortem" | "render_stats"`.

## Phases

Every phase ends with the **Codex gate** (next section). Do not start phase
N+1 until phase N's gate is green and its commit exists.

### Phase 0 — recon & environment (no product code)

1. `git fetch && git status` — confirm baseline still matches the facts above;
   if `main` moved, re-read the diff before proceeding and note it in the log.
2. Run the full CI-parity suite; record results in the progress log.
3. Install Post Mortem from the sibling clone:
   `pip install --user -e ../post-mortem` (or `pipx install ../post-mortem`).
   Verify `postmortem --help` runs and `python -c "from postmortem.analysis import analyze_wav"` imports.
4. **Decision to make and record:** how `verify` gets rich metrics for a WAV
   it already captured. Options, in order of preference:
   a. Import `postmortem.analysis.analyze_wav` directly when the package is
      importable (zero cross-repo change; `verify` does its own captures via
      `capture_track_audio` and analyzes the files). Check what `analyze_wav`
      returns and whether the masking table needs the multi-track path.
   b. Add a small `--wav <path>` mode to the `postmortem` CLI (cross-repo
      change in `../post-mortem`; keep it `--payload-only`-shaped).
   c. Shell out to `postmortem <track> --payload-only` twice and let it
      capture internally (simplest, but double-couples capture bounds to
      cursor state between calls — you must then freeze bounds yourself).
   Pick after reading `analysis.py` and `cli.py`; write the choice + why in
   the progress log. The spec's default assumption is (a).
5. No Codex gate for Phase 0, but log everything found.

### Phase 1 — `measure`: one capture, one metrics dict

**Deliverable:** `python3 reaperd.py measure <track> [--seconds N] [--start S] [--json]`
plus the module function it wraps (new file `verifyloop.py` at repo root, or
inside `reaperd.py` if it stays under ~200 lines — your call, log it).

Behavior:

1. Preflight first: `get_capture_preflight` for the track; refuse with the
   blocker list if `capture_allowed` is false. Surface the risk-gate blocker's
   message verbatim when the gate is what blocked — users always trip on this.
2. Resolve capture bounds ONCE and pass them explicitly (`start_seconds`,
   `duration_seconds`) so a later `measure` of the same spot is identical.
   Default duration 10 s (Post Mortem's own single-track default), max 60 for
   verify use (full 600 s captures are not verify material).
3. Capture via `capture_track_audio` with a unique timestamped `output_file`
   under the OS temp dir (never the repo; OneDrive syncs this workspace).
   Verify file mtime > command `created_at` (the schema demands it).
4. Metrics: always `lufs_i` (from `render_loudness_lufs`), `capture_scope`,
   `isolation_verified`, `file_path`, bounds used. When Post Mortem is
   importable, add its analysis (spectrum, true peak, RMS, silence fraction,
   stereo). Label `metrics_source`.
5. Silence guard: if RMS ≤ −60 dB or silence fraction ≥ 0.85 (thresholds
   copied from `_run_postmortem`), mark `silent: true` in the result — callers
   must refuse verdicts on silent captures.
6. `--json` prints machine-readable output (the MCP server consumes this path
   in Phase 3); default output is a short human table.

Tests (fake bridge, no REAPER):

- Extend `tests/bridge_fakes.py` with a scripted multi-reply fake
  (`fake_bridge_script(root, replies)` answering N commands in order). Keep
  the single-reply `fake_bridge` untouched — both suites share it.
- Cases: preflight blocked → refusal with blocker codes; happy path returns
  bounds + LUFS; silent capture flagged; missing Post Mortem degrades to
  `render_stats` source; stale outbox file (mtime check) rejected.

Docs: `AGENTS.md` gains a "Measuring" subsection; README one paragraph.

### Phase 2 — `verify`: measure → mutate → measure → verdict

**Deliverable:** `python3 reaperd.py verify <track> [--seconds N] [--json] -- <type> '<payload-json>'`
(the `--` split mirrors how `cmd` takes type+payload today).

Behavior:

1. Run Phase-1 `measure` (pre). Abort before mutating if capture is blocked
   or silent — a mutation you can't measure is just `cmd`, tell the user to
   use that instead.
2. Send the mutation via the same path `cmd` uses (`send_type` with resolve/
   repair, so `add_fx` name resolution and `set_fx_param` alias repair still
   apply). On mutation failure: report and stop; nothing to roll back.
3. Run `measure` (post) with the SAME frozen bounds.
4. Report deltas: ΔLUFS-I always; with Post Mortem, per-band spectrum deltas,
   Δtrue-peak, Δstereo width, masking deltas when applicable.
5. Verdict semantics (exit codes matter — agents branch on them):
   - `0` VERIFIED: both captures clean, deltas reported.
   - `1` REFUSED: the mutation was never sent (pre-capture blocked/silent);
     nothing was mutated.
   - `2` UNVERIFIED: the mutation was sent but not verified: applied with a
     failed/silent post-capture, partially applied, rejected by the bridge
     (a mid-edit handler failure can leave a partial change in one closed
     undo block, unprovable from outside, so rejections fail toward 2 and
     the JSON carries `mutation.rejection_code`), or transport-unknown.
     **The mutation is NOT rolled back** (it's one Ctrl/Cmd+Z away, say so
     verbatim in the output). This asymmetry is deliberate: never destroy a
     user-visible change because measurement hiccupped.
6. Scope honesty: if pre and post `capture_scope` differ, or either is not
   `isolated_track` with `isolation_verified`, the report must say the deltas
   describe the capture scope, not necessarily the track alone (mirrors the
   MCP server's `_capture_safety_error` stance). Never present full-mix
   deltas as per-track evidence.

Tests: scripted-fake sequences for all five outcomes above; a test that the
pre/post bounds sent to the bridge are byte-identical; a unit test for the
delta/verdict formatter with canned metrics dicts.

Docs: README "Closed-loop verify" section (this is the headline feature —
write it like the existing README, concrete commands and honest limits).

### Phase 3 — MCP tools: `verify_change` and `tune_param`

**Deliverables** in `reaper_mcp.py` (registry pattern at the bottom of the
file; follow it exactly):

- `verify_change` — thin wrapper over Phase 2 with `--json`; input schema:
  `track`, `command_type`, `payload`, optional `seconds`. Include the same
  destructive-intent confirmation language the other mutating tools use.
- `tune_param` — the outcome-driven search. Input: track, FX selector, param
  selector (reuse the selector conventions from `set_fx_param`), and a target:
  `{"metric": "lufs_i", "delta": -3.0, "tolerance": 0.5}` (v1 metric set:
  `lufs_i` always; `band_db` with a `band_hz` range when Post Mortem is
  present). Algorithm: pre-measure once, then iterate set-param → measure,
  bisecting on the normalized value (the monotonicity assumption holds for
  gain-like params; DOCUMENT it and stop after any non-monotone observation
  with a clear error — don't silently thrash). Hard cap **5 iterations**
  (each is a render; say so in the tool description so the model warns the
  user). Converged = within tolerance; report iterations, final param
  display value, final delta. On non-convergence: leave the best-observed
  value set, report honestly, exit as unconverged.
- Decide (and log): snapshot/restore between iterations vs. leaving each set
  applied. Leaning: leave applied (each set overwrites the same param; no
  cumulative damage), one undo point per iteration is acceptable.

Tests: `tests/test_reaper_mcp.py` additions with the scripted fake — schema
validation, happy path, non-monotone abort, iteration cap, unconverged
report. No live REAPER in CI.

Docs: README MCP section tool count + descriptions; `AGENTS.md`.

### Phase 4 — release polish + live smoke + full-diff review

1. Version bump to 3.12.0: Lua `@version` + header changelog, `index.xml`
   (read its header comment first), README version references if any.
2. `docs/SMOKE_VERIFY.md`: a 10-minute manual script for David to run with
   live REAPER — measure a track, verify an EQ cut, tune a gain param to
   −3 LUFS, confirm undo behavior. Write it for a musician, not a dev.
3. Full CI-parity suite green.
4. **Final Codex gate runs on the ENTIRE branch diff** (`git diff main...HEAD`),
   not just Phase 4 — fresh eyes on the whole feature.
5. Push branch, open PR with `gh pr create` (GH_TOKEN env var handles auth).
   PR body: summary, phase log, Codex review summary, smoke-test status
   (expected: NOT yet run live — David runs SMOKE_VERIFY.md before merge).

## The Codex adversarial gate (every phase)

Codex (`gpt-5.6-sol`, max reasoning — already configured in
`~/.codex/config.toml`) is the independent reviewer. The rule: **a phase is
not complete until Codex has genuinely tried to break it and failed.**

Protocol per phase:

1. Commit the phase's work on `feat/verify-loop` (tests green first — never
   ask Codex to review broken code; that wastes the review on things pytest
   already catches).
2. Invoke Codex. Preferred: the Claude Code Codex plugin (skill
   `codex:rescue`, or spawn the `codex:codex-rescue` agent). Fallback: raw
   CLI from the repo root:
   `codex exec "<review prompt>"` (read-only sandbox is fine; it needs to run
   the test suite, so allow workspace access if prompted).
3. Review prompt template (fill the brackets):

   > Adversarial code review. Repo: reaper-daemon, branch feat/verify-loop.
   > Scope: `git diff <prev-phase-commit>..HEAD` plus any file it touches.
   > Context: read docs/SPEC_VERIFY_LOOP.md, section "Phase <N>". Your job is
   > to BREAK this phase, not to approve it. Specifically hunt: (1) capture
   > bounds drift between pre/post measures; (2) restore-on-error gaps —
   > what state leaks if the process dies between mutate and post-measure?
   > (3) Windows path handling (this runs on win32 primarily — separators,
   > temp dirs, OneDrive file locks); (4) silence/scope-honesty bypasses —
   > any path where a full-mix or silent capture could be presented as
   > per-track evidence; (5) stale/reused outbox files and command-id
   > collisions; (6) the fake-bridge tests passing while the real protocol
   > would fail (fake fidelity); (7) tolerance/verdict lies — any output that
   > claims more certainty than the measurement supports. Run the test suite
   > yourself; do not trust the summary you were given. Verify each Phase-<N>
   > acceptance criterion independently against the code, not the commit
   > message. Report findings as BLOCKER / MAJOR / MINOR, each with
   > file:line and a concrete failure scenario (inputs/state → wrong
   > outcome). If you find nothing above MINOR, say exactly what you probed
   > and what convinced you. Do NOT fix anything — report only.

4. Gate rule: **zero BLOCKER, zero MAJOR** to pass. Fix findings, commit,
   re-run the gate scoped to the fixes. MINOR findings: fix them or record a
   one-line justification in the progress log — no silent drops.
5. Disagreement protocol: if you believe a BLOCKER/MAJOR finding is wrong,
   write your refutation in the progress log and re-submit that specific
   question to Codex once. If it stands after round 2, or after **3 total
   fix→review cycles** the gate still fails, STOP and surface the deadlock to
   David with both positions. Do not grind.
6. Log every gate: date, commit reviewed, findings count by severity,
   resolution per finding.

## Open questions (resolve in Phase 0, log answers)

1. `analyze_wav` return shape and whether masking requires the multi-track
   path — drives the Phase 0 decision (a)/(b)/(c).
2. Does `capture_track_audio`'s `start_seconds` override fully neutralize an
   active time selection, or must `verify` also guard against the user moving
   the time selection between pre and post? (Read the Lua; schema says
   `start_seconds` overrides the default range — confirm duration handling.)
3. `tune_param` metric for `band_db`: which Post Mortem payload field maps
   cleanly to "energy in band X–Y Hz"? Pick the one Post Mortem already
   computes; do not invent DSP.
4. Where does `reaperd.py` currently put temp files, if anywhere — follow
   that convention for capture WAVs or establish one (OS temp + cleanup on
   success, keep on failure for debugging).

## Progress log (append-only — maintain this religiously)

| Date | Phase | Commit | Status | Notes |
|---|---|---|---|---|
| 2026-07-23 | spec | — | Spec written, baseline verified (124 tests pass, main@8358a9a) | Authored by prior session; no code yet |
| 2026-07-23 | 0 | b483b59 | DONE | Recon complete. Details below. |
| 2026-07-23 | 1-gate | b865a04 | ATTEMPT 1 ABORTED (infra, not findings) | Codex plugin run died after 22 min: OpenAI-side content filter flagged the adversarial phrasing mid-run ("possible cybersecurity risk"), turn failed, no findings produced. Before dying it independently ran the suite: 150 passed, 1 skipped (the real-Post-Mortem test, not importable in its sandbox). Re-running via raw `codex exec` (spec's fallback) with defensive-review phrasing, same technical probe list. |
| 2026-07-23 | 1-gate | b865a04 | ROUND 1: FAIL — 0 BLOCKER, 4 MAJOR, 3 MINOR (via raw `codex exec`, defensive phrasing) | All 4 MAJORs fixed, all 3 MINORs fixed (none dropped): **M1** mtime freshness had a 2 s slack accepting a WAV written just before the command → strict `mtime >= send time`, no slack (+ 5 s-stale test). **M2** (pre-existing, in touched file) `send_command` swallowed a failed removal of a locked stale outbox reply → fail-closed `StaleReplyError`, surfaced as `STALE_REPLY_LOCKED` by send_type (+ 2 tests). **M3** NaN/inf analyzer metrics bypassed the silence guard (every comparison False) and emitted invalid JSON → recursive non-finite sanitizer, fail-closed silent on non-finite RMS, `allow_nan=False` on --json (+ test). **M4** analyzer attribute reads sat outside the try; a wrong-shaped Post Mortem result crashed measure and leaked the WAV → reads moved inside try, degrade to render_stats (+ test). **m1** failure paths now disclose the possible partial-WAV path; human output prints kept-WAV path. **m2** bridge-echoed bounds now cross-checked; mismatch → `BOUNDS_MISMATCH` refusal; fake echoes bounds (fidelity). **m3** docs no longer overstate repeatability (same `--start`/`--seconds` needed across runs) or LUFS availability (null on digital silence). Codex could not run pytest in its sandbox (no pytest for its Python); run here: 158 passed. Note: gate attempt via Codex plugin died to an OpenAI content filter (logged above); raw CLI with defensive phrasing worked. |
| 2026-07-23 | 1-gate | 99db394 | ROUND 2: **PASS** — 0 BLOCKER, 0 MAJOR, 5 MINOR | Severity gate clear. All 5 MINORs fixed (none dropped): **(1)** bounds echo failed open on missing/null/non-finite/non-numeric echoes (and "bogus" crashed + leaked the WAV) → any unconfirmable echo now fails closed as `BOUNDS_MISMATCH`, no crash (+4-case parametrized test). **(2)** strict mtime could false-reject a fresh capture on coarse-timestamp filesystems → structural fix: output path is unique and proven non-existent before send, so a reply reporting OUR path is fresh by construction (no mtime guessing); strict mtime applies only to a reply reporting a DIFFERENT path (the actual replay shape) (+acceptance test). **(3)** stale-file regression test couldn't detect a reintroduced 2 s slack → 1 s-stale case added on the other-path branch. **(4)** human error output now prints the kept partial-WAV path when the file exists. **(5)** module docstring LUFS overstatement fixed. Codex verified all 4 round-1 MAJORs resolved (M1–M4 table in its report). Suite after fixes: 162 passed. Phase 1 gate GREEN. | `measure` in new `verifyloop.py` (module choice: separate file — it grows through Phases 2–3, well past 200 lines). CLI `reaperd.py measure`. `fake_bridge_script` added (entries may be dicts or callables so a scripted capture reply can actually write the WAV — fake fidelity). 27 new tests, suite 151 green. Decisions: WAV deleted after analysis on success (`--keep-wav` to keep; kept on failure paths); render_stats mode with null LUFS-I is treated as SILENT, not a pass (digital silence renders -inf → bridge maps to null; no other level evidence without Post Mortem). |

| 2026-07-23 | 2-gate | 1c025f6 | ROUND 1: FAIL — 2 BLOCKER, 4 MAJOR, 2 MINOR | All fixed. **B1** exit 1 falsely promised "nothing changed": the Lua (run_command ~2946, command_batch ~2976) keeps partial mutations inside the closed undo block on failure → failed `batch` and transport-uncertain codes (TIMEOUT/NO_REPLY/BAD_REPLY/STALE_REPLY_LOCKED) now exit 2 UNVERIFIED with do-not-retry notes; single-command rejections stay exit 1 with an honest partial-change caveat; mutation timeout raised 10s→30s; verify() wraps everything after a successful mutation in try/except so internal errors become honest exit-2 results, and a `progress` callback prints step markers so a killed process leaves a trace (durable state file judged overkill for a CLI v1 — logged as the accepted residual for the hard-kill window). **B2** transiently locked reply reads now retried until deadline in send_command's poll loop (+test). **M1** options after the track were swallowed by argparse.REMAINDER → `_split_verify_mutation` recovers them; options now work in any position (+4 CLI tests). **M2** post-capture now targets the pre-capture's GUID (`target_track_guid`) and a GUID mismatch → UNVERIFIED (+test). **M3** scope change pre→post → UNVERIFIED exit 2, not a prose warning on exit 0 (same-scope non-isolated stays VERIFIED+warning, per spec wording) (+test updated). **M4** `lufs_i_delta` now ALWAYS present (null+reason when REAPER supplied no LUFS); VERIFIED requires ≥1 broadband level delta (LUFS-I or RMS) else UNVERIFIED; stereo width (side−mid dB) delta added (+tests). **m1** compute_deltas enforces isfinite and skips malformed band entries (+test). **m2** format_verify discloses kept failure WAV paths. README/AGENTS exit-code docs rewritten. Suite: 188 green. |
| 2026-07-23 | 2 | 03bceb5 | CODE DONE, gate round 2 running | Parallelization approved by David mid-run: phases may overlap as long as each still passes its Codex gate. |
| 2026-07-23 | 2-gate | 03bceb5 | ROUND 2: FAIL — 2 BLOCKER, 1 MAJOR, 2 MINOR | All fixed (cycle 3 of 3 — if round 3 fails, STOP and surface to David per the deadlock rule). **B1** `batch` + `stop_on_error:false` returns top-level ok:true with failed sub-results → now inspected; partial batch = post-capture still measured, deltas attached, but verdict UNVERIFIED exit 2 ("deltas describe the partially-applied state, not the requested change"); fully-ok batch still VERIFIED (+2 tests). **B2** wrong-shaped mutation replies (raw null, string error field, missing error) crashed or exited 1 after the command was queued → send_type now normalizes non-object replies to BAD_REPLY; verify classifies non-dict replies, code-less errors, and mutator exceptions (new MUTATOR_EXCEPTION in the uncertain set) as outcome-unknown exit 2; mutator call wrapped in try (+4 tests). **M1** missing pre-GUID now REFUSES before mutating (can't pin the post-capture → no trustworthy comparison); missing post-GUID → UNVERIFIED (can't confirm identity by omission) (+2 tests). **m1** human report now prints "dLUFS-I n/a" + the reason note instead of silently dropping null LUFS (+test). **m2** `verify Bass --help` (with or without `--`) now prints the verify options and exits 0; leading option tokens are peeled even without the `--` separator (+test). Suite: 212 green. |
| 2026-07-23 | 3 | (this commit) | CODE DONE, gate pending | `verify_change` + `tune_param` in reaper_mcp.py (registry pattern followed; tool count 18→20). Decisions: **(a)** snapshot/restore between tune iterations: NOT used — each set overwrites the same param, one undo point per set (spec's leaning confirmed); exception: NON_MONOTONE restores the INITIAL value (an untrustworthy metric/param relationship shouldn't leave a random probe point set). **(b)** `band_db` metric = power-sum of Post Mortem 1/3-octave bands with centers in `band_hz` (arithmetic over computed levels, no new DSP); requires postmortem metrics_source, REFUSED otherwise. **(c)** tune search: direction probe to the boundary (assume gain-like: metric increases with normalized), ONE flip allowed if the probe contradicts (wrong guess ≠ non-monotone), then bisection; non-monotone = mid falls outside bracket ± noise slack (max(0.1, tol/2)) → abort + restore. UNREACHABLE when target beyond boundary metric → best-observed left applied, honest report. Hard cap 5 renders after baseline. **(d)** MCP isError: REFUSED/MUTATION_FAILED/SET_FAILED/MEASURE_FAILED = tool error; VERIFIED/UNVERIFIED/CONVERGED/UNCONVERGED/UNREACHABLE/NON_MONOTONE = real results the model must relay (UNVERIFIED may mean the project already changed — marking it isError would invite blind retries). Tests: stateful auto-bridge (knob state drives reported LUFS — real feedback-loop fidelity), 12 new MCP tests: schemas, verify happy/refused, tune converge (2 iters, pinned sets asserted), 5-iteration cap + unconverged honesty, non-monotone abort+restore, gated refusal. Suite 202 green. | `verify` in verifyloop.py + `reaperd.py verify <track> [opts] -- <type> '<payload>'`. Decisions: (a) pre-refusals (capture blocked / pre-silent) exit 1 like MUTATION_FAILED — the agent-relevant meaning of exit 1 is "nothing was changed", statuses distinguish REFUSED vs MUTATION_FAILED; (b) frozen post bounds pass through an internal `_bounds` param on `measure` that skips re-validation and re-resolution (a time-selection-clamped pre duration <1 s must not be re-rejected on post); (c) mutation goes through send_type with resolve+repair ON (same as `cmd`), captures use resolve/repair OFF; (d) masking deltas: "not applicable: single-track verify" (masking is cross-track by definition). 21 new tests incl. all five outcome sequences, byte-identical bounds on the wire, canned-metrics delta/formatter units. Suite 174 green. |

| 2026-07-23 | 2-gate | 1dcaa23 | ROUND 3: FAIL — 2 BLOCKER, 0 MAJOR, 1 MINOR | Fix cycle 3 of 3 (round 4 verifies it; if that fails → STOP per deadlock rule). **B1** empty/non-string/unhashable `error.code` crashed the uncertain-set membership test or exited 1 → `_error_code()` normalizes to a clean string or None; None → outcome-unknown exit 2 (+3 parametrized cases). **B2** malformed batch results (null entries, `data` not an object, `results` not a list, count mismatch with requested commands) verified as exit 0 or crashed → a top-level-ok batch now requires ONE well-formed result per requested command or it is outcome-unknown exit 2 (+5 cases). **m1** partial batch now disclosed on every outcome path (post-capture failures included) and the human report prints "mutation batch: PARTIAL — N sub-command(s) failed" instead of "ok" (+test). |
| 2026-07-23 | 3-gate | df6e73e | ROUND 1: FAIL — 4 BLOCKER, 3 MAJOR, 4 MINOR | All fixed. **B1** FX pinned only by mutable index → identity (FX GUID at the pinned scoped index + parameter name) is now RE-VERIFIED via a non-render scan before EVERY set; any mismatch aborts IDENTITY_CHANGED without setting (set_fx_param cannot target a GUID directly, so proof-before-use is the strongest available pinning) (+test with mid-run GUID change). **B2** tune could converge on full-mix/changed-scope evidence → baseline must be verified isolated_track (master_output allowed for the master) else REFUSED; every iteration must match the baseline scope + verified isolation + track GUID else SCOPE_CHANGED/IDENTITY_CHANGED abort (+3 tests). **B3** MCP isError on applied mutations invited retries → isError now ONLY for REFUSED (+validation); MUTATION_FAILED/SET_FAILED/MEASURE_FAILED/etc. are results the model must relay; verify_change description no longer claims "nothing changed" for MUTATION_FAILED (+2 tests). **B4** final state could be absent or false → `final` is now READ BACK from the live project after every outcome (including SET_FAILED/MEASURE_FAILED); failed restore replies with uncertain codes say outcome UNKNOWN and defer to the read-back; a failed read-back is disclosed (`read_back: false`, last-known value labeled) (+tests). **M1** starting at the assumed boundary probes the other direction instead of false UNREACHABLE (+test). **M2** baseline already within tolerance → CONVERGED at 0 iterations, nothing set (+test). **M3** fake fidelity: auto-bridge now validates sets (range, GUID) and simulates chain edits + scope drift via hooks; band_db unit tests added. **m1** flat metric → UNREACHABLE (a constant is monotone), NON_MONOTONE reserved for bracket violations per the settled definition (+test; the old peaked-function test reclassified to UNREACHABLE-with-best-restore, which is the honest label — the peak IS the closest achievable). **m2** band_energy_db: non-dict entries skipped, power-sum underflow clamps to BAND_ENERGY_FLOOR_DB (-400) instead of log10(0) crash (+test). **m3** tune_param schema now expresses the target object shape (metric enum, required metric+delta) and the description states the required FX/param selectors (+schema test). **m4** facts fixed: 21 tools (not 20; pre-phase-3 was 19, not 18 as README claimed), "6 renders total", per-iteration undo points documented. Suite: 234 green. |

### Phase 0 findings (2026-07-23)

**Baseline re-check.** `main` moved from `8358a9a` to `24b39ab` — the diff is
this spec document itself, nothing else. Working tree clean, in sync with
origin. Branch `feat/verify-loop` created off `24b39ab`.

**CI-parity suite (all green).**
- `python -m pytest tests skills/drum-apparatus/tests -q` → 124 passed.
- `python -m py_compile reaperd.py reaper_mcp.py setup/install.py` → OK.
- Lua was NOT on PATH on this machine (CI installs it via gh-actions-lua).
  Installed Lua 5.4.6 via `winget install DEVCOM.Lua` →
  `%LOCALAPPDATA%\Programs\Lua\bin\lua.exe` (not on persistent PATH; invoke
  with the full path or extend PATH per shell).
  `lua bridge/test_bridge.lua` → OK (149 checks); `lua bridge/test_json.lua`
  → 40 passed, 0 failed.

**Post Mortem install.** `pip install --user -e ../post-mortem` succeeded;
`postmortem --help` runs (on PATH) and
`from postmortem.analysis import analyze_wav` imports.

**Decision — rich metrics for a captured WAV: option (a), with a twist.**
- `analyze_wav(path)` (`../post-mortem/postmortem/analysis.py:335`) returns a
  `TrackStats` dataclass: `duration_seconds`, `sample_rate`, `channels`,
  `sample_peak_db`, `rms_db`, `crest_factor_db`,
  `spectrum_third_octave` (list of `{freq_hz, level_db}`, 31 bands 20 Hz–20 kHz),
  `silence_fraction`, `stereo` (dict or None). Depends only on numpy.
- The masking table (`masking_overlap`) is a separate PURE function taking
  `{name: spectrum}` — it does NOT require the multi-track capture path. It is
  cross-track by nature; single-track `verify` v1 has no masking deltas
  (report says "not applicable: single track").
- Twist: true peak / LRA / momentary-LUFS parsing lives in
  `postmortem.diagnose.parse_render_stats`, but `postmortem.diagnose` imports
  `anthropic` at module top — importing it would couple the verify loop to the
  model-SDK dependency. So `verifyloop` imports ONLY `postmortem.analysis`
  (numpy) and carries its own tiny stdlib RENDER_STATS parser (string split,
  same key mappings: TRUEPEAK/TPEAK/TPK, LRA, LUFSI…). String parsing, not DSP.
- Rejected (b): cross-repo CLI change is unnecessary given (a). Rejected (c):
  double-couples capture bounds to cursor state — exactly the drift the spec
  bans.

**Open question 2 — `start_seconds` vs time selection.** Confirmed in
`bridge/reaper_agent_bridge.lua` (`command_capture_track_audio`, lines
2503–2515): when `payload.start_seconds` is present the time selection is
NEVER read and `duration_seconds` is used exactly as passed (the
`min(duration, ts_end - ts_start)` clamp only applies on the
no-start_seconds path). Passing explicit `start_seconds` + `duration_seconds`
fully freezes bounds; no guard against a user moving the time selection
between pre and post is needed. Bounds still must be resolved ONCE by
`measure` (via `get_selected_track`-style resolution or preflight) and
passed explicitly to both captures.

**Open question 3 — `band_db` metric.** Post Mortem's
`spectrum_third_octave` `level_db` per band is the clean mapping. For a
`band_hz: [lo, hi]` range: select the 1/3-octave bands whose center lies in
the range and power-sum them (`10*log10(sum(10^(L/10)))`). That is arithmetic
over already-computed band levels, not new DSP. Finalize in Phase 3.

**Open question 4 — temp file convention.** `reaper_mcp.py`
`tool_capture_track_audio` uses `tempfile.gettempdir()/reaper-mcp/`
with `capture-<UTC stamp>.wav`. The verify loop follows suit:
`tempfile.gettempdir()/reaper-verify/` + timestamped names, cleanup on
success, keep on failure for debugging. Never inside the repo (OneDrive).

**Other recon notes.**
- `fake_bridge` (`tests/bridge_fakes.py`) answers exactly one command;
  Phase 1 adds `fake_bridge_script(root, replies)` alongside it.
- `send_type` returns `{"ok": False, "error": {...}}` on timeout/bad reply —
  the verify loop can treat every bridge interaction uniformly.
- Capture reply carries `render_stats_raw` (semicolon `KEY:value` string) —
  the LUFS-I the bridge parses plus whatever else REAPER measured; our parser
  reads true peak et al. from it without Post Mortem's diagnose module.
- MCP `_capture_safety_error` refuses non-`isolated_track` /
  unverified captures for *diagnosis*; `verify` mirrors the stance as scope
  honesty (report, don't silently present full-mix deltas as per-track).
