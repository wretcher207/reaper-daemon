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
  `allow_risk_level_3: true` in `bridge/bridge_config.json` (read once at
  startup — changing it requires a REAPER restart). Do NOT assume REAPER is
  available; phases 1–3 must be fully testable against the fake bridge.

## Repo orientation (verified facts, with anchors)

Read `AGENTS.md` and `bridge/command_schema.md` in full before coding. Key
primitives this project builds on:

| Primitive | Where | What it gives you |
|---|---|---|
| `capture_track_audio` | `bridge/reaper_agent_bridge.lua` (`command_capture_track_audio`, ~line 2485); schema in `bridge/command_schema.md` | Renders one track to WAV. Gated on `allow_risk_level_3`. Returns `file_path` (authoritative, from `RENDER_TARGETS`), `render_loudness_lufs` (LUFS-I parsed from `RENDER_STATS`, ~line 2631), `capture_scope` (`isolated_track` \| `full_mix` \| `master_output`), `isolation_verified`. Restores selection + all render settings even on error. Bounds: active time selection if any, else cursor + `duration_seconds`; `start_seconds` overrides. |
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
   blocker list if `capture_allowed` is false. Surface `requires_restart_to_change`
   verbatim when the risk gate is the blocker — users always trip on this.
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
   - `0` VERIFIED — both captures clean, deltas reported.
   - `1` MUTATION_FAILED — mutation error, no post capture attempted.
   - `2` UNVERIFIED — mutation applied but post-capture failed or was silent.
     **The mutation is NOT rolled back** (it's one Ctrl/Cmd+Z away — say so
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
| 2026-07-23 | 0 | (this commit) | DONE | See "Phase 0 findings" below. Environment: remote Linux session, not David's Windows machine. Branch is `claude/status-last-pushed-h4l7un` (session-mandated), not `feat/verify-loop` — same role, one PR at the end. Codex CLI unavailable here; per David, the per-phase gate is an independent adversarial review by fresh agent sessions hunting the template's seven failure categories (David can re-run real Codex on his machine before merge). |

### Phase 0 findings (2026-07-23)

**Baseline re-verified in this environment:** `main` @ `24b39ab` (spec commit on
top of `8358a9a`; no code drift). Full CI parity green: 124 pytest, `py_compile`
OK on all three entry points, `lua bridge/test_bridge.lua` OK (149 checks),
`lua bridge/test_json.lua` 40 passed (must run from repo root, not `bridge/`).
Post Mortem installed editable from a fresh clone; `postmortem --help` and
`from postmortem.analysis import analyze_wav` both work.

**Open question 1 — `analyze_wav` shape → decision (a) CONFIRMED.**
`analyze_wav(path)` (`postmortem/analysis.py:335`) is a pure function over an
existing WAV: returns a `TrackStats` dataclass with `duration_seconds`,
`sample_rate`, `channels`, `sample_peak_db`, `rms_db`, `crest_factor_db`,
`spectrum_third_octave` (list of `{freq_hz, level_db}`, 31 ISO bands),
`silence_fraction`, and `stereo` (correlation/mid-side/balance, None for mono).
Notes that shape the report format: it does NOT compute LUFS (comes from
RENDER_STATS — which `capture_track_audio` already returns) and has no true
peak, only sample peak — `verify` must label it `sample_peak_db`, never claim
true peak. Masking (`masking_overlap`) is a separate pure function over
multiple tracks' spectra — not applicable to single-track pre/post verify;
per-band spectrum deltas cover the same ground. So: import
`postmortem.analysis` guarded in try/except (it needs numpy; this repo stays
stdlib-only — Post Mortem is an optional external), do our own captures via
`capture_track_audio`, analyze the WAVs directly. No cross-repo change needed.

**Open question 2 — bounds freezing CONFIRMED safe.** In
`command_capture_track_audio` (Lua ~2504–2515): when `start_seconds` is
present, the time-selection branch is never consulted and `duration_seconds`
is used as-is (the `min(duration, ts_end - ts_start)` clamp lives only in the
no-`start_seconds` branch). Therefore `verify` freezes bounds by ALWAYS
passing explicit `start_seconds` + `duration_seconds` on both captures; the
user moving the time selection or cursor between pre and post cannot shift
them. `measure` resolves initial bounds client-side from `get_context`
(`cursor.seconds`, `time_selection.start/end/active`), mirroring the bridge's
own resolution order.

**Open question 3 — `band_db` metric (Phase 3).** Maps to
`spectrum_third_octave`: select the 1/3-octave bands whose center `freq_hz`
lies within the requested range and power-average their `level_db`
(`10*log10(mean(10^(db/10)))`). Post Mortem computes the bands; no new DSP.

**Open question 4 — temp file convention.** `reaper_mcp.py` already uses
`tempfile.gettempdir()/reaper-mcp/capture-<UTC stamp>.wav` for captures;
`reaperd.py` uses `tempfile.NamedTemporaryFile` for scratch MIDI. Convention
adopted: capture WAVs go to `tempfile.gettempdir()/reaper-verify/` with
timestamped names, deleted on success, kept on failure for debugging.

**Module placement decision:** new file `verifyloop.py` at repo root
(measure + verify + formatters will clearly exceed the ~200-line threshold the
spec set for inlining into `reaperd.py`). `reaperd.py` gains thin `measure` /
`verify` subcommands delegating to it; all transport through `send_type`.

**Silence thresholds** copied from `reaper_mcp.py::_run_postmortem`:
silent when `silence_fraction >= 0.85` or `rms_db <= -60`. Without Post
Mortem (no numpy), RMS/silence-fraction are unavailable; the fallback silence
signal is `render_loudness_lufs` missing or absurdly low — the report must
carry `metrics_source: "render_stats"` and say what it could not check.
