"""A fake claude CLI for the console sidecar tests.

The sidecar drives a real child process over NDJSON stdin/stdout, so the only
honest test double is a real child process. This module writes a small Python
script to disk and hands back the argv the sidecar should spawn:

    [sys.executable, script_path, scenario_path]

`sys.executable` rather than a shebang: a `#!/usr/bin/env python3` line is inert
on Windows, and this suite runs on windows-latest. Same reasoning as
bridge_fakes.fake_bridge_script, which drives the file queue from a thread
because that is what the real bridge does.

The scenario is JSON so the fake needs no import from the test module:

    {
      "startup":  [item, ...],          # emitted before any stdin is read
      "turns":   [[item, ...], ...],    # one list per received user message
      "default": [item, ...]            # used past the end of "turns"
    }

An item that is a plain object is written to stdout as one NDJSON line, with
`session_id` filled in from --session-id when absent. Items with a "__" key are
directives:

    {"__sleep": 0.05}          pause
    {"__stderr": "text"}       one line on stderr
    {"__stderr_bytes": 262144} that many bytes of stderr, no newlines until the end
    {"__stdout_bytes": 262144} an assistant text block of that many characters
    {"__exit": 1}              exit immediately with this code
    {"__spawn_child": "path"}  spawn a grandchild that appends to `path` forever
    {"__flush": true}          no-op, for readability

A control_request/interrupt on stdin is always answered immediately with the
measured-real shape, and it aborts the rest of the current turn (a real
interrupt stops the turn mid-stream and still emits a result carrying
terminal_reason "aborted_streaming"). An interrupt received while IDLE is
answered and emits NO result at all, which is the behaviour the sidecar must
never block on.
"""

import json
import os
import sys

FAKE_CLAUDE_SOURCE = r'''
import json, os, subprocess, sys, threading, time

# The stand-in for the MCP server claude.exe spawns as a GRANDCHILD. It
# self-terminates after 30 s so a failing tree-kill assertion leaves no runaway
# python spinning on the dev box.
GRANDCHILD_SOURCE = """import sys, time

path = sys.argv[1]
end = time.time() + 30
while time.time() < end:
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write('x')
    time.sleep(0.02)
"""

scenario_path = sys.argv[1]
with open(scenario_path, "r", encoding="utf-8") as fh:
    SCENARIO = json.load(fh)

ARGV_PATH = scenario_path + ".argv.json"
STDIN_PATH = scenario_path + ".stdin.jsonl"

argv = sys.argv[2:]
with open(ARGV_PATH, "w", encoding="utf-8") as fh:
    json.dump(argv, fh)

SESSION_ID = None
for i, a in enumerate(argv):
    if a in ("--session-id", "--resume") and i + 1 < len(argv):
        SESSION_ID = argv[i + 1]
SESSION_ID = SESSION_ID or "fake-session"

out = sys.stdout.buffer
errout = sys.stderr.buffer
lock = threading.Lock()
state = {"interrupted": False, "stop": False}
children = []


def emit(obj):
    obj = dict(obj)
    obj.setdefault("session_id", SESSION_ID)
    with lock:
        out.write((json.dumps(obj) + "\n").encode("utf-8"))
        out.flush()


def emit_raw(text):
    with lock:
        out.write((text + "\n").encode("utf-8"))
        out.flush()


def record_stdin(obj):
    with open(STDIN_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


prompts = []
prompt_lock = threading.Condition()


def reader():
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        record_stdin(obj)
        if obj.get("type") == "control_request":
            rid = obj.get("request_id")
            sub = (obj.get("request") or {}).get("subtype")
            emit({"type": "control_response",
                  "response": {"subtype": "success", "request_id": rid,
                               "response": {"still_queued": []}}})
            if sub == "interrupt":
                state["interrupted"] = True
        elif obj.get("type") == "user":
            with prompt_lock:
                prompts.append(obj)
                prompt_lock.notify()
    with prompt_lock:
        state["stop"] = True
        prompt_lock.notify()


threading.Thread(target=reader, daemon=True).start()


def run_items(items):
    for item in items:
        if state["interrupted"]:
            emit({"type": "result", "subtype": "error_during_execution",
                  "terminal_reason": "aborted_streaming", "is_error": True,
                  "duration_ms": 5, "num_turns": 1,
                  "total_cost_usd": SCENARIO.get("aborted_cost", 0.01)})
            state["interrupted"] = False
            return
        if not isinstance(item, dict):
            continue
        if "__sleep" in item:
            time.sleep(float(item["__sleep"]))
        elif "__stderr" in item:
            with lock:
                errout.write((str(item["__stderr"]) + "\n").encode("utf-8"))
                errout.flush()
        elif "__stderr_bytes" in item:
            n = int(item["__stderr_bytes"])
            with lock:
                errout.write((b"E" * n) + b"\n")
                errout.flush()
        elif "__stdout_bytes" in item:
            n = int(item["__stdout_bytes"])
            emit({"type": "assistant",
                  "message": {"role": "assistant",
                              "content": [{"type": "text", "text": "S" * n}]}})
        elif "__raw" in item:
            emit_raw(str(item["__raw"]))
        elif "__exit" in item:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(int(item["__exit"]))
        elif "__spawn_child" in item:
            path = item["__spawn_child"]
            # A real script file, never `python -c "<multi-line code>"`. On
            # Windows the embedded newlines in a -c argument get lost through
            # CreateProcess, python then falls back to reading its program from
            # the INHERITED stdin pipe, and the grandchild hangs forever with no
            # output and a live pid. Same reason the fake CLI itself is invoked
            # as [sys.executable, script].
            gc_path = scenario_path + ".grandchild.py"
            if not os.path.exists(gc_path):
                with open(gc_path, "w", encoding="utf-8") as gc_fh:
                    gc_fh.write(GRANDCHILD_SOURCE)
            children.append(subprocess.Popen(
                [sys.executable, gc_path, path], stdin=subprocess.DEVNULL))
        elif "__flush" in item:
            pass
        else:
            emit(item)


run_items(SCENARIO.get("startup") or [])

turns = SCENARIO.get("turns") or []
default = SCENARIO.get("default")
index = 0
while True:
    with prompt_lock:
        while not prompts and not state["stop"]:
            prompt_lock.wait(0.05)
        if not prompts:
            if state["stop"]:
                break
            continue
        prompts.pop(0)
    # A real interrupt that arrived while idle emitted no result and left the
    # session usable; the flag must not leak into the next turn.
    state["interrupted"] = False
    items = turns[index] if index < len(turns) else (default or [])
    index += 1
    run_items(items)
'''


def write_fake_claude(tmp_dir, scenario):
    """Write the fake CLI plus its scenario. Returns (argv_prefix, paths)."""
    tmp_dir = str(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)
    script = os.path.join(tmp_dir, "fake_claude.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(FAKE_CLAUDE_SOURCE)
    scenario_path = os.path.join(tmp_dir, "scenario.json")
    with open(scenario_path, "w", encoding="utf-8") as fh:
        json.dump(scenario, fh)
    return ([sys.executable, script, scenario_path],
            {"script": script, "scenario": scenario_path,
             "argv": scenario_path + ".argv.json",
             "stdin": scenario_path + ".stdin.jsonl"})


# ---------------------------------------------------------------------------
# Scenario building blocks, so the tests read as protocol rather than as JSON.
# ---------------------------------------------------------------------------

def init_event(capabilities=("interrupt_receipt_v1",), tools=21):
    """system/init. Emitted PER TURN, not per session, and lazily: nothing is
    emitted at all until a user message arrives."""
    return {"type": "system", "subtype": "init",
            "capabilities": list(capabilities),
            "tools": [f"tool_{i}" for i in range(tools)],
            "model": "opus"}


def assistant_text(text):
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def text_delta(text):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta",
                      "delta": {"type": "text_delta", "text": text}}}


def tool_use(tool_id, name, tool_input=None):
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": tool_id,
                                     "name": name,
                                     "input": tool_input or {}}]}}


def tool_result(tool_id, payload, is_error=False):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "tool_result",
                                     "tool_use_id": tool_id,
                                     "is_error": is_error,
                                     "content": [{"type": "text", "text": text}]}]}}


def result(total_cost_usd, subtype="success", **extra):
    """total_cost_usd is CUMULATIVE for the process, which is the whole point
    of the differencing tests."""
    body = {"type": "result", "subtype": subtype,
            "duration_ms": 1200, "num_turns": 1,
            "total_cost_usd": total_cost_usd,
            "usage": {"input_tokens": 10, "output_tokens": 20}}
    body.update(extra)
    return body
