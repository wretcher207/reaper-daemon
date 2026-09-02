# Reaper Daemon

A local file bridge for controlling REAPER from an AI agent — Claude Code,
Codex, Cursor's terminal agent, anything that can read and write files. No
network, no socket required: the agent drops JSON command files in a folder, a
Lua script inside REAPER executes them and writes JSON results back. An
**optional MCP server** (`reaper_mcp.py`, stdio, same trust model) exposes the
whole bridge as tools for Claude Desktop, Claude Code, and any other MCP
client — see [MCP server](#mcp-server--talk-to-reaper-in-plain-english) below.

Works on **macOS, Windows, and Linux**. The bridge itself is pure Lua using
native REAPER API only (path separators are derived from `package.config`, so
the same script runs everywhere). The agent-facing CLI (`reaperd.py`) and the
MCP server (`reaper_mcp.py`) are plain Python 3 files with no third-party
dependencies.

The bridge is **plugin-agnostic and drum-library-agnostic**. It ships with no
knowledge of any specific synth, amp sim, or instrument. An agent discovers
what a project contains with `scan_fx`, acts on tracks/FX/parameters by name,
and can **auto-discover** a drum kit's note map from the library's own
`.midnam`.

## What it does

- **Project control** — transport, tempo, cursor, time selection, render, save.
- **Tracks** — add, delete, rename, select, volume, pan, mute, solo, arm, color.
- **Routing** — read a track's sends, receives and parent bus; create a send
  (`create_send`) and switch a track's direct feed to the master on or off
  (`set_master_send`).
- **FX** — add, remove, bypass, reorder, set parameters, write parameter
  automation envelopes. Any plugin (VST2/3, CLAP, AU, JS).
- **Markers, regions, media items.**
- **MIDI** — insert MIDI files; a creative drum DSL engine with humanization
  (velocity model, fatigue, timing jitter).
- **Guitar and bass** — `shred` renders a humanized riff (keyswitched palm
  mutes, power chords, legato, ties and slides, chord tokens, lead scale
  tones) onto a track; `band` cuts two guitars, bass and drums in one command.
- **Humanize** — `humanize` puts a dynamic contour and micro-timing into a
  drum take that already exists, and `--follow-lead` learns the hand from the
  bars you humanized yourself.
- **Discovery** — `scan_fx` dumps every FX and parameter; `discover_drum_map`
  reads a drum track's note names and builds a kit map for any library.

Every mutating command runs inside a REAPER undo block (Cmd+Z / Ctrl+Z to revert).

## How it works

A single runtime: the **Lua bridge** (`bridge/reaper_agent_bridge.lua`) runs
forever as a `reaper.defer` loop inside REAPER. It polls `inbox/`, executes one
command per tick, writes results to `outbox/`, and a heartbeat to
`bridge/heartbeat.json`. This is the only thing that touches the REAPER API.

All JSON writes are atomic (write `.tmp`, then rename). Command files move
`inbox/` → `processing/` → `archive/` (ok) or `failed/` (error).

## Install (cross-platform, one command)

Requires REAPER and Python 3.8+ (no pip packages needed).

```bash
git clone https://github.com/wretcher207/reaper-daemon.git
cd reaper-daemon
python3 setup/install.py        # macOS/Linux  (use `python` on Windows)
```

`setup/install.py` detects your OS, locates REAPER's per-user resource
directory, and writes a marker-delimited block into `Scripts/__startup.lua`
that auto-loads the bridge on every launch, pointing at this clone. It is
idempotent — re-run it any time you move the repo. Then **(re)start REAPER**.

Options:

```bash
python3 setup/install.py --dry-run     # preview, change nothing
python3 setup/install.py --uninstall   # remove the managed auto-start block
python3 setup/install.py --bridge-root /path/to/clone
REAPER_RESOURCE_PATH=/custom/dir python3 setup/install.py
```

Prefer to load it manually instead of at startup? In REAPER:
`Actions > Show action list > ReaScript: Load…`, pick
`bridge/reaper_agent_bridge.lua`, run it once. It runs as a background deferred
script and regenerates `bridge/bridge_config.json` and its working folders on
first run.

## Install via ReaPack (REAPER-native, alternative)

Prefer REAPER's own package manager? Add this repo to ReaPack:

1. In REAPER: `Extensions > ReaPack > Import repositories`.
2. Paste: `https://github.com/wretcher207/reaper-daemon/raw/main/index.xml`
3. `Extensions > ReaPack > Browse packages`, find **Reaper Daemon**, install.

Two things to know, because ReaPack delivers only the two Lua files
(`reaper_agent_bridge.lua` + `json.lua`) — not the agent CLI, the command
examples, or the drum engine:

- **It does not auto-start.** ReaPack installs the bridge as an Action but does
  not run it on launch. Run the action once per session, or add it to your
  `Scripts/__startup.lua` (`python3 setup/install.py` from a clone does this
  for you).
- **You still need the rest of the package.** `reaperd.py` (the agent CLI),
  `commands/examples/`, and `skills/drum-apparatus/` (the drum DSL engine) are
  NOT installed by ReaPack. For those, also clone the repo:
  `git clone https://github.com/wretcher207/reaper-daemon.git` and point your
  agent at the clone (or run `python3 setup/install.py` from the clone, which
  handles auto-start too). ReaPack then just keeps the bridge script updated.
- **Point your agent at the install folder.** ReaPack installs to
  `<REAPER resource>/Scripts/reaper-daemon/`. Right-click the package in
  ReaPack and "Show in explorer/finder" to get the exact path, then aim your
  agent at that folder.

## Verify it

With REAPER open and a project loaded:

```bash
python3 reaperd.py status
python3 reaperd.py send commands/examples/get_context.json --wait
```

`status` reports the bridge heartbeat; `send --wait` prints a JSON result
describing the open project. If it times out, check that
`bridge/heartbeat.json` exists and is fresh.

## If a capture comes back silent or quiet

REAPER has a preference, **Preferences → Audio → "set media items offline when
application is not active"**, that unloads disk media whenever REAPER is not
the active application. It exists to save RAM and for normal hand-driven use it
is invisible.

It is not invisible to a daemon. Every command here arrives from another
process — the CLI, the MCP server, the console sidecar — so REAPER is *always*
in the background when it runs one. With that preference on, reads see unloaded
media and a capture can measure sources that are not loaded. Virtual
instruments are unaffected, because there is no file to unload, which is what
makes the symptom look like a plugin bug rather than a preference.

Check it and clear it:

```bash
python3 reaperd.py cmd get_context '{"include_fx":false}'
python3 reaperd.py cmd get_items '{"target_track_name":"Gtr L"}'
python3 reaperd.py cmd set_media_offline_when_inactive '{"enabled":false}'
python3 reaperd.py cmd set_all_media_online '{}'
```

`get_context` reports `media_offline_when_inactive`. `get_items` reports a
`source_state` per item: `loaded`, `offline` (REAPER is not loading a file that
is right there), `unresolved` (the file genuinely cannot be read — a different
problem with a different fix), or `midi`.

Two things worth knowing. `source_state` is **inferred**, not read — REAPER
exposes no ReaScript call for media-item offline state, so the bridge infers it
from a source reporting zero length and zero sample rate while its file still
opens, and ships `source_state_inferred: true` so nothing mistakes it for an
API read. And once media has been unloaded the state is **sticky**: it does not
come back from restoring or focusing the window, from starting playback, or
from turning the preference back off. `set_all_media_online` is what clears it.

`get_capture_preflight` warns when the preference is on, so a measurement never
quietly reports a number taken against unloaded media.

## The agent CLI — `reaperd.py`

One Python entry point for everything an agent does (no shell helpers, no
`jq`/`grep` pipelines — works identically on macOS, Windows, Linux):

```bash
python3 reaperd.py status                       # liveness check (run first)
python3 reaperd.py send <cmd.json> --wait       # send a command file
python3 reaperd.py cmd <type> '<payload-json>'  # send by type + payload
python3 reaperd.py fxload "<plugin query>" <track|master>
python3 reaperd.py setparam <track> "<fx>" "<param>" "<display value>"
python3 reaperd.py eq <track> "<fx>" <band> <freqHz> <gaindB> [Q]
python3 reaperd.py measure <track> [--seconds N] [--start S] [--json]
python3 reaperd.py verify <track> [--seconds N] [--json] -- <type> '<payload-json>'
python3 reaperd.py profile <project.rpp> <track> [--start-bar N] [--bars N] [--max-seconds S]
python3 reaperd.py groove <beat.dsl> --track Drums [--position SEC] [--map NAME]
python3 reaperd.py jam                          # DSL drum beat from stdin -> selected track
python3 reaperd.py humanize --track Drums [--amount 0-100] [--follow-lead] [--dry-run]
python3 reaperd.py shred --track argent-l [--part guitar|bass] [--bars-file riff.txt] [--seed N]
python3 reaperd.py band                         # 2 guitars + bass + drums, one command
python3 reaperd.py list-maps                    # available drum-kit maps
python3 reaperd.py discover-map <track> [--save <name>]
python3 reaperd.py add-map <name> --file <map.json>   # or --roles '{...}' / stdin
python3 reaperd.py remove-map <name>
```

`fxload` and `cmd add_fx` resolve a fuzzy plugin query to REAPER's exact
installed name from the VST/CLAP/AU cache before loading. `setparam` works on
any plugin by parameter index, binary-searching the normalized value that
produces a target display value, then verifying.

`measure` captures one track once (behind the same `allow_audio_writes` gate
as `capture_track_audio`) and prints measured audio metrics: LUFS-I whenever
REAPER reports it (digital silence reads as null and is flagged), plus — when
[Post Mortem](https://github.com/wretcher207/post-mortem) is installed —
sample peak, RMS, crest factor, 1/3-octave spectrum, stereo image, and a
silence check. Bounds are resolved once (time selection start if active, else
edit cursor; override with `--start`) and passed explicitly; to compare two
separate runs of the same spot, pass the same `--start`/`--seconds` to both.
The output labels its `metrics_source` (`postmortem` or `render_stats`) and
its capture scope — full-mix fallbacks are reported honestly, never presented
as per-track evidence.

## Closed-loop verify — mix moves with measured proof

Every mutating command returns `ok: true`, but `ok` only means "REAPER did
it" — not "the mix got better". `verify` closes that loop: it captures the
track, runs your command, captures the **same spot** again, and reports what
actually changed in the audio.

```bash
python3 reaperd.py verify Bass -- set_fx_param \
  '{"target_track_name":"Bass","fx_name_contains":"ReaEQ","param_name_contains":"Gain","formatted_value":"-2.5 dB"}'
```

```text
[verify] pre:  LUFS-I -14.1 | RMS -18.0 dBFS | scope isolated_track (verified)
[verify] mutation set_fx_param: ok
[verify] post: LUFS-I -14.9 | RMS -18.8 dBFS | scope isolated_track (verified)
[verify] dLUFS-I -0.80   dRMS -0.80 dB
[verify] biggest spectrum moves: 315 Hz -3.1 dB, 250 Hz -1.7 dB
[verify] VERDICT: VERIFIED
```

Exit codes are the contract (agents branch on them): `0` VERIFIED (both
captures clean and comparable, deltas reported), `1` REFUSED (the mutation
was never sent: the pre-capture was blocked or silent, so nothing was
mutated), `2` UNVERIFIED: the mutation was sent and the project **may have
changed** but the change could not be verified. Exit 2 covers: post-capture
failed or silent, a partially-applied `batch` (the bridge keeps sub-commands
that ran before the failure), a bridge rejection (the JSON carries the
rejection code; a handler that failed mid-edit can leave a partial change
inside one closed undo block, indistinguishable from a clean resolution
rejection from outside), a mutation whose reply timed out or could not be
read (it may have executed, or may execute later), and pre/post captures
that stopped being comparable (track identity or capture scope changed
mid-verify). Nothing is ever rolled back automatically (one Ctrl/Cmd+Z
reverts it), and an agent must NOT blindly retry on exit 2: the change may
already be live.

Honest limits, by design:

- Bounds are frozen once, before the mutation: pre and post captures use the
  byte-identical `start_seconds`/`duration_seconds`, so a moved cursor or
  time selection between captures cannot skew the comparison.
- With Post Mortem installed you get per-band spectrum deltas, true peak,
  RMS, and stereo-image deltas; without it, LUFS-I deltas only (the report
  labels its `metrics_source`).
- If either capture is not a verified isolated track (some tracks fall back
  to a full-mix render), the report says the deltas describe the capture
  scope — it never presents full-mix deltas as per-track evidence.
- A silent capture refuses a verdict rather than comparing dead air.
- Two renders per verify: each capture blocks REAPER's UI for the render
  duration (default 10 s of audio; keep `--seconds` short).

## MCP server — talk to REAPER in plain English

`reaper_mcp.py` wraps the same file bridge as an
[MCP](https://modelcontextprotocol.io) stdio server, so any MCP client can
drive REAPER conversationally. Zero dependencies, no network listener — it
translates tool calls into the same inbox/outbox files, with the same safety
semantics (undo blocks, `dry_run`, risk gating).

Claude Code:

```bash
claude mcp add reaper -- python3 /path/to/reaper-daemon/reaper_mcp.py
```

(Use `python` instead of `python3` on Windows if that's what is on PATH.)

Claude Desktop (`claude_desktop_config.json`):

```json
{ "mcpServers": { "reaper": {
    "command": "python",
    "args": ["C:/path/to/reaper-daemon/reaper_mcp.py"] } } }
```

Then just ask: *"add a ReaEQ to the bass and carve 2 dB at 300 Hz"*, *"what
plugins are on the master?"*, *"program a d-beat groove at bar 33"*.

29 tools: project/FX discovery (`get_context`, `scan_fx`,
`get_fx_parameters`, `get_track_routing`), transport, tracks, FX chains,
`set_fx_param` (formatted values like `"-16.00 dB"` work), independent
automation-envelope readback (`get_fx_param_automation`) and writes,
markers/regions, MIDI insertion, `batch` (one undo block),
post-FX stem capture, the drum-workflow trio (`profile_track` and
`riff_grid`, which analyze a guitar stem out of the SAVED `.rpp` on disk
rather than REAPER's live project, a caveat every payload carries, plus
`insert_groove`, which renders a drum DSL to MIDI and inserts it in one
call), the part-writing trio (`insert_riff` for one humanized guitar or
bass part, `cut_band` for the whole four-track jam with per-leg results,
`humanize_take` for dynamics and micro-timing on a take already on a
track, `--follow-lead` included), the closed-loop pair — `verify_change` (run one
mutation with measured pre/post proof, see
[Closed-loop verify](#closed-loop-verify--mix-moves-with-measured-proof))
and `tune_param` (iteratively search a parameter until a measured target
like "bass LUFS-I down 3 dB" is hit; a baseline render plus up to 5
iteration renders — 6 total — with honest converged/unconverged/
non-monotone reporting) — and, with
[Post Mortem](https://github.com/wretcher207/post-mortem) installed,
`analyze_track` / `compare_tracks`, which hand the calling model measured
mix data (LUFS, true peak, spectrum, stereo image, masking table) to
diagnose. Mutations are undoable with Ctrl/Cmd+Z (single commands and
batches are one undo step each; `tune_param` leaves one undo point per
iteration set); destructive tools ask the model to confirm intent; audio
capture stays behind the `allow_audio_writes` config gate (turn it on with
`python3 setup/install.py --allow-audio-writes`, then send `reload_bridge`;
saving the project and writing REAPER preferences are separate gates, so
measuring never requires granting those).

FX discovery returns REAPER's real track and FX GUIDs alongside names and
indices. Use the GUIDs as stable identity when planning a later change: names
can be duplicated, and indices move when a user edits the chain. The exact
identity fields and compatibility rules are documented in
[`bridge/command_schema.md`](bridge/command_schema.md#stable-discovery-identities).

Post Mortem Phase 1 consumes these read-only identities to validate structured
recommendations. Reaper Daemon does not preview or apply those recommendations;
its existing mutation commands remain explicit, independently authorized bridge
operations. Reaper Daemon stays MIT-licensed and local. Planned hosted Post
Mortem services or UI do not move this bridge behind a commercial boundary.

## Daemon Console — a chat box docked inside REAPER

The console puts the same agent *inside* REAPER: a ReaImGui panel you dock
next to the transport, backed by a long-lived headless Claude Code session
(`console_sidecar.py`, stdlib-only) with `reaper_mcp.py` wired in. Every
prompt carries a live focus envelope (selected track + GUID, cursor bar, time
selection, tempo, transport, dirty flag), so "measure the drums" with a track
selected just works. After a change, an audition strip offers Locate / A/B /
Loop / Undo and one-tap verdicts (`too much`, `not enough`, `keep it`) that
send the follow-up with no typing.

One-time setup: install [ReaImGui](https://github.com/cfillion/reaimgui)
via ReaPack, then **Actions → New action → Load ReaScript** and pick
`bridge/reaper_daemon_console.lua`. The panel starts the sidecar itself; the
`claude` CLI must be on PATH. The session runs with full authority and no
approval prompts — read the trust statement before first use.

Full architecture, file protocol, money and liveness rules, and config
reference: [`docs/CONSOLE.md`](docs/CONSOLE.md). The console is **clone-only**
(not in the ReaPack package): ReaPack delivers Lua only, and the panel is
useless without the sidecar.

## Drum kits — any library, auto-discovered

The DSL drum engine (`skills/drum-apparatus/`) ships a few built-in kit maps
(GM Standard, RS Monarch, Odeholm, MDL Tone, Sleep Token II) in
`skills/drum-apparatus/catalog/maps.json`. For **any other library**, auto-discover:

```bash
# 1. With the drum plugin on a track that has its .midnam loaded:
python3 reaperd.py discover-map Drums --save MyKit
# 2. Use it:
python3 reaperd.py groove beat.dsl --track Drums --map MyKit
#    or in the DSL:   @map MyKit
```

`discover-map` reads the MIDI note names REAPER has for the track (the
`.midnam` the library installed), classifies each note into a groovekit role
(kick / snare / hat-closed / hat-open / ride / crash / china / tom1..4 / ...),
fills missing articulations by fallback so a sparse kit never breaks the
engine, and saves the result to the user overlay
(`skills/drum-apparatus/maps/<name>.json`, gitignored). Libraries that don't
ship a `.midnam` (some Kontakt kits) report no note names; for those, build the
map by hand with `add-map`:

```bash
python3 reaperd.py add-map MyKontactKit --roles '{"KICK_R":36,"SNARE":38,"HH_OPEN_1":46,"CRASH_R":49,"CHINA_R":52}'
```

## Daemon Beater: profile a stem before you write drums

`profile` reads a guitar stem bar by bar and prints the numbers an agent
needs to plan drums: onset density, timing regularity, palm-mute vs ringing
decay, silence, low/bright band balance, and a 16th-note accent grid, plus
suggested section boundaries and repeat groups (A / B / A). Numbers, not
verdicts: the agent proposes section labels, you correct them.

```bash
python3 reaperd.py profile song.rpp guitar-di
python3 reaperd.py profile song.rpp guitar-di --start-bar 32 --bars 8
python3 reaperd.py profile song.rpp guitar-di --start-bar 32 --max-seconds 10
```

Point it at any bar and it analyzes just that window (plus
one bar of pre-roll and post-roll for context), never the whole stem. The
cut is hop-aligned, so timing, onset, grid, decay, and band numbers from a
window match a full pass; only the silence ratio is scored against a
window-local loudness reference. `--max-seconds` counts whole bars that fit
inside the cap, and refuses caps shorter than one bar instead of padding.

Prefer the DI track when there is one; distortion flattens the decay
contrast that separates open notes from palm mutes. Ships in the cloned
repo (`skills/drum-apparatus/`), not via ReaPack.

MCP clients get the same three steps without a shell: `profile_track`,
`riff_grid`, and `insert_groove`. The two analysis tools parse the `.rpp`
file on disk, so save the project before calling them. On an unsaved
project they describe stale material, which their payloads state outright.

## Guitar and bass — `shred` and `band`

`skills/guitar-apparatus/` is the guitar-side counterpart to the drum engine.
It renders humanized guitar and bass MIDI and inserts it through the same
bridge write path the drum groove uses (`insert_midi_file`).

```bash
# the whole four-track jam (2 guitars + bass + drums), replacing what was there
python3 reaperd.py band

# other tracks, a custom riff, a custom drum DSL, an explicit tempo
python3 reaperd.py band --guitar-l argent-l --guitar-r argent-r \
  --bass nolly-bass-library --drums rs-drums-monarch \
  --bars-file riff.txt --dsl beat.dsl --tempo 146

# one part at a time; --replace clears the track from --position first
python3 reaperd.py shred --track argent-l --bars-file riff.txt --seed 101 --replace
python3 reaperd.py shred --track nolly-bass-library --part bass --seed 303
```

A riff is a text file, **one bar per line, 16 steps** (spaces inside a bar are
visual only):

- `.` rest, `x` muted chug, `X` accented root, `o` let-ring root, `g` ghost.
- Connection cells: `_` ties the previous note through this step, `~` slides
  into the next one. These are what stop a riff sounding stabby. Chugs also
  carry under the palm for up to two steps instead of being cut short.
- Chord cells are **uppercase** note names read in the key of E (`E F G A B C
  D`), each firing Power Chord Sustain so the section moves harmonically.
- Lead scale tones are lowercase/digits (`s 3 a 5 6 7 h`) for single ringing
  notes over those chords. Case matters: `B` is a chord, `b` is a tritone.
- Palm-muted melody notes (`m n v f k`) walk between the low-E anchors so a
  riff reads as notes instead of a monotone rake, and the dissonant
  palm-muted ring clusters (`r 2 t 9 j`) strike the root with a clashing
  interval so the clash actually bites.

The full token table, with the reasoning behind each one, is in
`skills/guitar-apparatus/SKILL.md`.

Dynamics come from the drum humanizer's taste model: a deterministic musical
contour drives velocity across a four-bar phrase, RNG is only garnish, and the
no-repeat golden rule keeps consecutive chugs off one velocity. Double tracking
is two *performances*, not a copy — same riff, different `--seed` on the left
and right guitar.

Tuning and keyswitch maps live in `skills/guitar-apparatus/guitargen/maps.py`
and are confirmed against the instruments' own manuals: `argent_e` and
`argent_csharp` (Shreddage 3.5 Argent, 9-string) and `nolly_e` /
`nolly_csharp` (GGD Nolly bass). Another library is a new entry in that file.
Ships in the cloned repo, not via ReaPack. The MCP server exposes the same
write path as `insert_riff` (one part), `cut_band` (the four-track jam), and
`humanize_take` (see below), so an agent without shell access can cut parts
too.

## Humanize an existing drum take

`humanize` takes a flat drum item that is already on a track and gives it a
dynamic contour and micro-timing, in place.

```bash
python3 reaperd.py humanize --track drums --dry-run       # plan, print, write nothing
python3 reaperd.py humanize --track drums --amount 25     # 0-100 looseness (default 25)
python3 reaperd.py humanize --track drums --follow-lead   # copy the hand from your own bars
```

The dynamic contour is always applied; `--amount` only scales the random
spread and timing looseness on top of it. Fills build as a crescendo that
peaks at the resolve rather than piling up on the ceiling. The golden rule is
hardline (`skills/drum-apparatus/drumgen/goldenrule.py`): no drum hits the
same velocity twice in a row, per drum in time order, whatever lands between.

`--follow-lead` reads the velocity hand out of the bars you humanized by hand
and carries it across the rest of the take instead of applying the shared
taste model. It auto-detects the first flat bar; `--example-through-bar N`
sets the boundary explicitly. `--seed` is fixed by default, so a run is
reproducible.

## Agent protocol

`reaperd.py` and the MCP server both speak this; write it yourself only if you
are driving the bridge from something else. Every rule below is load-bearing —
the bridge relies on it somewhere.

**A command is one JSON file in `inbox/`.**

```json
{
  "id": "agent-2026-08-17T21-14-03-9f2c",
  "type": "set_track_volume",
  "version": 3,
  "created_at": "2026-08-17T21:14:03-04:00",
  "payload": { "target_track_name": "Kick", "volume_db": -3.0 }
}
```

- **`id` must be timestamp-prefixed**, `agent-YYYY-MM-DDTHH-MM-SS-hex`. Inbox
  order is a lexical sort of filenames, so the stamp is what makes commands run
  in the order you wrote them, and the retention sweep parses that same stamp to
  find the oldest files to drop. A random id runs at an arbitrary point and
  ages unpredictably.
- **`created_at` is required.** If the bridge dies mid-command, the file is left
  in `processing/`; at startup the triage reads `created_at` and refuses to
  re-run anything stale rather than replaying an edit into a session that has
  moved on. No `created_at` means it cannot tell, and re-runs.
- **`version` is `3`.** Anything else is rejected with `UNSUPPORTED_VERSION`.
- **`token`** is required only when `auth_token` is set in
  `bridge/bridge_config.json`; without it the reply is `AUTH_FAILED`.

**Write `<id>.json.tmp`, then rename it to `<id>.json`.** The bridge scans for
`.json` and skips `.tmp`, so the rename is what publishes the command. Writing
straight to `.json` races the poll loop and can hand the bridge half a file.

**Replies land in `outbox/<id>.json`**, usually within a poll tick (the loop
runs four times a second; a render blocks it for the length of the render, and
`bridge/heartbeat.json` carries `busy` while that happens). **Delete each reply
after you read it.** The retention sweep deliberately never touches `outbox/`,
because a count-based sweep there would delete replies an agent had not polled
yet — so uncollected replies stay forever, and cleaning up is the reader's job.

Command files themselves move `inbox/` → `processing/` → `archive/` (ok) or
`failed/` (error); those the sweep does bound.

## For agents

Read `AGENTS.md` for the workflow, `CLAUDE.md` for the fast operational
protocol, and `bridge/command_schema.md` for every command. Working JSON
examples are in `commands/examples/`.

## Develop

The CI matrix covers Windows, macOS, and Linux. From the repository root:

```bash
python -m pip install pytest
python -m pytest tests skills/drum-apparatus/tests -q
python -m py_compile reaperd.py reaper_mcp.py setup/install.py
lua bridge/test_bridge.lua
lua bridge/test_json.lua
```

The Lua checks require Lua 5.4. Live REAPER integration remains a separate
manual gate because CI does not launch the DAW.

## Layout

```text
bridge/reaper_agent_bridge.lua   the bridge (runs inside REAPER, OS-neutral)
bridge/bridge_config.json        machine-specific config (regenerated)
bridge/command_schema.md         full command reference
reaperd.py                       cross-platform agent CLI (Python 3)
reaper_mcp.py                    MCP stdio server over the same bridge
console_sidecar.py               Daemon Console broker (headless Claude session)
bridge/reaper_daemon_console.lua Daemon Console panel (ReaImGui, clone-only)
docs/CONSOLE.md                  console architecture + operating rules
setup/install.py                 wire auto-start into REAPER (cross-platform)
commands/examples/               one JSON example per command
skills/drum-apparatus/           DSL drum engine + kit-map auto-discovery
skills/guitar-apparatus/         guitar/bass riff notation + performance engine
inbox/ outbox/ processing/ ...   runtime folders
```

## Security

The bridge is a **local file** control channel: any process that can write to
`inbox/` can drive REAPER (load FX, change tracks, render). That's fine for a
single-user dev box, which is what it's built for. Two rules keep it that way:

- **Keep the bridge folder local.** Don't put it on a network share, a synced
  drive, or anywhere another machine/user can write. That's the real boundary.
- Mutating commands run inside REAPER undo blocks, so anything an agent does is
  `Cmd/Ctrl+Z`-reversible.

**Optional shared-secret token.** On a shared or less-trusted box you can require
a token: set `auth_token` to any string in `bridge/bridge_config.json`. The
bridge then rejects any command without a matching `token` (`AUTH_FAILED`), and
`reaperd.py` reads the same config and attaches it automatically — no other
setup. Honest limit: the token lives in a local-readable config, so it stops
*accidental or other-app* writes, **not** a local attacker who can read that
file. Keeping the folder local is still the actual protection.

## License

MIT. See [LICENSE](LICENSE).
