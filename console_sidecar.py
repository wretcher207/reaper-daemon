#!/usr/bin/env python3
"""console_sidecar — the Daemon Console's broker between REAPER and claude.exe.

One long-lived headless Claude Code session, driven over NDJSON stdin/stdout,
with `reaper_mcp.py` wired in as its MCP server. A ReaImGui panel inside REAPER
writes prompt files; this process forwards them, normalizes the stream back into
something a Lua panel can parse on the UI thread, and enforces the money and
liveness rules the CLI does not.

Stdlib only, same rule as the rest of the repo (AGENTS.md:6,9). External
binaries are fine; third-party Python is not.

    python console_sidecar.py                      # daemon, panel-driven
    python console_sidecar.py --once "<prompt>"    # one turn, events to stdout

FILE PROTOCOL (all paths relative to the repo root)

Atomic replace, `.tmp` + os.replace, with a bounded PermissionError retry
because Windows raises while a reader holds the file open:

    console/state.json              sidecar writes, panel reads
    console/panel.json              panel writes, sidecar reads (liveness)
    console/prompts/<stamp>-<hex>.json   panel writes, sidecar consumes
    console/control/<stamp>-<hex>.json   panel writes, sidecar consumes
    console/sidecar.lock            sidecar only
    console/cost.json               sidecar only (daily spend across restarts)

Append only, never renamed, opened binary, one complete line per write + flush:

    console/events/<session>.jsonl  normalized events the panel renders
    console/raw/<session>.jsonl     every stdout line verbatim (redacted)
    logs/console_sidecar.log        human-readable log

Ids are timestamp-prefixed so a sorted directory listing is chronological; the
bridge learned that the hard way (reaper_agent_bridge.lua:3184-3188).

FACTS THIS FILE IS BUILT ON, all measured live on claude 2.1.225, not read
from docs. Changing any of them changes the code around it:

  * `result.total_cost_usd` is CUMULATIVE for the process. The per-turn figure
    is the difference between consecutive result events. Summing it inflates
    the running total without bound.
  * `system/init` is emitted PER TURN on the same session_id, and lazily: the
    child emits nothing at all until the first user message arrives. So "no
    init within N seconds of spawn" is not a failure, and stdin must never be
    closed as part of startup.
  * `--max-budget-usd` is advisory and LATCHING. It checks before a turn
    against cost already accrued, so it overshoots by one turn, and on
    exhaustion the process does not exit: it answers every later turn with
    subtype error_max_budget_usd in about 8 ms. Real enforcement is ours.
  * Reusing a minted `--session-id` fails with "Session ID <uuid> is already in
    use." on STDERR with ZERO stdout events. A stdout-only supervisor sees a
    silent empty stream, which is why stderr is read and retained here.
  * Hermeticity needs three flags. `--setting-sources ''` alone still loads 129
    tools and 7 remote connectors; `--strict-mcp-config` drops it to 32 and
    `--disable-slash-commands` zeroes skills. Together they also silence 13
    global hook events that would otherwise fire PowerShell on every tool call,
    inside a DAW.
  * REAPER blocks its own UI thread during captures (verifyloop.CAPTURE_TIMEOUT_MS
    is 180000). A 9.28 s block cost ~295 missed defer ticks and a 9.66 s gap in
    panel.json. Panel silence is therefore NOT evidence of death.

EXPLICIT NON-GOAL: there is no cache keepalive timer, and there will not be
one. Pre-warming a 25k-token prefix every 5 minutes for an 8 hour day costs
about $9 for zero work. An idle session costs nothing; one cold turn after
lunch costs pennies. The panel shows "cache cold" and we eat it.
"""

import argparse
import collections
import ctypes
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

SIDECAR_DIR = os.path.dirname(os.path.abspath(__file__))
if SIDECAR_DIR not in sys.path:
    sys.path.insert(0, SIDECAR_DIR)

import reaperd  # noqa: E402  (single-file CLI next to this script; import is safe)

# expanduser+abspath for the same reason reaperd.py:42 does it: a literal "~" in
# REAPER_DAEMON_ROOT used to silently create "./~/.../inbox" in the CWD.
BRIDGE_ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("REAPER_DAEMON_ROOT") or SIDECAR_DIR))

SCHEMA_VERSION = 1

# CreateProcess flag: pythonw.exe (how the panel launches us) has no console to
# inherit, so without this every child pops a console window over the DAW.
CREATE_NO_WINDOW = 0x08000000

# Windows OpenProcess access right that a non-elevated process can always ask
# for. PROCESS_QUERY_INFORMATION is refused across integrity levels; the
# _LIMITED_ variant is what liveness checks are supposed to use.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

DEFAULT_CONFIG = {
    "claude_path": None,
    "python_path": None,
    "model": "opus",
    "effort": "medium",
    "daily_budget_usd": 10.0,
    "session_budget_usd": None,
    "turn_warn_usd": 0.25,
    "cli_max_budget_usd": 25.0,
    "poll_interval": 0.25,
    "deadman_seconds": 300.0,
    "stdin_timeout": 20.0,
    "turn_timeout_seconds": 900.0,
    "max_queue_depth": 5,
    "event_line_cap": 2048,
    "retention_days": 7,
    "retention_bytes": 200 * 1024 * 1024,
    "quick_actions": [],
}

# The lock is confirmed one race window after it is claimed. 0.75 s is the same
# constant the bridge settled on (reaper_agent_bridge.lua:3215): comfortably
# past write-settling time, well under any refresh interval.
LOCK_CONFIRM_DELAY = 0.75
# A lock whose pid is gone is reclaimable, but a lock claimed microseconds ago
# by a sidecar that has not reached its first poll yet must not be stolen.
LOCK_FRESH_SECONDS = 10.0

# A single missing panel.json is not a death: the panel's atomic write has a
# remove window on Windows (reaper_agent_bridge.lua:182-190 documents the same
# fallback). Require several consecutive misses before the absence counts.
MIN_PANEL_MISSES = 3

# Three consecutive failing tool results means the model is looping against a
# broken bridge, and every loop is a full-context turn at Opus prices.
TOOL_ERROR_CIRCUIT_BREAK = 3

# Mutating calls waiting on a result. An interrupted turn abandons its calls
# mid-flight and no result ever arrives to retire them, so the parking lot is
# bounded rather than trusted to drain.
PENDING_CHANGE_CAP = 32

# A file written just AFTER a time.time() reading can carry an mtime just
# BEFORE it. Python 3.13 moved time.time() on Windows to the precise clock
# while NTFS still stamps from the coarse system tick, so the two disagree by
# up to the tick interval (measured ~1 ms on bare metal, and the classic
# Windows tick is 15.6 ms; a virtualized CI runner is worse). Without slack the
# inbox sweep skips exactly the command it exists to withdraw: the one the MCP
# child wrote microseconds into the turn. One second buries every plausible
# tick while staying far short of a command from another terminal.
MTIME_GRANULARITY_SLACK = 1.0

# Verbatim measurement fields. These are never folded into a summary string:
# a silent capture or an un-isolated scope changes what the numbers MEAN, and
# AGENTS.md:189-198 requires the caveat travel with them.
MEASURE_FIELDS = ("silent", "capture_scope", "isolation_verified",
                  "metrics_source")

# Secrets that must never reach console/raw/ or the event log. The auth token
# from bridge_config.json is added at runtime.
REDACT_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}"),
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
)
REDACTED = "[REDACTED]"


# ---------------------------------------------------------------------------
# Structured errors. Module boundaries return these instead of raising, so a
# panel poll or a control file can never take the daemon down (house style,
# same shape as reaperd.send_type / reaper_mcp._send).
# ---------------------------------------------------------------------------

def err(code, details):
    return {"ok": False, "error": {"code": code, "details": str(details)}}


def ok(**fields):
    out = {"ok": True}
    out.update(fields)
    return out


# ---------------------------------------------------------------------------
# Time / id helpers
# ---------------------------------------------------------------------------

def utc_iso(ts=None):
    """ISO-8601 Z, matching the bridge's os.date("!%Y-%m-%dT%H:%M:%SZ")."""
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_date(ts=None):
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc)
    return dt.strftime("%Y-%m-%d")


def stamped_id():
    """Timestamp-prefixed id so a sorted listing is chronological."""
    return (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-" + secrets.token_hex(4))


# ---------------------------------------------------------------------------
# Write classes. The distinction between them is load-bearing.
# ---------------------------------------------------------------------------

def atomic_write_json(path, value, attempts=20, delay=0.02):
    """`.tmp` + os.replace, retrying PermissionError.

    The repo already has this failure class on record (tests/bridge_fakes.py:33-41,
    reproduced live on windows-latest / Python 3.14): Windows raises when a
    reader holds the destination open, and the panel holds state.json open on
    a 200 ms timer for hours. A read-side retry alone does not cover the writer.
    Returns None on success, a structured error dict on failure.
    """
    tmp = path + ".tmp"
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    last = None
    for _ in range(max(1, attempts)):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, path)
            return None
        except PermissionError as e:
            last = e
            time.sleep(delay)
        except OSError as e:
            last = e
            break
    return err("ATOMIC_WRITE_FAILED", f"{path}: {last}")


def read_json(path, attempts=6, delay=0.02):
    """Read a JSON file written by somebody else's atomic replace.

    Tolerates the two transient states that replace leaves behind on Windows:
    a PermissionError while the rename settles, and (on a non-atomic writer) a
    half-written file that fails to parse. Returns None rather than raising:
    every caller here is a poll loop that must survive a bad tick.
    """
    for _ in range(max(1, attempts)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(delay)
        except ValueError:
            time.sleep(delay)
        except OSError:
            return None
    return None


def remove_file(path, attempts=20, delay=0.02):
    """Delete with the same transient-lock retry. True if it is gone."""
    for _ in range(max(1, attempts)):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            time.sleep(delay)
        except OSError:
            return False
    return False


# ---------------------------------------------------------------------------
# Redaction. Applied on the way into console/raw/ and into event text, because
# a full transcript of a session is exactly where a pasted key ends up.
# ---------------------------------------------------------------------------

def redact(text, extra=()):
    if not text:
        return text
    for value in extra:
        if value and len(str(value)) >= 4:
            text = text.replace(str(value), REDACTED)
    for pattern in REDACT_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


# ---------------------------------------------------------------------------
# Pure helpers. Everything below this line is a plain function with no threads,
# no filesystem and no subprocess, mirroring the bridge's `lock_verdict` which
# is pure precisely so it can be tested (reaper_agent_bridge.lua:234).
# ---------------------------------------------------------------------------

class LineSplitter:
    """Bytes in, complete decoded lines out, partial tail carried over.

    Both pipes are read in fixed-size chunks rather than with readline() so one
    multi-hundred-KB tool-result line cannot stall the other reader, and so a
    chunk boundary landing mid-line (or mid UTF-8 sequence) never corrupts a
    record. The carry buffer is the same idea the panel's tail reader uses.
    """

    def __init__(self):
        self.carry = b""

    def feed(self, chunk):
        if not chunk:
            return []
        data = self.carry + chunk
        parts = data.split(b"\n")
        self.carry = parts.pop()
        return [p.decode("utf-8", "replace").rstrip("\r") for p in parts]

    def flush(self):
        """Whatever is left when the pipe closes without a final newline."""
        if not self.carry:
            return []
        tail = self.carry.decode("utf-8", "replace").rstrip("\r")
        self.carry = b""
        return [tail] if tail else []


def cap_text(text, cap):
    """Trim one field so the panel never parses a 300 KB line on the UI thread.

    A scan_fx tool result is a single multi-hundred-KB line. The full text is
    always in console/raw/<session>.jsonl, so nothing is lost, only deferred.
    """
    if text is None:
        return None
    text = str(text)
    if cap is None or cap <= 0 or len(text) <= cap:
        return text
    return text[:cap] + f"... [+{len(text) - cap} chars, full text in console/raw/]"


def turn_cost_delta(prev_total, new_total):
    """Per-turn cost from two CUMULATIVE process totals.

    result.total_cost_usd is the running total for the whole child process, not
    the cost of this turn. Never sum it. A negative delta means the child was
    respawned (its counter restarted), so treat the new total as the cost.
    """
    try:
        prev = float(prev_total or 0.0)
        new = float(new_total or 0.0)
    except (TypeError, ValueError):
        return 0.0
    delta = new - prev
    if delta < 0:
        return max(0.0, new)
    return delta


def _blocks(message):
    """assistant/user message content as a list of blocks, string or list."""
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _result_text(block):
    """Flatten an MCP tool_result block's content into one string."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict):
                parts.append(json.dumps(item, separators=(",", ":")))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, separators=(",", ":"))


def tool_verdict(text, is_error):
    """Three-state verdict for a tool result, plus the verbatim measurement.

    verifyloop has a THREE state contract (EXIT_VERIFIED=0, EXIT_MUTATION_FAILED=1
    meaning REFUSED, EXIT_UNVERIFIED=2, verifyloop.py:435-437). UNVERIFIED is
    neither ok nor not-ok: the mutation WAS sent, the project MAY have changed,
    and nothing was measured. Collapsing that into ok:true is exactly what
    AGENTS.md:189-192 forbids, so `ok` stays None there and the panel has to
    render a third colour.

    Returns (verdict, ok_or_None, measurement_dict_or_None).
    """
    parsed = None
    if text:
        stripped = text.strip()
        if stripped[:1] in ("{", "["):
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None

    measurement = None
    if isinstance(parsed, dict):
        measurement = _measurement_fields(parsed)

    status = None
    if isinstance(parsed, dict):
        for holder in (parsed, parsed.get("data")):
            if isinstance(holder, dict) and isinstance(holder.get("status"), str):
                status = holder["status"].strip().upper()
                break

    if status == "VERIFIED":
        return "verified", True, measurement
    if status == "UNVERIFIED":
        # Deliberately ok=None. Not a success, not a clean failure.
        return "unverified", None, measurement
    if status == "REFUSED":
        return "refused", False, measurement

    if is_error:
        return "error", False, measurement
    if isinstance(parsed, dict) and "ok" in parsed and not parsed.get("ok"):
        return "error", False, measurement
    # reaper_mcp sets isError on every failure path, so a result that is not
    # flagged and carries no three-state status really is a plain success.
    return "ok", True, measurement


def _measurement_fields(obj):
    """Pull the honesty fields out verbatim, wherever they live."""
    if not isinstance(obj, dict):
        return None
    found = {}
    for holder in (obj, obj.get("data")):
        if not isinstance(holder, dict):
            continue
        for key in MEASURE_FIELDS:
            if key in holder and key not in found:
                found[key] = holder[key]
    for side in ("pre", "post", "baseline", "final"):
        sub = obj.get(side)
        if isinstance(sub, dict):
            subset = {k: sub[k] for k in MEASURE_FIELDS if k in sub}
            if subset:
                found[side] = subset
    return found or None


# --- the last change, for the audition loop ---------------------------------
#
# The audition loop exists to kill "which one did you mean". That only works if
# the change it offers to locate, A/B and undo is a change that really happened,
# so mutating tools are named here rather than guessed at from the result. A
# reader promoted by accident would point Locate at whatever the model last
# measured, which is worse than no button at all.

CHANGE_TOOLS = frozenset((
    "track", "fx", "set_fx_param", "write_automation", "markers",
    "insert_midi_file", "delete_items_in_range", "batch", "insert_groove",
    "verify_change", "tune_param",
))

# Actions on an otherwise-mutating tool that change nothing worth auditioning.
_NON_CHANGE_ACTIONS = {"track": frozenset(("select",))}

# Tools whose change is an FX doing something, so toggling that FX in and out is
# a true before/after. Everything else gets Locate and Undo but no A/B: an undo
# /redo A/B would be a lie the moment he makes an edit of his own between
# presses, and the undo stack is already shared with him.
_AB_FX_TOOLS = frozenset(("fx", "set_fx_param", "write_automation", "tune_param"))

# verify_change names the real mutation in `command_type` (bridge names, see
# bridge/command_schema.md:373-401), so its A/B is decided by the command it
# wrapped rather than by the wrapper.
_AB_FX_COMMANDS = frozenset(("add_fx", "bypass_fx", "set_fx_param",
                             "write_fx_param_automation"))


def _tool_base_name(name):
    """`mcp__reaper-daemon__set_fx_param` -> `set_fx_param`."""
    return str(name or "").rsplit("__", 1)[-1]


def _bar_of(position):
    """A position object -> a bar number, or None.

    Only `{"type": "bar"}` is a bar. A cursor or seconds position is real but
    the sidecar cannot convert it without REAPER's tempo map, and inventing the
    conversion here would put a second, divergent implementation of the bar math
    next to `reaper_focus.lua`'s.
    """
    if isinstance(position, dict) and position.get("type") == "bar":
        try:
            return int(position.get("bar"))
        except (TypeError, ValueError):
            return None
    return None


def _track_selector(args):
    """Pass the selector through verbatim; the panel resolves it against live
    REAPER. Resolving it here would need a bridge round trip per tool call."""
    for key in ("track", "track_contains"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return {"kind": key, "value": value.strip()}
    if args.get("use_selected_track"):
        return {"kind": "selected", "value": None}
    return None


def _change_bars(args, focus):
    """Bars the change touched, with provenance. `source` is not decoration:
    a range lifted from his time selection is where he was LOOKING, not where
    the tool said it wrote, and Locate must not present the two as the same
    claim."""
    points = args.get("points")
    if isinstance(points, list):
        numbers = [p.get("bar") for p in points
                   if isinstance(p, dict) and isinstance(p.get("bar"), (int, float))]
        if numbers:
            return {"start": int(min(numbers)), "end": int(max(numbers)),
                    "source": "tool"}

    start = None
    for key in ("position", "start", "range"):
        start = _bar_of(args.get(key))
        if start is not None:
            break
    if start is not None:
        end = _bar_of(args.get("end"))
        if end is None:
            length = args.get("length_bars")
            if isinstance(length, (int, float)) and length > 0:
                end = start + int(length) - 1
        return {"start": start, "end": end if end is not None else start,
                "source": "tool"}

    selection = (focus or {}).get("time_selection")
    if isinstance(selection, dict) and selection.get("start_bar"):
        return {"start": selection.get("start_bar"),
                "end": selection.get("end_bar") or selection.get("start_bar"),
                "source": "time_selection"}
    return None


def _change_summary(base, args, track, fx):
    """One producer-readable line. Track first: on a 40 track project that is
    the word he scans for."""
    parts = []
    if track:
        parts.append(track["value"] or "selected track")
    if fx:
        parts.append(fx)
    action = args.get("action")
    if isinstance(action, str) and action:
        parts.append(action)
    param = args.get("param_name_contains")
    if isinstance(param, str) and param:
        parts.append(param)
    value = args.get("formatted_value") or args.get("relative")
    if isinstance(value, str) and value:
        parts.append(value)
    elif isinstance(args.get("normalized_value"), (int, float)):
        parts.append(f"{args['normalized_value']:g} normalized")
    elif isinstance(args.get("volume_db"), (int, float)):
        parts.append(f"{args['volume_db']:+g} dB")
    if not parts:
        parts.append(base)
    return " · ".join(str(p) for p in parts)


def describe_change(name, tool_input, focus=None):
    """One mutating tool call -> the record the panel's audition strip renders.

    Pure. `tool_input` arrives as the JSON string the normalizer already capped,
    so an oversized batch parses as nothing and yields None. That is deliberate:
    a truncated record would still draw a Locate button, pointing at bars nobody
    asked for.
    """
    base = _tool_base_name(name)
    if base not in CHANGE_TOOLS:
        return None

    args = tool_input
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict):
        return None
    # A dry run changed nothing. Offering to undo it would undo his last real
    # edit instead.
    if args.get("dry_run"):
        return None

    action = args.get("action")
    if isinstance(action, str) and action in _NON_CHANGE_ACTIONS.get(base, ()):
        return None

    # verify_change carries the real mutation one level down, and tune_param's
    # selectors sit at the top. Merge so one extractor serves both, with the
    # outer keys winning: `track` is the GUID-pinned measurement target.
    merged = dict(args)
    payload = args.get("payload")
    if isinstance(payload, dict):
        for key, value in payload.items():
            merged.setdefault(key, value)

    track = _track_selector(merged)
    fx = None
    for key in ("fx_name_contains", "fx_name"):
        value = merged.get(key)
        if isinstance(value, str) and value.strip():
            fx = value.strip()
            break
    fx_index = merged.get("fx_index")
    fx_index = fx_index if isinstance(fx_index, int) else None

    record = {
        "tool": base,
        "summary": _change_summary(base, merged, track, fx),
        "track": track,
        "fx_name_contains": fx,
        "fx_index": fx_index,
        "bars": _change_bars(merged, focus),
    }

    command_type = args.get("command_type")
    ab_shaped = base in _AB_FX_TOOLS or (
        base == "verify_change" and command_type in _AB_FX_COMMANDS)

    if ab_shaped and action not in ("remove", "move"):
        if track and (fx or fx_index is not None):
            record["ab"] = "fx"
        else:
            record["ab_blocked"] = "no track and FX to toggle"
    elif base == "batch":
        record["ab_blocked"] = "batch: several changes in one undo block"
    elif base in ("insert_groove", "insert_midi_file"):
        record["ab_blocked"] = "inserted MIDI: use Undo to compare"
    elif action in ("remove", "move"):
        record["ab_blocked"] = f"FX {action}: nothing left to toggle"
    else:
        record["ab_blocked"] = "no FX to toggle on this change"
    return record


def normalize_stream_object(obj, cap=2048, prev_total_cost=0.0, ts=None):
    """One claude stdout object -> zero or more normalized panel events.

    The panel renders exactly these types and nothing else:
      user, text, text_delta, thinking, tool, tool_result, turn_end, error,
      session_end.
    Anything the CLI adds later is ignored here rather than leaking raw JSON
    into a Lua parser. Pure: no I/O, no state beyond the arguments.
    """
    ts = utc_iso(ts) if not isinstance(ts, str) else ts
    if not isinstance(obj, dict):
        return []
    kind = obj.get("type")

    if kind == "stream_event":
        event = obj.get("event") or {}
        if event.get("type") != "content_block_delta":
            return []
        delta = event.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            return [{"t": "text_delta", "ts": ts,
                     "text": cap_text(delta.get("text", ""), cap)}]
        if dtype == "thinking_delta":
            return [{"t": "thinking", "ts": ts, "delta": True,
                     "text": cap_text(delta.get("thinking", ""), cap)}]
        return []

    if kind == "assistant":
        events = []
        for block in _blocks(obj.get("message")):
            btype = block.get("type")
            if btype == "text":
                text = block.get("text") or ""
                if text.strip():
                    events.append({"t": "text", "ts": ts,
                                   "text": cap_text(text, cap)})
            elif btype == "thinking":
                events.append({"t": "thinking", "ts": ts, "delta": False,
                               "text": cap_text(block.get("thinking") or "", cap)})
            elif btype == "tool_use":
                events.append({
                    "t": "tool", "ts": ts,
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": cap_text(
                        json.dumps(block.get("input"), separators=(",", ":"),
                                   ensure_ascii=False, default=str), cap),
                })
        return events

    if kind == "user":
        # A user message on stdout is the tool-result carrier. The prompt echo
        # is emitted by the sidecar itself when it forwards, so text blocks
        # here are dropped rather than duplicated into the transcript.
        events = []
        for block in _blocks(obj.get("message")):
            if block.get("type") != "tool_result":
                continue
            text = _result_text(block)
            verdict, is_ok, measurement = tool_verdict(text, bool(block.get("is_error")))
            event = {"t": "tool_result", "ts": ts,
                     "id": block.get("tool_use_id"),
                     "verdict": verdict,
                     "ok": is_ok,
                     "text": cap_text(text, cap)}
            if measurement:
                event["measurement"] = measurement
            events.append(event)
        return events

    if kind == "result":
        subtype = obj.get("subtype")
        terminal = obj.get("terminal_reason")
        total = obj.get("total_cost_usd")
        event = {
            "t": "turn_end", "ts": ts,
            "subtype": subtype,
            "terminal_reason": terminal,
            "duration_ms": obj.get("duration_ms"),
            "num_turns": obj.get("num_turns"),
            "total_cost_usd": total,
            "turn_cost_usd": round(turn_cost_delta(prev_total_cost, total), 6),
            "usage": _usage_summary(obj.get("usage")),
        }
        # An interrupt stops the turn mid-stream and a result still arrives
        # with error_during_execution / aborted_streaming. That is "the user
        # cancelled", NOT an error to surface in red.
        if terminal == "aborted_streaming":
            event["cancelled"] = True
            event["error"] = False
        elif subtype == "error_max_budget_usd":
            event["error"] = True
            event["budget_exhausted"] = True
        else:
            event["error"] = bool(obj.get("is_error")) or (
                isinstance(subtype, str) and subtype.startswith("error"))
        if event.get("error") and obj.get("result"):
            event["text"] = cap_text(obj.get("result"), cap)
        return [event]

    return []


def _usage_summary(usage):
    """Token counts only. Never priced here: modelUsage is tokens, and the
    price table is not ours to guess at (plan, Phase 0b item 4)."""
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens")
    out = {k: usage[k] for k in keys if k in usage}
    return out or None


def lock_verdict(existing, now_epoch, pid_alive):
    """None = proceed (no lock, dead owner, or stale); str = refuse to start.

    Two sidecars on one console/ would both write state.json, both consume the
    same prompt files (whichever wins the race), and both bill. Same shape as
    the bridge's lock_verdict, kept pure for the same reason.
    """
    if not existing:
        return None
    if not isinstance(existing, dict):
        # An unparsable lock is not evidence of a live sidecar; the pid check
        # below is the real authority, and refusing forever on a corrupt file
        # is how the bridge once bricked itself (reaper_agent_bridge.lua:225-229).
        return None
    pid = existing.get("pid")
    started = existing.get("started")
    if isinstance(pid, int) and pid > 0 and pid_alive(pid):
        return (f"CONSOLE_ALREADY_RUNNING: console/sidecar.lock held by live pid "
                f"{pid} (started {started}, project {existing.get('project_name')})")
    try:
        age = now_epoch - float(started)
    except (TypeError, ValueError):
        return None
    if age < LOCK_FRESH_SECONDS:
        # The pid may simply not have been observable yet (claim-then-confirm
        # window). Refuse rather than race a sidecar that is mid-startup.
        return (f"CONSOLE_ALREADY_RUNNING: console/sidecar.lock claimed "
                f"{age:.1f}s ago by pid {pid}")
    return None


def deadman_verdict(now, panel_last_ok, panel_missing_since, panel_miss_count,
                    bridge_busy, turn_in_flight, deadman_seconds,
                    min_misses=MIN_PANEL_MISSES):
    """Compound predicate: (should_kill, reason). Pure.

    Panel silence alone is NOT death. Measured live: a 9.28 s REAPER block cost
    ~295 missed defer ticks and a 9.66 s gap in panel.json, and it scales
    linearly toward CAPTURE_TIMEOUT_MS (180 s). All three conditions must hold:

      1. panel.json stale for the full timeout,
      2. the bridge is not mid-render (its heartbeat says so before it blocks),
      3. no turn in flight (killing mid-turn strands MCP commands in inbox/).

    A MISSING panel.json is "unknown", not "dead": the panel's own atomic write
    has a remove window, so absence must also span the timeout and cover several
    consecutive polls before it counts.
    """
    if panel_last_ok is None:
        # Never seen a panel at all: headless / --once. Nothing to guard.
        return False, "no_panel_yet"
    if turn_in_flight:
        return False, "turn_in_flight"
    busy = bridge_busy or "none"
    if busy != "none":
        return False, f"bridge_busy_{busy}"
    if (now - panel_last_ok) < deadman_seconds:
        return False, "panel_fresh"
    if panel_missing_since is not None:
        if panel_miss_count < min_misses:
            return False, "panel_missing_briefly"
        if (now - panel_missing_since) < deadman_seconds:
            return False, "panel_missing_window"
    return True, "panel_stale"


def budget_gate(daily_cost, daily_budget, session_cost, session_budget,
                last_turn_cost, turn_warn_usd, warn_acknowledged,
                bridge_ok, status, queue_depth=0, max_queue_depth=None):
    """(allowed, code, details) for forwarding one prompt. Pure.

    Order matters: the cheapest and most certain refusals first. The bridge
    check is the single cheapest guard in the whole design. With a stale bridge
    every MCP call burns a full timeout, the model retries, and each retry is a
    full-context Opus turn charged in full for a result that cannot change.
    """
    if status == "budget_exhausted":
        return False, "BUDGET_EXHAUSTED", (
            "the CLI latched error_max_budget_usd; resume the session to recover")
    if status == "child_exited":
        return False, "CHILD_EXITED", "the claude process is not running"
    if not bridge_ok:
        return False, "BRIDGE_STALE", (
            "the REAPER bridge heartbeat is stale or dead; every tool call would "
            "burn a full timeout and the model would retry at full turn cost")
    if daily_budget is not None and daily_cost >= daily_budget:
        return False, "DAILY_BUDGET_EXCEEDED", (
            f"${daily_cost:.2f} spent today against a ${daily_budget:.2f} cap")
    if session_budget is not None and session_cost >= session_budget:
        return False, "SESSION_BUDGET_EXCEEDED", (
            f"${session_cost:.2f} spent this session against a ${session_budget:.2f} cap")
    if (turn_warn_usd is not None and not warn_acknowledged
            and last_turn_cost is not None and last_turn_cost > turn_warn_usd):
        # There is no cheap mid-turn ceiling, so the gate is pre-flight on the
        # NEXT turn. The panel clears it with an ack_warn control file.
        return False, "TURN_WARN_UNACKNOWLEDGED", (
            f"the last turn cost ${last_turn_cost:.2f} (> ${turn_warn_usd:.2f}); "
            "acknowledge before sending another")
    if max_queue_depth is not None and queue_depth >= max_queue_depth:
        return False, "QUEUE_FULL", f"{queue_depth} prompts already queued"
    return True, None, None


def sweep_plan(entries, now, max_age_seconds, max_bytes, protect=()):
    """Which retention files to delete. entries: [(path, mtime, size)]. Pure.

    Same shape as the bridge's sweep_once (reaper_agent_bridge.lua:3172-3198):
    age first, then a total-size cap dropping oldest first. The active session's
    files are protected: a sweep that deleted the file the panel is tailing
    would look exactly like a crash.
    """
    protected = set(os.path.normcase(os.path.abspath(p)) for p in protect)
    live = []
    doomed = []
    for path, mtime, size in entries:
        if os.path.normcase(os.path.abspath(path)) in protected:
            continue
        if max_age_seconds is not None and (now - mtime) > max_age_seconds:
            doomed.append(path)
        else:
            live.append((path, mtime, size))
    if max_bytes is not None:
        live.sort(key=lambda e: e[1])  # oldest first
        total = sum(e[2] for e in live)
        while live and total > max_bytes:
            path, _mtime, size = live.pop(0)
            doomed.append(path)
            total -= size
    return doomed


def resolve_claude_path(candidate, isfile=os.path.isfile):
    """Resolve to a real executable, refusing a .cmd/.bat/.ps1 shim.

    npm-style shims spawn node as a CHILD. proc.kill() then kills the shim and
    leaves node alive holding a paid session that keeps answering nobody. A
    sibling .exe is used when one exists; otherwise this fails closed, because
    a sidecar that cannot kill its child cannot enforce a budget.
    Returns (path, None) or (None, error_dict).
    """
    if not candidate:
        return None, err("CLAUDE_NOT_FOUND",
                         "claude executable not found; set claude_path in "
                         "console_config.json")
    candidate = os.path.abspath(os.path.expanduser(str(candidate)))
    stem, ext = os.path.splitext(candidate)
    if ext.lower() in (".cmd", ".bat", ".ps1", ".sh"):
        for alt_ext in (".exe", ""):
            alt = stem + alt_ext
            if alt != candidate and isfile(alt):
                return alt, None
        return None, err("CLAUDE_SHIM_UNSUPPORTED",
                         f"{candidate} is a shell shim, not an executable. "
                         "Killing a shim leaves the node process alive holding a "
                         "paid session. Point claude_path at the real binary.")
    if not isfile(candidate):
        return None, err("CLAUDE_NOT_FOUND", f"{candidate} does not exist")
    return candidate, None


def mcp_config_json(root, python_path):
    """The --mcp-config payload. No secret ever goes in here: argv is visible
    in any local process listing, and the bridge auth token is read by
    reaper_mcp.py from bridge/bridge_config.json instead."""
    return json.dumps({"mcpServers": {"reaper-daemon": {
        "type": "stdio",
        "command": python_path,
        "args": [os.path.join(root, "reaper_mcp.py")],
        "env": {"REAPER_DAEMON_ROOT": root,
                "REAPER_DAEMON_CONSOLE_MODE": "1"},
    }}}, separators=(",", ":"))


def build_argv(claude_path, root, config, session_id=None, resume_id=None,
               contract_path=None, python_path=None):
    """The verified-working launch line. Pure, so the flag set is testable.

    Every flag here was checked live on 2.1.225. Notably:
      --setting-sources '' alone is NOT hermetic (129 tools, 7 remote
      connectors); --strict-mcp-config takes it to 32 and
      --disable-slash-commands zeroes skills. All three or none.
      --disallowedTools AskUserQuestion because that one call falls through
      even under bypassPermissions and would hang forever waiting for a human
      who is looking at a DAW, not a terminal.
      --max-budget-usd is a BACKSTOP only, set above our own cap: it overshoots
      by one turn and then latches the session into uselessness.
    """
    argv = [claude_path, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages"]
    if resume_id:
        argv += ["--resume", resume_id]
    else:
        argv += ["--session-id", session_id]
    argv += ["--setting-sources", "",
             "--strict-mcp-config",
             "--disable-slash-commands",
             "--model", str(config.get("model") or "opus"),
             "--effort", str(config.get("effort") or "medium"),
             "--permission-mode", "bypassPermissions",
             "--disallowedTools", "AskUserQuestion",
             "--add-dir", root,
             "--mcp-config", mcp_config_json(root, python_path or sys.executable)]
    if contract_path:
        argv += ["--append-system-prompt-file", contract_path]
    cap = config.get("cli_max_budget_usd")
    if cap:
        argv += ["--max-budget-usd", str(cap)]
    return argv


def child_env(root, base=None):
    """ENABLE_TOOL_SEARCH=0 inlines all 21 MCP schemas. Without it the CLI
    defers tool schemas above ~50 tools and the model burns a whole turn on
    ToolSearch before it can reach a REAPER tool. Correct for a persistent
    session (the schema cost is paid once and amortized), wrong for one-shots."""
    env = dict(os.environ if base is None else base)
    env["ENABLE_TOOL_SEARCH"] = "0"
    env["REAPER_DAEMON_ROOT"] = root
    return env


def user_message_line(session_id, text):
    """One turn per stdin line, in the shape --input-format stream-json wants."""
    return {"type": "user",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]},
            "parent_tool_use_id": None,
            "session_id": session_id}


# ---------------------------------------------------------------------------
# Process liveness and tree kill
# ---------------------------------------------------------------------------

def pid_alive(pid):
    """True if the pid is a running process.

    NOT os.kill(pid, 0) on Windows: CPython maps os.kill for any signal other
    than the console-control events onto TerminateProcess, so the POSIX
    "signal 0 is a liveness probe" idiom would KILL the process it is asking
    about. ctypes is stdlib, so this stays inside the no-dependency rule.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by somebody else
        except OSError:
            return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def kill_tree(proc, timeout=5.0):
    """Kill the child AND its descendants.

    claude.exe spawns the MCP server (python reaper_mcp.py) as a grandchild.
    Killing only the direct child leaves that python holding the bridge queue,
    and on Windows leaves node alive holding a paid session. taskkill /F /T is
    the only tree kill Windows offers without a job object; on POSIX the child
    is started with start_new_session=True so the process GROUP can be signalled.
    Returns a structured error on failure, None on success.
    """
    if proc is None or proc.poll() is not None:
        return None
    pid = proc.pid
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        else:
            import signal
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
    except Exception as e:  # noqa: BLE001 - a failed kill must not crash shutdown
        try:
            proc.kill()
        except OSError:
            return err("KILL_FAILED", f"pid {pid}: {e}")
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return err("KILL_TIMEOUT", f"pid {pid} still running after {timeout}s")
    return None


# ---------------------------------------------------------------------------
# The sidecar
# ---------------------------------------------------------------------------

class ConsoleSidecar:
    """Owns one claude child, the console/ file protocol, and the money rules."""

    def __init__(self, root=None, config=None, claude_argv=None,
                 poll_interval=None, deadman_seconds=None, stdin_timeout=None,
                 turn_timeout=None, emit=None, contract_path=None,
                 take_lock=True, bridge_cache_seconds=2.0):
        self.root = os.path.abspath(root or BRIDGE_ROOT)
        self.config = dict(DEFAULT_CONFIG)
        self.config.update(load_config(self.root))
        if config:
            self.config.update(config)

        # Injectable so tests can drive the whole daemon at 10 ms instead of
        # 300 s. Every timing constant in the loop comes from here.
        self.poll_interval = float(
            poll_interval if poll_interval is not None else self.config["poll_interval"])
        self.deadman_seconds = float(
            deadman_seconds if deadman_seconds is not None else self.config["deadman_seconds"])
        self.stdin_timeout = float(
            stdin_timeout if stdin_timeout is not None else self.config["stdin_timeout"])
        self.turn_timeout = float(
            turn_timeout if turn_timeout is not None else self.config["turn_timeout_seconds"])
        # status_ok shells out to tasklist/pgrep when the heartbeat is missing
        # or stale, so the probe is cached. Tests inject 0 to read every tick.
        self.bridge_cache_seconds = float(bridge_cache_seconds)

        self.claude_argv = claude_argv  # tests inject [sys.executable, fake.py]
        self.contract_path = contract_path or os.path.join(
            self.root, "docs", "console_contract.md")
        self.take_lock = take_lock
        self.emit = emit  # optional callback(event) for --once / tests

        self.console = os.path.join(self.root, "console")
        self.dirs = {
            "prompts": os.path.join(self.console, "prompts"),
            "control": os.path.join(self.console, "control"),
            "events": os.path.join(self.console, "events"),
            "raw": os.path.join(self.console, "raw"),
            "audio": os.path.join(self.console, "audio"),
        }
        for path in self.dirs.values():
            os.makedirs(path, exist_ok=True)
        os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)

        self.lock_path = os.path.join(self.console, "sidecar.lock")
        self.state_path = os.path.join(self.console, "state.json")
        self.panel_path = os.path.join(self.console, "panel.json")
        self.cost_path = os.path.join(self.console, "cost.json")
        self.log_path = os.path.join(self.root, "logs", "console_sidecar.log")
        self.owner = f"{os.getpid()}-{secrets.token_hex(6)}"

        self.proc = None
        self.session_id = None
        self.events_path = None
        self.raw_path = None
        self._events_fh = None
        self._raw_fh = None
        self._file_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self.status = "starting"
        self.capabilities = set()
        self.child_tools = None
        self.turn_in_flight = False
        self.turn_started = None
        self.turn_seq = 0
        self.queue = []
        self.stderr_tail = []          # retained: the silent-stdout collision
        self.last_error = None
        self.prev_total_cost = 0.0
        self.turn_cost = 0.0
        self.session_cost = 0.0
        self.last_turn_cost = None
        self.warn_acknowledged = True
        self.consec_tool_errors = 0
        # The audition loop's subject. `turn_focus` is the envelope that came
        # WITH the running prompt, not the live one: by the time a tool result
        # lands he may have clicked another track, and the change belongs to
        # what he was looking at when he asked.
        self.last_change = None
        self.turn_focus = None
        self._pending_changes = collections.OrderedDict()
        self.cache_cold = True
        self.turns = 0
        self.started_at = time.time()
        self.stop_reason = None
        self._stopping = threading.Event()
        self._control_seq = 0
        self._control_waiters = {}
        self._control_results = {}
        self._result_event = threading.Event()
        self._readers = []
        self._stderr_thread = None
        self._auth_token = reaperd._auth_token(self.root)

        self.panel_last_ok = None
        self.panel_missing_since = None
        self.panel_miss_count = 0
        self.bridge_ok = False
        self.bridge_busy = "none"
        self.project_name = None
        self._bridge_checked_at = 0.0
        self._last_sweep = 0.0

        self.daily_cost = 0.0
        self.daily_reset_utc = utc_date()
        self._load_daily_cost()

    # -- logging ----------------------------------------------------------

    def log(self, msg, level="INFO"):
        line = f"[{utc_iso()}] {level} {msg}\n"
        with self._log_lock:
            try:
                with open(self.log_path, "ab") as f:
                    f.write(redact(line, (self._auth_token,)).encode("utf-8", "replace"))
                    f.flush()
            except OSError:
                pass

    # -- lock -------------------------------------------------------------

    def claim_lock(self, confirm_delay=LOCK_CONFIRM_DELAY):
        """Claim-then-confirm singleton, same protocol as the bridge lock
        (reaper_agent_bridge.lua:164-253) but a DIFFERENT file. logs/bridge.lock
        is the bridge's and is never touched here.

        There is no atomic O_EXCL create that also survives a stale lock, so the
        read->reclaim->write has a TOCTOU window: two sidecars starting in the
        same tick both see the stale lock and both write. Each stamps a unique
        owner token, then re-reads one race window later; exactly one sees its
        own token and keeps running.
        """
        verdict = lock_verdict(read_json(self.lock_path), time.time(), pid_alive)
        if verdict:
            return err("CONSOLE_ALREADY_RUNNING", verdict)
        payload = {"pid": os.getpid(), "owner": self.owner,
                   "started": time.time(), "started_iso": utc_iso(),
                   "project_name": self.project_name or self._peek_project_name()}
        failure = atomic_write_json(self.lock_path, payload)
        if failure:
            return failure
        time.sleep(confirm_delay)
        held = read_json(self.lock_path)
        if not isinstance(held, dict) or held.get("owner") != self.owner:
            return err("CONSOLE_RACE_LOST",
                       f"console/sidecar.lock is owned by {held.get('owner') if isinstance(held, dict) else held}")
        return None

    def release_lock(self):
        held = read_json(self.lock_path)
        if isinstance(held, dict) and held.get("owner") == self.owner:
            remove_file(self.lock_path)

    def _peek_project_name(self):
        hb = read_json(os.path.join(self.root, "bridge", "heartbeat.json"))
        return hb.get("project_name") if isinstance(hb, dict) else None

    # -- daily cost persistence ------------------------------------------

    def _load_daily_cost(self):
        """--max-budget-usd is per process, and the panel's Restart button would
        otherwise zero the day's spend. The daily figure lives on disk."""
        data = read_json(self.cost_path)
        today = utc_date()
        if isinstance(data, dict) and data.get("daily_reset_utc") == today:
            try:
                self.daily_cost = float(data.get("daily_cost_usd") or 0.0)
            except (TypeError, ValueError):
                self.daily_cost = 0.0
            self.daily_reset_utc = today
        else:
            self.daily_cost = 0.0
            self.daily_reset_utc = today

    def _save_daily_cost(self):
        atomic_write_json(self.cost_path, {
            "daily_cost_usd": round(self.daily_cost, 6),
            "daily_reset_utc": self.daily_reset_utc,
            "updated_at": utc_iso()})

    def _roll_day_if_needed(self):
        today = utc_date()
        if today != self.daily_reset_utc:
            self.daily_reset_utc = today
            self.daily_cost = 0.0
            self._save_daily_cost()

    # -- append-only writers ----------------------------------------------

    def _open_session_files(self, session_id):
        with self._file_lock:
            self._close_session_files_locked()
            self.session_id = session_id
            self.events_path = os.path.join(self.dirs["events"], f"{session_id}.jsonl")
            self.raw_path = os.path.join(self.dirs["raw"], f"{session_id}.jsonl")
            # Binary append: two processes appending text to one file on Windows
            # interleave mid-line, and the panel parses whole lines only.
            self._events_fh = open(self.events_path, "ab")
            self._raw_fh = open(self.raw_path, "ab")

    def _close_session_files_locked(self):
        for attr in ("_events_fh", "_raw_fh"):
            fh = getattr(self, attr, None)
            if fh is not None:
                try:
                    fh.flush()
                    fh.close()
                except OSError:
                    pass
                setattr(self, attr, None)

    def write_event(self, event):
        """One complete line, one write, one flush. Never renamed, never
        truncated in place: the panel tails this file by byte offset."""
        data = (json.dumps(event, separators=(",", ":"), ensure_ascii=False,
                           default=str) + "\n")
        data = redact(data, (self._auth_token,)).encode("utf-8", "replace")
        with self._file_lock:
            if self._events_fh is not None:
                try:
                    self._events_fh.write(data)
                    self._events_fh.flush()
                except OSError as e:
                    self.log(f"event write failed: {e}", "WARN")
        if self.emit:
            try:
                self.emit(event)
            except Exception:  # noqa: BLE001 - a bad sink must not kill the loop
                pass

    def write_raw(self, line):
        payload = redact(line, (self._auth_token,))
        with self._file_lock:
            if self._raw_fh is not None:
                try:
                    self._raw_fh.write((payload + "\n").encode("utf-8", "replace"))
                    self._raw_fh.flush()
                except OSError:
                    pass

    # -- state.json --------------------------------------------------------

    def publish_state(self):
        with self._state_lock:
            state = {
                "schema": SCHEMA_VERSION,
                "pid": os.getpid(),
                "owner": self.owner,
                "session_id": self.session_id,
                "status": self.status,
                "model": self.config.get("model"),
                "effort": self.config.get("effort"),
                "turn_in_flight": self.turn_in_flight,
                "turn_started_at": utc_iso(self.turn_started) if self.turn_started else None,
                "queue_depth": len(self.queue),
                "max_queue_depth": self.config.get("max_queue_depth"),
                "turns": self.turns,
                "turn_cost_usd": round(self.turn_cost, 6),
                "session_cost_usd": round(self.session_cost, 6),
                "daily_cost_usd": round(self.daily_cost, 6),
                "daily_reset_utc": self.daily_reset_utc,
                "daily_budget_usd": self.config.get("daily_budget_usd"),
                "session_budget_usd": self.config.get("session_budget_usd"),
                "turn_warn_usd": self.config.get("turn_warn_usd"),
                "warn_pending": not self.warn_acknowledged,
                "cache_cold": self.cache_cold,
                "capabilities": sorted(self.capabilities),
                "tool_count": self.child_tools,
                "events_file": (os.path.relpath(self.events_path, self.root).replace("\\", "/")
                                if self.events_path else None),
                "bridge_ok": self.bridge_ok,
                "bridge_busy": self.bridge_busy,
                "project_name": self.project_name,
                "last_error": self.last_error,
                "stderr_tail": self.stderr_tail[-5:],
                "quick_actions": self.config.get("quick_actions") or [],
                "last_change": self.last_change,
                "started_at": utc_iso(self.started_at),
                "updated_at": utc_iso(),
            }
            failure = atomic_write_json(self.state_path, state)
            if failure:
                self.log(f"state publish failed: {failure['error']['details']}", "WARN")

    # -- child lifecycle ---------------------------------------------------

    def spawn(self, resume_id=None):
        """Start claude.exe. Returns None or a structured error.

        Nothing is written to stdin here and stdin is NEVER closed: init is
        lazy, so a child that has said nothing is normal, and closing stdin
        ends the session instead of starting it.
        """
        if self.claude_argv:
            argv = list(self.claude_argv)
            session_id = resume_id or str(uuid.uuid4())
        else:
            path, failure = resolve_claude_path(
                self.config.get("claude_path") or shutil.which("claude"))
            if failure:
                return failure
            session_id = resume_id or str(uuid.uuid4())
            contract = self.contract_path if os.path.isfile(self.contract_path) else None
            argv = build_argv(path, self.root, self.config,
                              session_id=session_id, resume_id=resume_id,
                              contract_path=contract,
                              python_path=self.config.get("python_path") or sys.executable)

        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": self.root,
            "env": child_env(self.root),
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            # A new session gives the child its own process group, which is the
            # only way to kill the MCP grandchild on POSIX without walking /proc.
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(argv, **kwargs)
        except OSError as e:
            return err("SPAWN_FAILED", f"{argv[0]}: {e}")

        self._open_session_files(session_id)
        self.prev_total_cost = 0.0  # per-process counter restarts with the process
        self.capabilities = set()
        self.status = "idle"
        self.cache_cold = True
        self._stopping.clear()
        self.log(f"spawned pid {self.proc.pid} session {session_id}"
                 + (" (resume)" if resume_id else ""))
        self._stderr_thread = threading.Thread(target=self._read_stderr,
                                               daemon=True, name="console-stderr")
        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True,
                             name="console-stdout"),
            self._stderr_thread,
        ]
        for thread in self._readers:
            thread.start()
        return None

    def _read_stdout(self):
        proc = self.proc
        splitter = LineSplitter()
        stream = proc.stdout
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                for line in splitter.feed(chunk):
                    self._handle_stdout_line(line)
            for line in splitter.flush():
                self._handle_stdout_line(line)
        except (OSError, ValueError):
            pass
        finally:
            self._on_child_stream_end(proc)

    def _read_stderr(self):
        """A separate thread, not a merged pipe.

        Two reasons, both real. (1) 64 KB of unread stderr blocks the child's
        write and deadlocks a supervisor that is only draining stdout.
        (2) `Session ID <uuid> is already in use.` arrives on stderr with ZERO
        stdout events, so without this the failure is a silent empty stream and
        the operator has nothing to read. The tail is retained for state.json.
        """
        proc = self.proc
        splitter = LineSplitter()
        stream = proc.stderr
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                for line in splitter.feed(chunk):
                    self._record_stderr(line)
            for line in splitter.flush():
                self._record_stderr(line)
        except (OSError, ValueError):
            pass

    def _record_stderr(self, line):
        if not line.strip():
            return
        self.log("child stderr: " + line, "CHILD")
        self.stderr_tail.append(cap_text(line, 400))
        if len(self.stderr_tail) > 50:
            del self.stderr_tail[:-50]
        if "already in use" in line:
            self.last_error = {"code": "SESSION_ID_IN_USE", "details": line}

    def _handle_stdout_line(self, line):
        if not line.strip():
            return
        self.write_raw(line)
        try:
            obj = json.loads(line)
        except ValueError:
            # A malformed line is tolerated, never fatal: the CLI interleaves
            # its own diagnostics into stdout on some paths, and one bad line
            # must not end a paid session.
            self.log("unparsable stdout line: " + cap_text(line, 200), "WARN")
            return
        if not isinstance(obj, dict):
            return

        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            self._on_init(obj)
            return
        if kind == "control_request":
            self._answer_control_request(obj)
            return
        if kind == "control_response":
            self._on_control_response(obj)
            return

        events = normalize_stream_object(
            obj, cap=self.config.get("event_line_cap"),
            prev_total_cost=self.prev_total_cost)
        for event in events:
            if event["t"] == "tool":
                self._on_tool_call(event)
            if event["t"] == "tool_result":
                self._on_tool_result(event)
            self.write_event(event)
            if event["t"] == "turn_end":
                self._on_turn_end(obj, event)

    def _on_init(self, obj):
        """system/init arrives PER TURN, not per session, on the same
        session_id. Keying state off the init event instead of session_id would
        corrupt the state machine on the second turn."""
        sid = obj.get("session_id")
        caps = obj.get("capabilities") or []
        if isinstance(caps, dict):
            caps = [k for k, v in caps.items() if v]
        self.capabilities = set(str(c) for c in caps)
        tools = obj.get("tools")
        if isinstance(tools, list):
            self.child_tools = len(tools)
        if sid and self.session_id and sid != self.session_id:
            # Not fatal, but worth a loud log: the id we minted is the one the
            # panel's events file is named after.
            self.log(f"child reports session {sid}, expected {self.session_id}", "WARN")
        self.cache_cold = False
        self.publish_state()

    def _answer_control_request(self, obj):
        """Always answer, even with an error. Silently dropping a control
        request makes the CLI block forever with no visible failure."""
        rid = obj.get("request_id")
        request = obj.get("request") or {}
        subtype = request.get("subtype")
        if subtype == "can_use_tool":
            # Under bypassPermissions this never arrives (measured: zero
            # permission traffic, empty permission_denials). Answering allow
            # anyway keeps a future permission mode from hanging the DAW.
            response = {"subtype": "success", "request_id": rid,
                        "response": {"behavior": "allow",
                                     "updatedInput": request.get("input")}}
        elif subtype in ("initialize", "set_permission_mode", "hook_callback",
                         "mcp_message"):
            response = {"subtype": "success", "request_id": rid, "response": {}}
        else:
            response = {"subtype": "error", "request_id": rid,
                        "error": f"unsupported control_request subtype {subtype!r}"}
        self._write_stdin({"type": "control_response", "response": response})

    def _on_control_response(self, obj):
        response = obj.get("response") or {}
        rid = response.get("request_id") or obj.get("request_id")
        self._control_results[rid] = response
        waiter = self._control_waiters.get(rid)
        if waiter is not None:
            waiter.set()

    def _on_tool_call(self, event):
        """Park a mutating call until its result says whether it landed. A tool
        call is a request, not a change: promoting it here would let a refused
        edit arm the Undo button."""
        record = describe_change(event.get("name"), event.get("input"),
                                 self.turn_focus)
        if record is None:
            return
        record["id"] = event.get("id")
        record["ts"] = event.get("ts")
        self._pending_changes[event.get("id")] = record
        # Bounded. An interrupted turn leaves calls that no result will ever
        # answer, and this dict would otherwise grow for the life of the daemon.
        while len(self._pending_changes) > PENDING_CHANGE_CAP:
            self._pending_changes.popitem(last=False)

    def _promote_change(self, event):
        record = self._pending_changes.pop(event.get("id"), None)
        if record is None:
            return
        # ok is None for UNVERIFIED, and that is exactly a change he wants to
        # hear: the mutation was sent, nothing measured it. Only an outright
        # false — error or refused — means nothing happened.
        if event.get("ok") is False:
            return
        record["verdict"] = event.get("verdict")
        record["measured"] = event.get("ok") is True and bool(event.get("measurement"))
        self.last_change = record
        self.publish_state()

    def _on_tool_result(self, event):
        """Circuit breaker. Three consecutive failing tool results means the
        model is looping against something broken, and every loop is a full
        Opus turn charged in full. `unverified` deliberately does NOT count as
        an error: the mutation went through and the model must be free to
        measure it."""
        self._promote_change(event)
        if event.get("verdict") in ("error", "refused"):
            self.consec_tool_errors += 1
            if self.consec_tool_errors >= TOOL_ERROR_CIRCUIT_BREAK and self.turn_in_flight:
                self.log("circuit break: 3 consecutive failing tool results", "WARN")
                self.write_event({"t": "error", "ts": utc_iso(),
                                  "code": "TOOL_ERROR_CIRCUIT_BREAK",
                                  "details": ("three tool calls in a row failed; "
                                              "the turn was interrupted")})
                # wait=False: this runs on the stdout reader thread.
                self.interrupt(wait=False)
        else:
            self.consec_tool_errors = 0

    def _on_turn_end(self, raw, event):
        total = raw.get("total_cost_usd")
        self.turn_cost = float(event.get("turn_cost_usd") or 0.0)
        try:
            self.prev_total_cost = float(total or self.prev_total_cost)
        except (TypeError, ValueError):
            pass
        # Accumulate turn deltas rather than copying the child's cumulative
        # figure: a --resume respawn restarts the child's counter at zero, and
        # the session's real spend does not.
        self.session_cost += self.turn_cost
        self._roll_day_if_needed()
        self.daily_cost += self.turn_cost
        self._save_daily_cost()
        self.last_turn_cost = self.turn_cost
        warn = self.config.get("turn_warn_usd")
        self.warn_acknowledged = not (warn is not None and self.turn_cost > warn)
        self.turns += 1
        self.turn_in_flight = False
        self.turn_started = None
        self.consec_tool_errors = 0
        if event.get("budget_exhausted"):
            # Distinct, latched state. The process stays ALIVE and answers
            # everything afterwards in ~8 ms with the same error, so pretending
            # the session still works would burn the panel's Send button.
            self.status = "budget_exhausted"
            self.last_error = {"code": "BUDGET_EXHAUSTED",
                               "details": "CLI --max-budget-usd latched; resume to recover"}
        else:
            self.status = "idle"
        # Publish BEFORE signalling. --once returns the moment this event fires
        # and then publishes "stopped", so a state file written afterwards
        # would skip the finished turn entirely and the panel would never see
        # the cost of the turn it just paid for.
        self.publish_state()
        self._result_event.set()

    def _on_child_stream_end(self, proc):
        if proc is not self.proc:
            return
        code = proc.poll()
        if self._stopping.is_set():
            return
        # Let the stderr reader finish draining before composing the diagnosis.
        # The whole point of the second pipe is the collision case, where the
        # child dies with `Session ID <uuid> is already in use.` on stderr and
        # ZERO stdout events; reporting "exited, no output" while the
        # explanation is still in flight would waste the one clue there is.
        thread = getattr(self, "_stderr_thread", None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.status = "child_exited"
        self.turn_in_flight = False
        detail = f"claude exited with code {code}"
        if self.stderr_tail:
            detail += "; stderr: " + " | ".join(self.stderr_tail[-3:])
        self.last_error = {"code": "CHILD_EXITED", "details": detail}
        self.log(detail, "ERROR")
        self.write_event({"t": "session_end", "ts": utc_iso(),
                          "reason": "child_exited", "exit_code": code,
                          "stderr_tail": self.stderr_tail[-3:]})
        self.publish_state()
        self._result_event.set()

    # -- stdin -------------------------------------------------------------

    def _write_stdin(self, obj):
        """ONE lock around every stdin write, and one write of line + "\\n".

        A prompt and an interrupt interleaving mid-line produces a JSON parse
        error inside the CLI and silently drops both.
        """
        line = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False,
                           default=str) + "\n").encode("utf-8")
        with self._stdin_lock:
            proc = self.proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                return err("CHILD_NOT_RUNNING", "no live claude process")
            try:
                # bufsize=0 makes stdin a raw FileIO, whose write() may consume
                # only part of the buffer on a pipe. A short write here would
                # cut a prompt in half and the CLI would drop both halves, so
                # push until every byte is gone.
                view = memoryview(line)
                while view:
                    written = proc.stdin.write(view)
                    if not written:
                        return err("STDIN_WRITE_FAILED", "zero-length write")
                    view = view[written:]
                proc.stdin.flush()
            except (OSError, ValueError) as e:
                return err("STDIN_WRITE_FAILED", str(e))
        return None

    # -- turns -------------------------------------------------------------

    def check_bridge(self, max_age=None):
        """Bridge liveness, reusing reaperd.status_ok so BUSY handling stays in
        exactly one place (reaperd.py:462-513): a synchronous render blocks the
        bridge loop on purpose and is NOT a death. Cached briefly because
        status_ok shells out to tasklist when the heartbeat is missing."""
        if max_age is None:
            max_age = self.bridge_cache_seconds
        now = time.time()
        if now - self._bridge_checked_at < max_age:
            return self.bridge_ok
        self._bridge_checked_at = now
        hb = read_json(os.path.join(self.root, "bridge", "heartbeat.json"))
        if isinstance(hb, dict):
            self.bridge_busy = hb.get("busy") or "none"
            self.project_name = hb.get("project_name")
        else:
            self.bridge_busy = "none"
        try:
            self.bridge_ok = bool(reaperd.status_ok(self.root, quiet=True))
        except Exception as e:  # noqa: BLE001 - a probe must never kill the daemon
            self.log(f"bridge probe failed: {e}", "WARN")
            self.bridge_ok = False
        return self.bridge_ok

    def gate(self):
        return budget_gate(
            daily_cost=self.daily_cost,
            daily_budget=self.config.get("daily_budget_usd"),
            session_cost=self.session_cost,
            session_budget=self.config.get("session_budget_usd"),
            last_turn_cost=self.last_turn_cost,
            turn_warn_usd=self.config.get("turn_warn_usd"),
            warn_acknowledged=self.warn_acknowledged,
            bridge_ok=self.check_bridge(),
            status=self.status)

    def enqueue(self, text, source="panel", focus=None):
        """A Send during a live turn is QUEUED, never forwarded. Two user
        messages on one stdin during a turn is undefined behaviour in the CLI
        and the second one silently vanished in the spike."""
        max_depth = self.config.get("max_queue_depth")
        if max_depth is not None and len(self.queue) >= max_depth:
            failure = err("QUEUE_FULL", f"{len(self.queue)} prompts already queued")
            self.write_event({"t": "error", "ts": utc_iso(),
                              "code": "QUEUE_FULL",
                              "details": failure["error"]["details"]})
            return failure
        self.queue.append({"text": text, "source": source, "at": utc_iso(),
                           "focus": focus})
        self.publish_state()
        return None

    def pump_queue(self):
        if self.turn_in_flight or not self.queue:
            return
        allowed, code, details = self.gate()
        if not allowed:
            if code in ("BRIDGE_STALE", "TURN_WARN_UNACKNOWLEDGED"):
                # Recoverable: hold the prompt, report, retry on a later poll.
                if self.status != "refused":
                    self.status = "refused"
                    self.last_error = {"code": code, "details": details}
                    self.write_event({"t": "error", "ts": utc_iso(),
                                      "code": code, "details": details})
                    self.publish_state()
                return
            # Terminal for this session: drop the prompt rather than pretend.
            prompt = self.queue.pop(0)
            self.last_error = {"code": code, "details": details}
            self.write_event({"t": "error", "ts": utc_iso(), "code": code,
                              "details": details, "dropped_prompt":
                                  cap_text(prompt["text"], 200)})
            self.publish_state()
            return
        prompt = self.queue.pop(0)
        self.send_now(prompt["text"], focus=prompt.get("focus"))

    def send_now(self, text, focus=None):
        self.turn_focus = focus
        self.turn_seq += 1
        self.turn_in_flight = True
        self.turn_started = time.time()
        self.turn_cost = 0.0
        self.consec_tool_errors = 0
        self.status = "thinking"
        self._result_event.clear()
        self.write_event({"t": "user", "ts": utc_iso(), "turn": self.turn_seq,
                          "text": cap_text(text, self.config.get("event_line_cap"))})
        # Publish BEFORE the handoff. A cheap turn can come back on the reader
        # thread in milliseconds, and a publish after the write would then stamp
        # a stale "thinking" over the reader's "idle" and leave the panel
        # showing a turn that already finished.
        self.publish_state()
        failure = self._write_stdin(user_message_line(self.session_id, text))
        if failure:
            self.turn_in_flight = False
            self.turn_started = None
            self.status = "child_exited"
            self.last_error = failure["error"]
            self.write_event({"t": "error", "ts": utc_iso(),
                              "code": failure["error"]["code"],
                              "details": failure["error"]["details"]})
            self.publish_state()
        return failure

    def interrupt(self, wait=True):
        """Stop the running turn. Acknowledged in ~20 ms.

        Gated on init.capabilities carrying interrupt_receipt_v1, which is only
        known after the first turn's init (init is lazy). An interrupt while
        IDLE returns success and emits NO result at all, so nothing here waits
        for one: only the control_response is awaited.

        `wait=False` is mandatory when calling from the stdout reader thread
        (the circuit breaker does). The control_response arrives ON stdout, so
        a reader thread that blocks waiting for it would be waiting on itself.
        """
        if "interrupt_receipt_v1" not in self.capabilities:
            return err("INTERRUPT_UNSUPPORTED",
                       "child has not advertised interrupt_receipt_v1")
        self._control_seq += 1
        rid = f"req_{self._control_seq}"
        waiter = threading.Event()
        self._control_waiters[rid] = waiter
        failure = self._write_stdin({"type": "control_request",
                                     "request_id": rid,
                                     "request": {"subtype": "interrupt"}})
        if failure:
            self._control_waiters.pop(rid, None)
            return failure
        if not wait:
            return ok(request_id=rid, awaited=False)
        if not waiter.wait(self.stdin_timeout):
            self._control_waiters.pop(rid, None)
            return err("INTERRUPT_TIMEOUT",
                       f"no control_response within {self.stdin_timeout}s")
        self._control_waiters.pop(rid, None)
        return ok(response=self._control_results.pop(rid, None))

    # -- panel / control polling ------------------------------------------

    def poll_panel(self):
        try:
            mtime = os.path.getmtime(self.panel_path)
        except OSError:
            self.panel_miss_count += 1
            if self.panel_missing_since is None:
                self.panel_missing_since = time.time()
            return
        self.panel_missing_since = None
        self.panel_miss_count = 0
        # Trust the file's own mtime, not "we managed to stat it": a panel
        # frozen mid-render leaves a readable but old file.
        self.panel_last_ok = max(self.panel_last_ok or 0.0, mtime)

    def _consume_dir(self, name):
        """Sorted listing (ids are timestamp-prefixed, so this is chronological),
        read then delete. A file that cannot be parsed is still deleted, or it
        would be re-read on every poll forever."""
        out = []
        directory = self.dirs[name]
        try:
            # `.json.tmp` (the atomic-write scratch file) does not end in
            # `.json`, so this filter already skips a half-written prompt.
            files = sorted(f for f in os.listdir(directory) if f.endswith(".json"))
        except OSError:
            return out
        for filename in files:
            path = os.path.join(directory, filename)
            data = read_json(path)
            if data is None:
                # Could be mid-replace. Only drop it if it is genuinely old.
                try:
                    if time.time() - os.path.getmtime(path) < 2.0:
                        continue
                except OSError:
                    continue
            remove_file(path)
            if isinstance(data, dict):
                data.setdefault("_id", filename[:-len(".json")])
                out.append(data)
        return out

    def poll_prompts(self):
        for prompt in self._consume_dir("prompts"):
            text = prompt.get("text") or prompt.get("prompt")
            if not text or not str(text).strip():
                continue
            focus = prompt.get("focus")
            if focus:
                # The envelope is attached to the prompt so the model does not
                # spend a tool call rediscovering what the panel already knows.
                text = (str(text).rstrip() + "\n\n<focus>\n"
                        + json.dumps(focus, indent=1, default=str) + "\n</focus>")
            self.enqueue(str(text), source=prompt.get("source") or "panel",
                         focus=focus if isinstance(focus, dict) else None)

    def poll_control(self):
        for control in self._consume_dir("control"):
            action = (control.get("action") or "").strip().lower()
            self.log(f"control: {action}")
            if action == "shutdown":
                self.stop_reason = "shutdown_control"
                self._stopping.set()
            elif action == "interrupt":
                result = self.interrupt()
                if result and not result.get("ok"):
                    self.write_event({"t": "error", "ts": utc_iso(),
                                      "code": result["error"]["code"],
                                      "details": result["error"]["details"]})
            elif action == "ack_warn":
                self.warn_acknowledged = True
                self.publish_state()
            elif action == "clear_queue":
                self.queue.clear()
                self.publish_state()
            elif action in ("resume", "restart"):
                self.restart(resume=True)
            elif action == "new_session":
                self.restart(resume=False)
            else:
                self.write_event({"t": "error", "ts": utc_iso(),
                                  "code": "UNKNOWN_CONTROL",
                                  "details": f"action {action!r}"})

    # -- dead-man + restart ------------------------------------------------

    def check_deadman(self):
        should_kill, reason = deadman_verdict(
            now=time.time(),
            panel_last_ok=self.panel_last_ok,
            panel_missing_since=self.panel_missing_since,
            panel_miss_count=self.panel_miss_count,
            bridge_busy=self.bridge_busy,
            turn_in_flight=self.turn_in_flight,
            deadman_seconds=self.deadman_seconds)
        if should_kill:
            self.log(f"dead-man fired ({reason}); killing child", "ERROR")
            self.stop_reason = "deadman"
            self.write_event({"t": "session_end", "ts": utc_iso(),
                              "reason": "deadman", "details": reason})
            self._stopping.set()
        return should_kill

    def check_turn_timeout(self):
        """A turn with no result pins turn_in_flight true, and the dead-man
        switch refuses to fire while a turn is in flight. Without this, REAPER
        crashing during a hung turn would leave a paid session running forever.
        """
        if not self.turn_in_flight or self.turn_started is None:
            return False
        if (time.time() - self.turn_started) < self.turn_timeout:
            return False
        self.log(f"turn exceeded {self.turn_timeout}s; interrupting", "ERROR")
        self.write_event({"t": "error", "ts": utc_iso(), "code": "TURN_TIMEOUT",
                          "details": f"no result within {self.turn_timeout}s"})
        result = self.interrupt()
        if not (isinstance(result, dict) and result.get("ok")):
            self.restart(resume=True)
        else:
            # The interrupt is acknowledged in ~20 ms but the result may take a
            # moment; if it never comes, the next pass restarts.
            self.turn_started = time.time()
        return True

    def sweep_inbox_for_console_commands(self, since):
        """Remove inbox commands this session's MCP child left behind.

        A killed MCP process never reaches reaperd's withdraw path
        (reaperd.py:281-290), so a queued `batch` would execute minutes later
        against whatever project is open then. Only files written since the
        CURRENT turn started are swept, and only when a turn was in flight:
        reaperd.send_type is synchronous, so anything the MCP server has
        outstanding was necessarily written inside this turn. Anything older
        belongs to somebody else (David's own terminal) and is left alone.

        "Older" carries MTIME_GRANULARITY_SLACK of tolerance, because a file
        written just after the turn's timestamp can carry an mtime just before
        it. The cost is that a command fired from another terminal inside that
        one-second window is swept too; the alternative is stranding a live
        `batch` in the inbox, which is the worse of the two.
        """
        removed = []
        if since is None:
            return removed
        inbox = os.path.join(self.root, "inbox")
        try:
            files = sorted(f for f in os.listdir(inbox) if f.endswith(".json"))
        except OSError:
            return removed
        for filename in files:
            path = os.path.join(inbox, filename)
            try:
                if os.path.getmtime(path) < since - MTIME_GRANULARITY_SLACK:
                    continue
            except OSError:
                continue
            data = read_json(path, attempts=2)
            if isinstance(data, dict) and data.get("created_by") not in ("cli", "mcp"):
                continue
            if remove_file(path, attempts=5):
                removed.append(filename)
        if removed:
            self.log(f"withdrew stranded inbox commands: {', '.join(removed)}", "WARN")
        return removed

    def kill_child(self, reason="stop"):
        turn_started = self.turn_started if self.turn_in_flight else None
        self._stopping.set()
        failure = kill_tree(self.proc)
        if failure:
            self.log(f"kill failed: {failure['error']['details']}", "ERROR")
        self.sweep_inbox_for_console_commands(turn_started)
        self.turn_in_flight = False
        self.turn_started = None
        with self._file_lock:
            self._close_session_files_locked()
        self.log(f"child killed ({reason})")
        return failure

    def restart(self, resume=True):
        """Resume keeps the thread and skips the ~$0.27 Opus cold start; a new
        session pays it again. Reusing the MINTED --session-id would fail with
        `Session ID <uuid> is already in use.` on stderr and ZERO stdout events,
        which is why restart always goes through --resume."""
        resume_id = self.session_id if resume else None
        previous = self.events_path
        self.write_event({"t": "session_end", "ts": utc_iso(),
                          "reason": "resume" if resume else "new_session"})
        self.kill_child(reason="restart")
        # Calls the dead child never got a result for. last_change survives on
        # purpose: killing the session does not un-change the project, and the
        # Undo button is most wanted right after something went wrong.
        self._pending_changes.clear()
        self._stopping.clear()
        failure = self.spawn(resume_id=resume_id)
        if failure:
            self.status = "child_exited"
            self.last_error = failure["error"]
            self.log(f"respawn failed: {failure['error']['details']}", "ERROR")
        else:
            self.status = "idle"
            if not resume:
                self.session_cost = 0.0
                self.prev_total_cost = 0.0
            self.log(f"respawned from {previous}")
        self.publish_state()
        return failure

    # -- retention ---------------------------------------------------------

    def sweep_retention(self, interval=300.0):
        now = time.time()
        if now - self._last_sweep < interval:
            return []
        self._last_sweep = now
        entries = []
        for name in ("events", "raw", "audio"):
            for path in glob.glob(os.path.join(self.dirs[name], "*")):
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if not os.path.isfile(path):
                    continue
                entries.append((path, stat.st_mtime, stat.st_size))
        days = self.config.get("retention_days")
        doomed = sweep_plan(
            entries, now,
            max_age_seconds=(days * 86400.0) if days else None,
            max_bytes=self.config.get("retention_bytes"),
            protect=[p for p in (self.events_path, self.raw_path) if p])
        for path in doomed:
            remove_file(path, attempts=3)
        if doomed:
            self.log(f"retention sweep removed {len(doomed)} files")
        return doomed

    # -- main loop ---------------------------------------------------------

    def run(self):
        if self.take_lock:
            failure = self.claim_lock()
            if failure:
                self.log(failure["error"]["details"], "ERROR")
                return failure
        try:
            failure = self.spawn()
            if failure:
                self.status = "child_exited"
                self.last_error = failure["error"]
                self.publish_state()
                return failure
            self.check_bridge(max_age=0.0)
            self.publish_state()
            while not self._stopping.is_set():
                time.sleep(self.poll_interval)
                self.poll_panel()
                self.check_bridge()
                self.poll_control()
                if self._stopping.is_set():
                    break
                self.poll_prompts()
                self.pump_queue()
                self.check_turn_timeout()
                self.check_deadman()
                self.sweep_retention()
                self.publish_state()
        finally:
            self.shutdown()
        return None

    def shutdown(self):
        self._stopping.set()
        self.status = "stopped"
        self.write_event({"t": "session_end", "ts": utc_iso(),
                          "reason": self.stop_reason or "shutdown"})
        self.kill_child(reason=self.stop_reason or "shutdown")
        self.publish_state()
        if self.take_lock:
            self.release_lock()

    # -- one-shot ----------------------------------------------------------

    def once(self, prompt, timeout=None):
        """One turn, normalized events to the emit sink, then exit.

        The demo path: no panel, no REAPER-side code, no dead-man switch (there
        is no panel to be stale). The bridge gate still applies, because a stale
        bridge makes the whole turn pointless and expensive.
        """
        if self.take_lock:
            failure = self.claim_lock()
            if failure:
                return failure
        try:
            failure = self.spawn()
            if failure:
                return failure
            self.check_bridge(max_age=0.0)
            allowed, code, details = self.gate()
            if not allowed:
                return err(code, details)
            self._result_event.clear()
            failure = self.send_now(prompt)
            if failure:
                return failure
            deadline = timeout if timeout is not None else self.turn_timeout
            if not self._result_event.wait(deadline):
                self.interrupt()
                return err("TURN_TIMEOUT", f"no result within {deadline}s")
            return ok(turn_cost_usd=round(self.turn_cost, 6),
                      session_cost_usd=round(self.session_cost, 6),
                      daily_cost_usd=round(self.daily_cost, 6),
                      session_id=self.session_id,
                      events_file=self.events_path,
                      status=self.status)
        finally:
            self.stop_reason = "once_complete"
            self.shutdown()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(root):
    """console_config.json from the repo root. Keys starting with "_" are
    documentation in the example file and are ignored."""
    path = os.path.join(root, "console_config.json")
    data = read_json(path)
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if not k.startswith("_") and k in DEFAULT_CONFIG}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="console_sidecar",
        description="Daemon Console sidecar: one persistent headless Claude "
                    "Code session brokered to a ReaImGui panel inside REAPER.")
    parser.add_argument("--root", default=BRIDGE_ROOT,
                        help="bridge root (default: this file's directory)")
    parser.add_argument("--once", metavar="PROMPT",
                        help="run one turn, print normalized events, exit")
    parser.add_argument("--model", help="override the configured model")
    parser.add_argument("--effort", help="override the configured effort level")
    parser.add_argument("--timeout", type=float, default=None,
                        help="--once: seconds to wait for the turn result")
    parser.add_argument("--no-lock", action="store_true",
                        help="skip the singleton lock (debugging only)")
    args = parser.parse_args(argv)

    overrides = {}
    if args.model:
        overrides["model"] = args.model
    if args.effort:
        overrides["effort"] = args.effort

    if args.once:
        # The model answers with em dashes and smart quotes, and a Windows
        # console defaults to cp1252, where printing one raises
        # UnicodeEncodeError and kills the demo path AFTER the turn was paid
        # for. Force UTF-8 and degrade unmappable characters instead.
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

        def emit(event):
            print(json.dumps(event, separators=(",", ":"), ensure_ascii=False,
                             default=str), flush=True)

        sidecar = ConsoleSidecar(root=args.root, config=overrides, emit=emit,
                                 take_lock=not args.no_lock)
        result = sidecar.once(args.once, timeout=args.timeout)
        if result and not result.get("ok"):
            print(json.dumps(result, separators=(",", ":")), file=sys.stderr,
                  flush=True)
            return 1
        print(json.dumps({"ok": True,
                          "turn_cost_usd": result.get("turn_cost_usd"),
                          "session_cost_usd": result.get("session_cost_usd"),
                          "daily_cost_usd": result.get("daily_cost_usd"),
                          "session_id": result.get("session_id")},
                         separators=(",", ":")), flush=True)
        return 0

    sidecar = ConsoleSidecar(root=args.root, config=overrides,
                             take_lock=not args.no_lock)
    result = sidecar.run()
    if result and not result.get("ok"):
        print(json.dumps(result, separators=(",", ":")), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
