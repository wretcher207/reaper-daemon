"""A fake bridge for tests: answers the file queue like the real Lua bridge.

Shared by test_reaperd.py and test_reaper_mcp.py so the inbox/outbox protocol
emulation lives in exactly one place — if the queue semantics ever change,
both suites break together instead of one passing against stale behavior.
"""

import json
import os
import threading
import time


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
                    with open(path, "r", encoding="utf-8") as f:
                        record.append(json.load(f))
                os.remove(path)
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
