#!/usr/bin/env python3
"""Detached event writer for the Reaper Daemon console spike.

Two jobs, one file, stdlib only:

  default mode  append one JSON line per second for N seconds to an event
                stream (default console/events/spike.jsonl). Proves that
                reaper.ExecProcess actually launched something (C2) and gives
                the panel's byte-offset tailer a live file to follow (C3).

  --freeze      run `reaperd.py measure <track> --seconds N` in a child
                process and log start/end/returncode to a second stream
                (default console/events/freeze.jsonl). The capture blocks
                REAPER's main thread for several seconds, which is exactly the
                stall the panel has to survive (C4). This runs OUT OF PROCESS
                on purpose: the panel must not be the caller, or "it froze" and
                "it was busy" are the same event.

Both modes write their pid, the interpreter that actually launched them and
their full argv as the FIRST line of the stream, so the panel can prove what
really ran instead of trusting ExecProcess's return value.

Every record is one write() of a complete line (payload + "\n") followed by
flush(), so a concurrent reader either sees the whole line or nothing after
the previous newline. The stream is opened for BINARY APPEND and is never
renamed over: it is an append-only log, not a replace-style state file.
"""

import argparse
import json
import os
import subprocess
import sys
import time

# The event lines carry a fixed-size pad. It costs nothing (60 lines) and it
# widens each write past the point where a single WriteFile is trivially
# atomic from a concurrent reader's point of view, which is what gives the
# panel's carry-buffer a realistic chance of catching a mid-append state.
PAD = "." * 256


def iso_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def emit(handle, obj):
    """Write exactly one complete JSON line, then flush."""
    line = json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n"
    handle.write(line.encode("utf-8"))
    handle.flush()


def open_stream(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    # "ab" == binary append. Every write lands at the current end of file even
    # if another process appended in between, which is what makes concurrent
    # append + tail safe here.
    return open(path, "ab")


def header(tag, path, extra=None):
    record = {
        "seq": 0,
        "kind": "header",
        "tag": tag,
        "utc": iso_utc(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        # sys.executable is the whole point of this line: it proves whether
        # pythonw.exe or python.exe is what ExecProcess really started.
        "executable": sys.executable,
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "out": os.path.abspath(path),
    }
    if extra:
        record.update(extra)
    return record


def run_appender(args):
    with open_stream(args.out) as handle:
        emit(handle, header(args.tag, args.out, {
            "mode": "append",
            "seconds": args.seconds,
            "interval": args.interval,
        }))
        started = time.monotonic()
        deadline = started + args.seconds
        seq = 1
        next_at = started
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            emit(handle, {
                "seq": seq,
                "kind": "tick",
                "tag": args.tag,
                "utc": iso_utc(),
                "t": round(now - started, 3),
                "pid": os.getpid(),
                "pad": PAD,
            })
            seq += 1
            next_at += args.interval
            sleep_for = next_at - time.monotonic()
            if sleep_for > 0:
                time.sleep(min(sleep_for, args.interval))
        emit(handle, {
            "seq": seq,
            "kind": "done",
            "tag": args.tag,
            "utc": iso_utc(),
            "t": round(time.monotonic() - started, 3),
            "ticks_written": seq - 1,
            "pid": os.getpid(),
        })
    return 0


def run_freeze(args):
    reaperd = os.path.abspath(args.reaperd)
    python = args.python or sys.executable
    cmd = [python, reaperd, "measure", args.track,
           "--seconds", str(args.capture_seconds), "--json"]
    with open_stream(args.out) as handle:
        emit(handle, header(args.tag, args.out, {
            "mode": "freeze",
            "cmd": cmd,
            "delay": args.delay,
            "capture_seconds": args.capture_seconds,
        }))
        if args.delay > 0:
            time.sleep(args.delay)
        emit(handle, {
            "seq": 1,
            "kind": "freeze_start",
            "tag": args.tag,
            "utc": iso_utc(),
            "cmd": cmd,
        })
        started = time.monotonic()
        rc, out, err, failure = None, "", "", None
        try:
            proc = subprocess.run(
                cmd,
                cwd=os.path.dirname(reaperd) or None,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired:
            failure = "timeout after %ss" % args.timeout
        except OSError as exc:
            failure = "OSError: %s" % exc
        emit(handle, {
            "seq": 2,
            "kind": "freeze_end",
            "tag": args.tag,
            "utc": iso_utc(),
            "elapsed": round(time.monotonic() - started, 3),
            "returncode": rc,
            "failure": failure,
            "stdout_head": out[:600],
            "stderr_head": err[:600],
        })
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Append-only event writer for the console spike.")
    parser.add_argument("--out", required=True,
                        help="event stream to append to (created if missing)")
    parser.add_argument("--tag", default="spike",
                        help="tag stamped on every record")
    parser.add_argument("--freeze", action="store_true",
                        help="run reaperd.py measure instead of the tick loop")

    # append mode
    parser.add_argument("--seconds", type=float, default=60.0,
                        help="append-mode run length (default 60)")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="append-mode seconds between lines (default 1)")

    # freeze mode
    parser.add_argument("--reaperd", default="",
                        help="path to reaperd.py (freeze mode)")
    parser.add_argument("--python", default="",
                        help="interpreter to run reaperd.py with (freeze mode)")
    parser.add_argument("--track", default="Track 1",
                        help="track name to measure (freeze mode)")
    parser.add_argument("--capture-seconds", type=float, default=8.0,
                        help="measure capture length (freeze mode)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="seconds to wait before starting the capture")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="hard timeout on the measure child process")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.freeze:
        if not args.reaperd:
            print("--freeze needs --reaperd <path to reaperd.py>",
                  file=sys.stderr)
            return 2
        return run_freeze(args)
    return run_appender(args)


if __name__ == "__main__":
    sys.exit(main())
