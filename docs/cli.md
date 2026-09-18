# The CLI

`reaperd.py` is the one entry point for everything an agent does. No shell
helpers, no `jq` or `grep` pipelines. It behaves the same on macOS, Windows,
and Linux.

## Commands

| Command | What it does |
| --- | --- |
| `status` | liveness check. Run it first. |
| `recipe capture\|diff\|rebuild <file> [--dry-run]` | save, compare or rebuild a [mix recipe](mix-recipes.md) |
| `send <cmd.json> --wait` | send a command file and print the reply |
| `cmd <type> '<payload-json>'` | send a command by type and payload |
| `fxload "<plugin query>" <track\|master>` | load a plugin by fuzzy name |
| `save-chain <track\|master> [--name NAME] [--overwrite]` | live FX chain to `.RfxChain` |
| `snapshot-chains [--prefix P] [--overwrite] [--dry-run]` | save every track's chain |
| `setparam <track> "<fx>" "<param>" "<display value>"` | set any parameter by display value |
| `eq <track> "<fx>" <band> <freqHz> <gaindB> [Q]` | set one EQ band |
| `measure <track> [--seconds N] [--start S] [--json]` | capture and measure one track |
| `verify <track> [--seconds N] [--json] -- <type> '<payload>'` | mutate with pre/post proof |
| `profile <project.rpp> <track> [--start-bar N] [--bars N] [--max-seconds S]` | analyze a stem for drum planning |
| `groove <beat.dsl> --track Drums [--position SEC] [--map NAME]` | render a drum DSL to a track |
| `jam` | drum DSL from stdin to the selected track |
| `humanize --track Drums [--amount 0-100] [--follow-lead] [--dry-run]` | humanize an existing drum take |
| `transcribe start\|status\|insert\|configure` | [transcribe an audio item into drum MIDI](transcription.md) using an optional local runtime |
| `shred --track T [--part guitar\|bass] [--bars-file riff.txt] [--seed N]` | render a guitar or bass riff |
| `band` | two guitars, bass, and drums in one command |
| `list-maps` | available drum-kit maps |
| `discover-map <track> [--save <name>]` | build a kit map from a track's note names |
| `add-map <name> --file <map.json>` | add a kit map by hand (or `--roles '{...}'`, or stdin) |
| `remove-map <name>` | remove a kit map |

## Notes

**Plugin loading.** `fxload` and `cmd add_fx` resolve a fuzzy query to
REAPER's exact installed name from the VST, CLAP, and AU cache before loading.

**Parameters.** `setparam` works on any plugin by parameter index. It
binary-searches the normalized value that produces the target display value,
then verifies the result.

**Measuring.** `measure` captures one track once, behind the same
`allow_audio_writes` gate as `capture_track_audio`. It always reports LUFS-I
when REAPER provides it. Digital silence reads as null and is flagged. With
[Post Mortem](https://github.com/wretcher207/post-mortem) installed it adds
sample peak, RMS, crest factor, 1/3-octave spectrum, stereo image, and a
silence check.

Bounds resolve once: time selection start if active, otherwise the edit
cursor. Override with `--start`. To compare two runs of the same spot, pass
the same `--start` and `--seconds` to both. Output labels its `metrics_source`
(`postmortem` or `render_stats`) and its capture scope. A full-mix fallback
is reported as such, never as per-track evidence.

Drum, guitar, and verify commands have their own pages:
[Drums](drums.md), [Guitar and bass](guitar-bass.md), [Verify](verify.md).
