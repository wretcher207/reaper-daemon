# Audio to drum MIDI

Put a song on an audio track, choose a drum kit, and transcribe the drum part.
The daemon separates the drums locally, detects the hits, and maps them to your
kit. Timing follows the recording, including the item's trim and position in
the project. Analysis runs in a separate process while REAPER stays available.

## Optional installation

The bridge, CLI and MCP server still need only Python's standard library. Drum
transcription uses a separate Python 3.11 environment, ffmpeg and Git on PATH.
From the repository root:

```bash
python setup/install_transcription.py --python /path/to/python3.11
```

On Windows, pass the full path to your Python 3.11 executable. The installer
creates `.venvs/drum-transcription/` and installs the pinned dependencies from
`setup/requirements-transcription.txt`. It downloads packages and ADTOF weights;
Demucs downloads its weights on the first separation job. Audio stays local.
Model code and weights aren't bundled into the daemon's distribution.

To use an environment that already has those dependencies:

```bash
python reaperd.py transcribe configure --python /path/to/environment/python
```

`REAPER_TRANSCRIPTION_PYTHON` also accepts that path. After updating the daemon,
send `python reaperd.py cmd reload_bridge '{}'` and reconnect your MCP client
so it discovers the tools.

## CLI

```bash
python reaperd.py transcribe start --source-track "Reference"
python reaperd.py transcribe status JOB_ID
python reaperd.py transcribe insert JOB_ID --track "Monarch" --dry-run
python reaperd.py transcribe insert JOB_ID --track "Monarch"
```

Replace `JOB_ID` with the ID returned by `start`. With several items on the
source track, add `--item-index 0` for the first item. `--source-item-guid`
selects an item directly. Without a selector, exactly one audio item must be
selected in REAPER. No project save is needed for analysis.

`status` reports progress, failure details or completed output paths.
`insert --dry-run` checks the live source and destination without creating an
item. Stop playback before inserting. The destination range must be empty.
One undo removes the new part; save the project when you want to keep it.
Insertion preserves the reference's mute state, project tempo and existing MIDI.

The destination's live note names determine the drum map. The Monarch naming
pattern selects its map and rimshot snare; other kits use the existing note-name
matcher. If the kit doesn't expose enough names, choose a map with
`--map "GM Standard"`, or add one through `add-map` first.

For an isolated drum recording, `--drums-only` skips separation. CPU processing
uses four threads by default; `--threads 1` through `--threads 16` changes that.
`--device cuda` requires a working CUDA-enabled PyTorch installation and GPU.

## MCP

| Tool | Use |
| --- | --- |
| `transcribe_drums` | Start analysis. Optional `source_track`, `item_index` or `source_item_guid`; otherwise use the selected item. `separate` defaults to true. |
| `get_drum_transcription` | Read progress and output files for `job_id`. |
| `insert_drum_transcription` | Insert `job_id` on `track`, with optional `map_name` and `dry_run`. |

An agent can run this sequence from “recreate the drums from the reference
track on Monarch.” It should report progress while the worker runs, then insert
once analysis completes. A completed analysis alone hasn't changed the project.

## Verification and recovery

Insertion checks the source file, project tab, item and take identities, trim,
position and length. It checks kit note names again inside the bridge call that
writes the notes. A changed source requires a fresh job.

Notes are inserted in project seconds, so tempo markers don't shift them away
from the audio. The bridge verifies pitch, velocity, channel, timing, duration
and count. A failed write removes only the item created by that command. Drum
velocities pass the shared no-consecutive-repeats check before and after insertion.

A job marker prevents duplicate insertion if a client loses the reply. The job
folder keeps `insert-attempt.json` and `receipt.json`. After an ambiguous failure,
inspect those files and the live project before another write. Starting another
job isn't a way to bypass that check.

## Files and limits

Jobs live in `state/drum-transcription/`, excluded from Git. Each keeps a source
snapshot, file fingerprint, worker log, model activations, detected hits,
`drums-gm.mid` and `drums-preview.wav`. In-project MIDI doesn't depend on the
exported MIDI file.

The first version accepts one local audio take of up to ten minutes at original
speed and pitch. Reversed/section sources, stretch markers, take FX, take
envelopes and items extending past the source are refused. It reads the source
file before item fades/gain and track FX. Transcription doesn't render or save
the REAPER project.

The models detect kick, snare, tom, hat and cymbal hits. Tom pitches come from
estimated resonances; cymbal type and hat openness are approximate. Dense parts
can contain missed or extra hits. MIDI verification proves the detected notes
reached the track; listen against the recording to judge the transcription.

Processing uses [Demucs](https://github.com/adefossez/demucs) and
[ADTOF-pytorch](https://github.com/xavriley/ADTOF-pytorch). The installer pins the
ADTOF source revision used by the worker.
