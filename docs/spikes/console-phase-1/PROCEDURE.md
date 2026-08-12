# Console spike panel

> **Archived 2026-08-11.** These are the original spike instructions, kept as the
> record of what was run. The panel they register has changed; do not follow this
> as installation steps today. See `README.md` in this folder.

A ReaImGui panel that runs its own acceptance checks inside REAPER and writes
the verdicts to disk. The agent that wrote it cannot load a ReaScript, so the
panel reports on itself and you read the files afterward.

Register it once. After that, every rebuild lands at the same path and re-running
the same action picks up the new code.

## One-time registration

1. In REAPER, open **Actions > Show action list...** (or press `?`).
2. Click **New action...** at the bottom left of the Actions window.
3. Choose **Load ReaScript...** from that menu.
4. In the file dialog, go to
   `C:\Users\wretc\workspace\reaper-daemon\bridge\reaper_daemon_console.lua`
   and click **Open**.
5. The action now shows in the list as **Script: reaper_daemon_console.lua**.
   Select it and click **Run**.

That is the whole registration. Do not repeat it after a rebuild. Select the
same action and click Run again. The script calls `set_action_options(1)`, so a
second Run replaces the running instance instead of stacking a second one.

## While the panel is open

Leave it open for at least 90 seconds. The detached event writer runs for 60
seconds, and C3's strongest verdict needs the writer's own closing record.

### C5, the keyboard checks

The panel drives these itself. Follow the yellow prompt at the top, one step at a
time. Each step has **Arm / re-snapshot** (take a fresh before-reading, use it if
you fumbled a step) and **Skip / Next** (give up on that step and move on, which
records a FAIL).

1. Click the panel background, not the text box, then press **SPACE**. REAPER's
   transport should start. The panel stops it again on its own.
2. Click **inside** the text box, then press **SPACE**. The transport must not
   move and a space must land in the text.
3. With the text box still focused, press **ESCAPE**.
4. Click inside the text box again, then press **CTRL+SPACE**.
5. Click the panel background, then press the **LEFT ARROW** key. REAPER's edit
   cursor should move.

### C1, the text box behaviour

Click into the text box and press **ENTER**. It should add a new line rather than
submit. Then press **CTRL+ENTER**. That should submit. Both are needed for a PASS.
If the row has not flipped by itself, click **Finish C1 input test**.

### C2, the console flash

Watch the screen at the moment the panel starts and again when you click
**Relaunch appender**. If a black console window blinks, tick **A console window
flashed**. **python.exe flash A/B** launches the same script through `python.exe`
instead of `pythonw.exe` for comparison. The shipped launch path always uses
`pythonw.exe`.

### C4, the freeze

Click **Run C4 freeze**. That fires a detached process which records an 8 second
capture of Track 1 through `reaperd.py measure`. A separate process does the
calling, so any stall the panel measures is REAPER blocking rather than the panel
waiting on itself. Wait about 30 seconds for the row to settle.

C4 may come back saying no stall was recorded. Believe it. A 3 second capture
run from a shell on this machine never starved the bridge's own defer loop for
more than a heartbeat interval. Click **Self-block 6s (control)** afterwards.
That blocks the main thread deliberately and exercises the same recovery path,
and it is recorded under its own name so it can never be mistaken for C4.

### C6, the dock

C6 needs two runs. On the first run there is nothing saved to restore, so it
records a FAIL that says so. Drag the panel into a REAPER docker, close it, then
run the action again. The second run should restore the dock and record a PASS.

## Where the output lands

| File | What it holds |
|---|---|
| `console/spike_results.jsonl` | one JSON verdict per line, append-only |
| `logs/console_panel.log` | human-readable `[<UTC ISO>] msg` log |
| `console/events/spike.jsonl` | the detached writer's event stream |
| `console/events/freeze.jsonl` | the C4 capture's start, end and return code |
| `console/panel.json` | liveness file, rewritten about once a second |

Nothing here is written to `logs/bridge.log`, and `logs/bridge.lock` is never
touched.

## Reading the results

Every check is FAIL until it has evidence. Closing the panel writes a row for
every check that never ran, marked with a `not_triggered` or `skipped` detail.
Those rows mean no data was collected. Read the `detail` object on any FAIL
before concluding the code is broken.
