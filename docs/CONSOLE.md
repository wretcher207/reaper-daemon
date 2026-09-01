# Daemon Console

A chat box docked inside REAPER. You type, a headless Claude Code session
answers through the same bridge every other surface uses, and the answer
arrives without an alt-tab, without retyping what track is selected, and
without leaving the transport.

The console is three processes and a folder of files:

```
REAPER (UI thread)                    this machine                (paid session)
┌────────────────────────┐   files    ┌────────────────────┐  NDJSON  ┌────────────┐
│ reaper_daemon_console  │ ◄────────► │ console_sidecar.py │ ◄──────► │ claude.exe │
│ .lua  (ReaImGui panel) │  console/  │ (broker, stdlib)   │  stdio   │ + MCP:     │
└────────────────────────┘            └────────────────────┘          │ reaper_mcp │
        ▲                                                             └─────┬──────┘
        │ ordinary REAPER API calls (audition strip)                        │
        ▼                                                                   ▼
   the live project ◄──────────────── inbox/ outbox/ ◄──── the file bridge ─┘
```

- **The panel** (`bridge/reaper_daemon_console.lua`) owns no logic about what
  Claude should do. It moves prompt/control files out, tails the transcript in,
  and draws status. One exception: the audition strip (Locate, A/B, Loop, Undo)
  runs as ordinary REAPER API calls in-process, because the panel IS inside
  REAPER — no bridge round trip, no render, no paid turn.
- **The sidecar** (`console_sidecar.py`, stdlib only) owns one long-lived
  headless Claude Code session with `reaper_mcp.py` wired in as its MCP server,
  and enforces the money and liveness rules the CLI does not.
- **The bridge** is the same `inbox/`/`outbox/` file protocol everything else
  uses. The console adds no new command surface to it.

## Trust statement, in plain words

The console session runs with **full authority and no approval prompt**
(`--permission-mode bypassPermissions`). A misread prompt can delete a track,
insert MIDI, or start a render. That was a deliberate decision: in-panel
approval modals were rejected as friction that kills the point of the tool.

What protects you, and its exact limits:

- Every bridge mutation runs in a REAPER undo block. **One Ctrl+Z reverts it.**
- Undo does **not** cover files written to disk: rendered WAVs, inserted `.mid`
  files, overwritten media. The operating contract
  ([console_contract.md](console_contract.md)) requires the model to announce
  destruction in one sentence first, but the enforcement is the contract, not a
  prompt.
- The bridge has no project-save command. Writing your `.rpp` is your Ctrl+S,
  always (`save_project` exists on the file bridge behind the risk gate, but
  the console contract does not use it).
- Console captures are clamped to 8 seconds (see freeze clamp below), so a
  measurement request cannot freeze REAPER for minutes.

Also know: the Claude Code CLI writes its own transcript to
`~/.claude/projects/<slug>/<session>.jsonl` — outside this repo and outside
this repo's `.gitignore`. `console/` (transcripts, raw event logs) is
gitignored here, but the CLI's copy is not ours to manage.

## Setup

1. Install the bridge normally (`install.sh` / `setup/install.py`) and confirm
   `python reaperd.py status` says CONNECTED with REAPER running.
2. Install **ReaImGui** (via ReaPack). The panel is written against 0.10.x /
   Dear ImGui 1.92.
3. Register the panel as an action, once: REAPER menu **Actions → Show action
   list → New action → Load ReaScript**, pick
   `bridge/reaper_daemon_console.lua`. Bind a key or add a toolbar button if
   you like. Run it; dock it where you want it.
4. The panel **starts the sidecar itself** when it isn't running. No terminal
   needed. (You can also run `python console_sidecar.py` by hand, or
   `python console_sidecar.py --once "<prompt>"` for one panel-less turn.)
5. Optional: copy `console_config.example.json` to `console_config.json` in
   the repo root and edit. Every key is optional; defaults are baked into the
   sidecar. The config is read from the file, never argv, so nothing lands in
   a process listing.

Requirements beyond the bridge: `claude` (Claude Code CLI) on PATH or pointed
at by `claude_path`, and ReaImGui. Nothing else — the sidecar is stdlib-only.

## The file protocol

All paths relative to the repo root. Atomic replace (`.tmp` + rename, with a
bounded PermissionError retry for Windows readers):

| file | writer → reader | what it is |
| --- | --- | --- |
| `console/state.json` | sidecar → panel | status, cost, queue, `last_change` |
| `console/panel.json` | panel → sidecar | heartbeat; the dead-man input |
| `console/prompts/<stamp>-<hex>.json` | panel → sidecar | one prompt + focus envelope |
| `console/control/<stamp>-<hex>.json` | panel → sidecar | interrupt, ack_warn, restart |
| `console/sidecar.lock` | sidecar only | singleton lock |
| `console/cost.json` | sidecar only | daily spend, survives restarts |

Append-only, never renamed, one complete line per write:

| file | what it is |
| --- | --- |
| `console/events/<session>.jsonl` | normalized events the panel renders (tailed by byte offset) |
| `console/raw/<session>.jsonl` | every child stdout line verbatim (redacted) |
| `logs/console_sidecar.log` | human-readable sidecar log |
| `logs/console_panel.log` | human-readable panel log |

Ids are timestamp-prefixed so a sorted listing is chronological — same lesson
the bridge learned.

**The focus envelope.** Every prompt carries the live selection: selected
track (name + GUID), edit cursor bar, time selection in bars, tempo,
transport state, project name, dirty flag (`bridge/reaper_focus.lua`). "The
drums" with one track selected means the selected track; the model is told to
use the envelope instead of re-fetching context.

## Money rules

Model: **Opus, `--effort medium`, hermetic** (`--setting-sources ''
--strict-mcp-config --disable-slash-commands` — without all three the child
loads 129 tools, 7 remote connectors, and 13 global hook events that would
fire PowerShell inside a DAW).

Measured cost, real numbers (2026-08-09, claude 2.1.225): a cold turn
**$0.56** (53k cache-creation tokens at 1h TTL); a brand-new session inside
the 1h cache window **$0.07**; a warm turn **$0.028**. The expensive event is
the cold start, not the conversation — an afternoon of steady use runs $2–4,
plus ~$0.53 whenever the prefix falls out of cache.

Enforcement, all sidecar-side because the CLI's `--max-budget-usd` is
advisory and latching (it checks pre-turn, overshoots by one turn, then
answers every later turn with an 8 ms error):

- `daily_budget_usd` (default $10) — the real hard stop, persisted in
  `console/cost.json` across restarts.
- `turn_warn_usd` (default $0.25) — a turn costing more than this **blocks
  the next Send** until you acknowledge in the panel. There is no cheap
  mid-turn ceiling, so the gate is pre-flight on the following turn.
- `session_budget_usd` — optional per-process cap.
- Per-turn cost is the *difference* between consecutive cumulative
  `total_cost_usd` results; the panel shows it per turn and per day.

**Explicit non-goal: there is no cache keepalive timer, and there will not be
one.** Pre-warming a 25k-token prefix every 5 minutes for an 8-hour day costs
about $9 for zero work. An idle session costs nothing; one cold turn after
lunch costs pennies. The panel shows "cache cold" and we eat it.

## Liveness rules

- **REAPER freezes itself during captures** — a synchronous render blocks the
  UI thread, the panel misses defer ticks, and `panel.json` goes quiet.
  Measured: a 9.28 s block cost ~295 missed ticks and a 9.66 s heartbeat gap.
  Panel silence is therefore NOT evidence of death.
- The **dead-man switch** is a compound predicate: kill the child only when
  `panel.json` is stale `deadman_seconds` (default 300 s, deliberately above
  the 180 s capture timeout) AND the bridge is not mid-render AND no turn is
  in flight. A missing heartbeat file reads as "unknown", not "dead". If the
  panel dies, the paid session is killed rather than left running.
- `turn_timeout_seconds` (default 900 s): a turn with no result is
  interrupted, then the child is killed and resumed — without this a hung
  turn pins `turn_in_flight` and the dead-man can never fire.

**The freeze clamp.** The sidecar sets `REAPER_DAEMON_CONSOLE_MODE=1` on the
MCP child, and console captures clamp to 8 seconds across
`capture_track_audio`, `verify_change`, `tune_param`, `analyze_track`, and
`compare_tracks` (override:
`REAPER_DAEMON_CONSOLE_MAX_CAPTURE_SECONDS`). The clamp is announced to the
model as `console_note` *inside the JSON body* — the panel decides a result
is structured by its first character, so a prose preamble would make every
measurement opaque.

## The audition strip

After a mutating tool call, `state.json` carries `last_change` (tool, a
producer-readable summary, track, FX, bar range) and the panel draws a strip:
**Locate · A/B · Loop · Undo**, then verdict buttons — `too much` /
`not enough` / `wrong direction` / `try another` / `keep it` — each of which
sends a follow-up prompt with no typing.

Rules baked in (do not regress them):

- A tool call is parked until its result lands. A refused mutation never arms
  Undo. UNVERIFIED *does* promote, and the strip prints `unverified`.
- A/B is an FX-enable toggle, never undo/redo — undo/redo A/B lies the
  instant you edit between presses. No FX to toggle means the reason is
  printed where the button would be.
- Bar provenance travels: `bars 17-24` from the tool and `bars 17-24 (your
  selection)` from the focus envelope are different claims.
- The focus on a change is the one that came WITH the prompt, not the live one.
- Read-only tools and `dry_run` calls never become the last change.

## Config reference

See `console_config.example.json` — it is the reference, every key commented.
Highlights: `claude_path` (a `.cmd`/`.bat` shim is refused — killing a shim
leaves node alive holding a paid session), `model`/`effort`, the three budget
keys above, `deadman_seconds`, `turn_timeout_seconds`, `event_line_cap`
(bytes per normalized event line; full text always lands in `console/raw/`),
`retention_days`/`retention_bytes`, and `quick_actions` (label + prompt pairs
rendered as panel buttons).

## Operating contract

The system-prompt rules the session runs under are
[console_contract.md](console_contract.md): three-sentence answers, use the
focus envelope, announce freezes before measuring, never claim an unmeasured
improvement, relay verify statuses verbatim, refuse to profile a dirty
project, and stop when the bridge is stale instead of paying for retries.

## Not shipped via ReaPack

ReaPack delivers Lua only. The panel is useless without the Python sidecar
and carries an undeclared ReaImGui dependency, so shipping it there hands
strangers a broken window. The console is **clone-only**: it ships in the
cloned repo, and the ReaPack package remains bridge-only.
