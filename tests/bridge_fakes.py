"""A fake bridge for tests: answers the file queue like the real Lua bridge.

Shared by test_reaperd.py and test_reaper_mcp.py so the inbox/outbox protocol
emulation lives in exactly one place — if the queue semantics ever change,
both suites break together instead of one passing against stale behavior.
"""

import json
import math
import os
import struct
import threading
import time
import wave


def write_test_wav(path, seconds=0.05, rate=8000, amplitude=0.5):
    """A small real WAV (440 Hz sine) for fakes that must honor the capture
    contract: the reply's file_path must point at an actual fresh file."""
    n = int(rate * seconds)
    frames = b"".join(
        struct.pack("<h", int(amplitude * 32767
                              * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(n))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def _read_command(path):
    """Read an inbox command, retrying transient Windows file locks.

    The same phenomenon the CLI's reply reader handles (Codex gate BLOCKER,
    2026-07-23): on Windows a freshly os.replace()d JSON can throw
    PermissionError on the first open while the rename settles or an
    indexer holds it. The real Lua bridge polls, so it never dies to this;
    a fake that crashes instead leaves the command unanswered and fails
    the test as a phantom protocol bug (seen live on windows-latest /
    Python 3.14, run 30472609660). Returns None if the file vanished."""
    deadline = time.monotonic() + 2.0
    while True:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _remove_command(path):
    """Delete an inbox command with the same transient-lock retry."""
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def fake_bridge(root, reply_body, record=None, delay=0.0):
    """Watch inbox/, answer the first command with reply_body, like the bridge.
    When record is a list, the received command dict is appended to it."""
    def run():
        inbox = os.path.join(root, "inbox")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            files = [f for f in os.listdir(inbox)
                     if f.endswith(".json") and not f.endswith(".tmp")]
            if files:
                cid = files[0][: -len(".json")]
                path = os.path.join(inbox, files[0])
                if record is not None:
                    command = _read_command(path)
                    if command is None:
                        continue
                    record.append(command)
                _remove_command(path)
                time.sleep(delay)
                reply = dict(reply_body, id=cid)
                out = os.path.join(root, "outbox", cid + ".json")
                with open(out + ".tmp", "w", encoding="utf-8") as f:
                    f.write(json.dumps(reply))
                os.replace(out + ".tmp", out)
                return
            time.sleep(0.01)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def fake_bridge_script(root, replies, record=None, delay=0.0):
    """Watch inbox/, answer up to len(replies) commands IN ORDER, then exit.

    Each entry in replies is either a reply-body dict, or a callable taking
    the received command dict and returning the reply body — callables let a
    scripted reply mimic bridge side effects (e.g. writing the capture WAV the
    reply's file_path points at), keeping fake fidelity with the real
    protocol. When record is a list, every received command dict is appended.
    The single-reply fake_bridge above stays untouched; both suites share it.
    """
    def run():
        inbox = os.path.join(root, "inbox")
        deadline = time.monotonic() + 5.0
        answered = 0
        while answered < len(replies) and time.monotonic() < deadline:
            files = sorted(f for f in os.listdir(inbox)
                           if f.endswith(".json") and not f.endswith(".tmp"))
            if not files:
                time.sleep(0.01)
                continue
            cid = files[0][: -len(".json")]
            path = os.path.join(inbox, files[0])
            command = _read_command(path)
            if command is None:
                continue
            if record is not None:
                record.append(command)
            _remove_command(path)
            time.sleep(delay)
            body = replies[answered]
            if callable(body):
                body = body(command)
            reply = dict(body, id=cid)
            out = os.path.join(root, "outbox", cid + ".json")
            with open(out + ".tmp", "w", encoding="utf-8") as f:
                f.write(json.dumps(reply))
            os.replace(out + ".tmp", out)
            answered += 1
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
