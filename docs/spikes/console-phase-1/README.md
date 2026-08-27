# Console spike, Phase 1 (2026-08-09)

Archived. This is the evidence trail for the Daemon Console panel, not live code.
Nothing in the repo calls `spike_appender.py` any more, and `PROCEDURE.md` registers
a panel whose behaviour has since changed, so do not follow it as installation
instructions. It is here as a working pattern to copy for Phase 2, and as the record
of what was actually observed rather than claimed. The spike tooling itself (`spike_appender.py` and
`spike_results.jsonl`) was removed after the console shipped; both live in
git history if the raw rows are ever needed.

## The problem it solved

An agent session cannot load a ReaScript into REAPER. So rather than guess whether a
ReaImGui panel would survive the things a console has to survive, the panel ran its own
acceptance checks inside REAPER and appended one JSON verdict per line to
`spike_results.jsonl`. You worked the keyboard, the panel wrote down what happened, and
the results were read afterward from disk.

Two rules made the output trustworthy:

- **Every check is FAIL until it has evidence.** Closing the panel writes a FAIL row for
  every check that never ran. A FAIL is not the same as a defect, so read the `detail`
  object before concluding anything is broken.
- **The freeze test runs out of process.** `spike_appender.py --freeze` calls
  `reaperd.py measure` from a detached child, never from the panel. If the panel were
  the caller, "REAPER blocked" and "the panel was waiting on itself" would look
  identical in the log.

`spike_appender.py` also has a plain append mode: one padded JSON line per second to a
stream, which proves `reaper.ExecProcess` really launched something and gives the
panel's byte-offset tailer a live file to follow. Every record is one `write()` of a
complete line plus a flush, opened for binary append and never renamed over, so a
concurrent reader sees a whole line or nothing.

## What the runs found

`spike_results.jsonl` holds 24 rows across two runs. The second run was cut short, so
most of its rows are the no-evidence kind. Reading only the last row per check
misreads the file.

| Check | Result | Note |
|---|---|---|
| `C1_INPUTTEXT` | FAIL, both runs, with data | `EnterReturnsTrue` swallowed every keystroke. Fixed in `8a1ed8c`. |
| `C1_IMGUI_BOOT` | PASS then FAIL | The FAIL detail is a real API note: `GetVersion()` takes no arguments. |
| `C5_CTRL_SPACE_ROUTED` | FAIL, run 1, with data | Ten routing strategies were available and none carried it. |
| `C6_DOCK` | FAIL then PASS | Exactly as designed: nothing to restore on a first run, restored on the second. |
| `C2_EXECPROCESS` | PASS, both | The stream grew after the call, so the child really started. |
| `C3_TAIL` | PASS, both | 62 lines consumed, 0 missing, carry buffer empty at the end. |
| `C4_FREEZE_SURVIVAL` | PASS, both | A real 8 second capture never starved the defer loop past a heartbeat. |
| `C4_SELF_BLOCK_CONTROL` | PASS, run 1 | A deliberate 12.2 second block, recorded under its own name so it can never be mistaken for C4. |
| `C5` keyboard steps | mixed, mostly no evidence | Run 1 got space-unfocused and escape; the rest closed before the step ran. |

The short version: the design held, and the failures with evidence behind them are the
commits that followed.
