#!/usr/bin/env python3
"""Reaper Daemon MCP server: drive REAPER from any MCP client over stdio.

A thin adapter over the same local file bridge everything else uses — it
translates MCP tool calls into JSON command files in inbox/ and reads results
from outbox/. No network listener, no third-party dependencies: pure stdlib,
JSON-RPC 2.0 over stdin/stdout (newline-delimited), same trust model as the
rest of the daemon (local files, undo blocks, dry_run, risk gating).

Wire it into Claude Code:

    claude mcp add reaper -- python /path/to/reaper-daemon/reaper_mcp.py

or Claude Desktop (claude_desktop_config.json):

    { "mcpServers": { "reaper": {
        "command": "python",
        "args": ["C:/path/to/reaper-daemon/reaper_mcp.py"] } } }

The bridge root defaults to this file's directory; override with the
REAPER_DAEMON_ROOT environment variable.

The analyze_track / compare_tracks tools additionally need Post Mortem
(https://github.com/wretcher207/post-mortem) installed so the `postmortem`
command is on PATH (or set POSTMORTEM_CMD). They return only verified-isolated
payloads; the model on the client side does the diagnosing.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SERVER_DIR)

import reaperd  # noqa: E402  (single-file CLI next to this script; import is safe)
import verifyloop  # noqa: E402  (closed-loop measure/verify/tune primitives)

BRIDGE_ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("REAPER_DAEMON_ROOT") or SERVER_DIR
))

SERVER_INFO = {"name": "reaper-daemon", "version": "1.0.0"}
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
DEFAULT_PROTOCOL = "2025-06-18"

DEFAULT_TIMEOUT_MS = 15000
CAPTURE_TIMEOUT_MS = 180000
_POSTMORTEM_MCP_RECEIPT = None


def _log(msg):
    print(f"[reaper-mcp] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Bridge plumbing
# ---------------------------------------------------------------------------

def _send(cmd_type, payload, timeout_ms=DEFAULT_TIMEOUT_MS, dry_run=False):
    """Send one command through the file bridge and return the parsed reply.
    None-valued payload keys are dropped so optional tool args never reach the
    bridge (False and 0 are meaningful and survive). The normal path is
    reaperd.send_type so fx-name resolution / set_fx_param repair stay in ONE
    place; only dry_run builds its own envelope (send_type has no dry_run
    knob), which is why dry_run add_fx needs the exact fx_name."""
    payload = {k: v for k, v in payload.items() if v is not None}
    if not dry_run:
        reply = reaperd.send_type(cmd_type, payload, bridge_root=BRIDGE_ROOT,
                                  timeout_ms=timeout_ms)
        if not isinstance(reply, dict):
            return {"ok": False, "error": {"code": "BAD_REPLY",
                                           "details": "reply is not a JSON object"}}
        return reply
    if cmd_type == "set_fx_param":
        payload = reaperd.repair_set_fx_param(payload)
    cmd = {"version": 3, "type": cmd_type, "payload": payload,
           "created_by": "mcp", "dry_run": True}
    try:
        _cid, raw = reaperd.send_command(
            cmd, wait=True, timeout_ms=timeout_ms, bridge_root=BRIDGE_ROOT)
    except TimeoutError as e:
        return {"ok": False, "error": {"code": "TIMEOUT", "details": str(e)}}
    except reaperd.StaleReplyError as e:
        # Same structured shape send_type gives the non-dry-run path: a
        # locked stale reply is a fail-closed refusal, not a generic crash.
        return {"ok": False, "error": {"code": "STALE_REPLY_LOCKED",
                                       "details": str(e)}}
    if raw is None:
        return {"ok": False, "error": {"code": "NO_REPLY", "details": "no reply"}}
    try:
        reply = json.loads(raw)
    except ValueError as e:
        return {"ok": False, "error": {"code": "BAD_REPLY", "details": str(e)}}
    if not isinstance(reply, dict):
        return {"ok": False, "error": {"code": "BAD_REPLY",
                                       "details": "reply is not a JSON object"}}
    return reply


def _text(text, is_error=False):
    out = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


def _reply_result(reply, note=None, extra=None):
    """Format a bridge reply as an MCP tool result.

    `note` goes INSIDE the JSON body, never in front of it: the console's
    normalizer decides a result is structured by its first character
    (console_sidecar.py:tool_verdict), so a prose preamble would turn every
    measurement back into an opaque string.
    """
    if reply.get("ok"):
        body = {"ok": True}
        if reply.get("message"):
            body["message"] = reply["message"]
        if reply.get("data") is not None:
            body["data"] = reply["data"]
        if reply.get("dry_run"):
            body["dry_run"] = True
        if extra:
            body.update(extra)
        if note:
            body["console_note"] = note
        return _text(json.dumps(body, indent=1))
    body = {"ok": False, "error": reply.get("error")}
    if extra:
        body.update(extra)
    if note:
        body["console_note"] = note
    return _text(json.dumps(body, indent=1), is_error=True)


# --- console freeze policy --------------------------------------------------
#
# Every capture is an offline render and REAPER's UI is dead for its duration.
# From a terminal that costs an alt-tab. From the panel it costs the instrument
# he is working in, with the chat box he was typing into frozen mid-word, and
# verify_change is TWO renders while tune_param is up to six. A 30 second
# default would make the panel worse than the terminal it replaced, so console
# captures are clamped and the clamp is always announced.

CONSOLE_MODE = os.environ.get("REAPER_DAEMON_CONSOLE_MODE") == "1"
try:
    CONSOLE_MAX_CAPTURE_SECONDS = float(
        os.environ.get("REAPER_DAEMON_CONSOLE_MAX_CAPTURE_SECONDS") or 8)
except ValueError:
    CONSOLE_MAX_CAPTURE_SECONDS = 8.0


def clamp_capture_seconds(seconds, default, console_mode=None, maximum=None):
    """(seconds, note_or_None) for a capture length under the console policy.

    Pure, and the note is not decoration: a silently shortened capture has the
    model reporting on 8 seconds of a section while saying it measured 30, which
    is the honesty failure AGENTS.md exists to prevent.
    """
    console_mode = CONSOLE_MODE if console_mode is None else console_mode
    maximum = CONSOLE_MAX_CAPTURE_SECONDS if maximum is None else maximum
    try:
        value = float(default if seconds is None else seconds)
    except (TypeError, ValueError):
        return seconds, None
    if not console_mode or value <= maximum:
        return seconds, None
    return maximum, (
        f"Capture shortened from {value:g}s to {maximum:g}s: the Daemon Console "
        f"runs inside REAPER and a capture freezes its UI for the whole render. "
        f"The numbers describe {maximum:g}s of audio, not {value:g}s. Say so when "
        f"you report them; ask before requesting a longer one.")


def _track_payload(args):
    """Shared track selector -> bridge payload fields."""
    return {
        "target_track_guid": args.get("target_track_guid"),
        "target_track_name": args.get("track"),
        "track_name_contains": args.get("track_contains"),
        "use_selected_track": True if args.get("use_selected_track") else None,
    }


TRACK_PROPS = {
    "target_track_guid": {"type": "string",
                          "description": "Stable REAPER track GUID; preferred when available."},
    "track": {"type": "string",
              "description": "Exact track name (case-insensitive)."},
    "track_contains": {"type": "string",
                       "description": "Case-insensitive substring; errors if it matches more than one track."},
    "use_selected_track": {"type": "boolean",
                           "description": "Target the currently selected track instead of naming one."},
}
DRY_RUN_PROP = {
    "dry_run": {"type": "boolean",
                "description": "Preview: return what would run without changing the project."},
}


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _forward(cmd_type, args, keys, timeout_ms=DEFAULT_TIMEOUT_MS,
             track=False, dry_run=False, extra=None):
    """Mechanical arg -> payload forwarding shared by the thin tool handlers.

    Copies each key verbatim from args (missing keys become None, which _send
    strips). `track` prepends the shared track-selector fields, `dry_run`
    forwards the tool's dry_run flag, and `extra` carries any per-tool
    computed values (defaults, validated fields).
    """
    payload = _track_payload(args) if track else {}
    for key in keys:
        payload[key] = args.get(key)
    if extra:
        payload.update(extra)
    return _reply_result(_send(cmd_type, payload, timeout_ms=timeout_ms,
                               dry_run=bool(args.get("dry_run")) if dry_run
                               else False))


def tool_get_status(args):
    alive = reaperd.status_ok(bridge_root=BRIDGE_ROOT, quiet=True)
    info = {"alive": bool(alive), "bridge_root": BRIDGE_ROOT}
    hb_path = os.path.join(BRIDGE_ROOT, "bridge", "heartbeat.json")
    try:
        with open(hb_path, "r", encoding="utf-8") as f:
            hb = json.load(f)
        info["heartbeat"] = hb
        info["heartbeat_age_seconds"] = round(time.time() - os.path.getmtime(hb_path), 1)
    except (OSError, ValueError):
        info["heartbeat"] = None
    cfg_path = os.path.join(BRIDGE_ROOT, "bridge", "bridge_config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        info["allow_risk_level_3"] = bool(cfg.get("allow_risk_level_3"))
    except (OSError, ValueError):
        info["allow_risk_level_3"] = None
    if not alive:
        info["fix"] = ("Start REAPER (the bridge auto-loads via __startup.lua), "
                       "or run the bridge action manually. See README.")
    return _text(json.dumps(info, indent=1), is_error=not alive)


def tool_get_context(args):
    return _forward("get_context", args, (),
                    extra={"include_fx": args.get("include_fx", True)})


def tool_scan_fx(args):
    return _forward("scan_fx", args, (), track=True, timeout_ms=30000,
                    extra={"include_values": args.get("include_values", False)})


def tool_get_fx_parameters(args):
    base = _track_payload(args)
    for key in ("fx_name_contains", "fx_index", "fx_scope", "param_name_contains"):
        base[key] = args.get(key)
    base = {k: v for k, v in base.items() if v is not None}
    data, err = reaperd.scan_fx_parameter_data(
        base, BRIDGE_ROOT, include_values=args.get("include_values", True))
    if err:
        return _text(json.dumps({"ok": False, "error": err}, indent=1), is_error=True)
    return _text(json.dumps({"ok": True, **data}, indent=1))


def tool_get_track_routing(args):
    return _forward("get_track_routing", args, (), track=True)


_TRANSPORT_SIMPLE = {"play", "stop", "pause", "record"}


def tool_transport(args):
    action = args.get("action")
    if action in _TRANSPORT_SIMPLE:
        return _reply_result(_send(action, {}))
    if action == "set_cursor":
        return _reply_result(_send("set_cursor", {
            "position": args.get("position"),
            "seek_play": args.get("seek_play"),
        }))
    if action == "set_time_selection":
        return _reply_result(_send("set_time_selection", {
            "start": args.get("start"), "end": args.get("end"),
            "length_bars": args.get("length_bars"),
            "clear": args.get("clear"),
        }))
    if action == "set_tempo":
        return _reply_result(_send("set_tempo", {"bpm": args.get("bpm")}))
    return _text(f"unknown transport action: {action!r}", is_error=True)


_TRACK_ACTIONS = {
    "add": "add_track", "delete": "delete_track", "rename": "rename_track",
    "select": "select_track", "set_volume": "set_track_volume",
    "set_pan": "set_track_pan", "mute": "mute_track", "solo": "solo_track",
    "arm": "arm_track", "set_color": "set_track_color",
}


def tool_track(args):
    action = args.get("action")
    cmd_type = _TRACK_ACTIONS.get(action)
    if not cmd_type:
        return _text(f"unknown track action: {action!r}", is_error=True)
    dry_run = bool(args.get("dry_run"))
    if action == "add":
        return _reply_result(_send("add_track", {
            "name": args.get("name"), "index": args.get("index"),
            "color": args.get("color"), "select": args.get("select"),
        }, dry_run=dry_run))
    payload = _track_payload(args)
    payload.update({
        "new_name": args.get("new_name"), "volume_db": args.get("volume_db"),
        "pan": args.get("pan"), "mute": args.get("mute"),
        "solo": args.get("solo"), "armed": args.get("armed"),
        "color": args.get("color"), "exclusive": args.get("exclusive"),
        "select": args.get("select"),
    })
    return _reply_result(_send(cmd_type, payload, dry_run=dry_run))


_FX_ACTIONS = {"add": "add_fx", "remove": "remove_fx",
               "bypass": "bypass_fx", "move": "move_fx"}


def tool_fx(args):
    action = args.get("action")
    cmd_type = _FX_ACTIONS.get(action)
    if not cmd_type:
        return _text(f"unknown fx action: {action!r}", is_error=True)
    payload = _track_payload(args)
    payload.update({
        "fx_name": args.get("fx_name"),
        "fx_name_contains": args.get("fx_name_contains"),
        "fx_index": args.get("fx_index"), "fx_scope": args.get("fx_scope"),
        "bypass": args.get("bypass"), "to_index": args.get("to_index"),
        "show": args.get("show"),
    })
    return _reply_result(_send(cmd_type, payload, dry_run=bool(args.get("dry_run"))))


def tool_set_fx_param(args):
    return _forward("set_fx_param", args,
                    ("fx_name_contains", "fx_index", "fx_scope",
                     "param_name_contains", "param_index", "normalized_value",
                     "formatted_value", "relative"),
                    track=True, dry_run=True)


def tool_write_automation(args):
    return _forward("write_fx_param_automation", args,
                    ("fx_name_contains", "fx_index", "fx_scope",
                     "param_name_contains", "param_index", "points",
                     "ranges", "clear_existing_in_range"),
                    track=True, timeout_ms=30000, dry_run=True)


def tool_apply_automation_transaction(args):
    return _forward("apply_automation_transaction", args,
                    ("writes", "transaction_id"),
                    timeout_ms=30000, dry_run=True)


def tool_get_fx_param_automation(args):
    return _forward("get_fx_param_automation", args,
                    ("fx_guid", "fx_name_contains", "fx_index", "fx_scope",
                     "param_name_contains", "param_index", "start_time",
                     "end_time", "include_neighbors"),
                    track=True)


_MARKER_ACTIONS = {"add_marker", "add_region", "delete_marker"}


def tool_markers(args):
    action = args.get("action")
    if action not in _MARKER_ACTIONS:
        return _text(f"unknown markers action: {action!r}", is_error=True)
    return _forward(action, args,
                    ("position", "start", "end", "length_bars", "name",
                     "color", "marker_index", "is_region"),
                    dry_run=True)


def tool_insert_midi_file(args):
    return _forward("insert_midi_file", args,
                    ("midi_path", "length", "loop",
                     "replace_existing_in_range"),
                    track=True, timeout_ms=30000, dry_run=True,
                    extra={"position": args.get("position") or {"type": "cursor"}})


def tool_delete_items_in_range(args):
    return _forward("delete_items_in_range", args,
                    ("range", "length_bars", "length_seconds", "all_tracks"),
                    track=True, dry_run=True)


def tool_batch(args):
    return _forward("batch", args, ("commands", "undo_label"),
                    timeout_ms=60000, dry_run=True,
                    extra={"stop_on_error": args.get("stop_on_error", True)})


def tool_capture_track_audio(args):
    payload = _track_payload(args)
    seconds, clamp_note = clamp_capture_seconds(args.get("duration_seconds"), 30)
    if seconds is None:
        seconds = 30
    output = args.get("output_file")
    if not output:
        temp_dir = os.path.join(tempfile.gettempdir(), "reaper-mcp")
        os.makedirs(temp_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        output = os.path.join(temp_dir, f"capture-{stamp}.wav")
    payload.update({"duration_seconds": seconds, "output_file": output,
                    "start_seconds": args.get("start_seconds")})
    return _reply_result(_send("capture_track_audio", payload,
                               timeout_ms=CAPTURE_TIMEOUT_MS), note=clamp_note)


def tool_raw_command(args):
    cmd_type = args.get("type")
    if not cmd_type:
        return _text("raw_command needs a 'type'", is_error=True)
    # Sanitize BEFORE sending: a bad timeout that blew up after the inbox
    # write would leave an orphaned command the bridge executes later.
    try:
        timeout_ms = int(args.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    except (TypeError, ValueError):
        return _text(f"timeout_ms must be an integer, got {args.get('timeout_ms')!r}",
                     is_error=True)
    if timeout_ms <= 0:
        timeout_ms = DEFAULT_TIMEOUT_MS
    return _reply_result(_send(
        cmd_type, args.get("payload") or {},
        timeout_ms=timeout_ms,
        dry_run=bool(args.get("dry_run"))))


# --- Drum workflow (profile / riff / groove) ---------------------------------
#
# These three shell out to `sys.executable`, the same way reaperd.py runs the
# drum engine: the analysis code lives in skills/drum-apparatus/ and the bridge
# itself never runs external processes (docs/safety.md). Promoting them to MCP
# tools removes the shell from the loop; it does not move the work.

DRUM_SKILL_SUBDIR = os.path.join("skills", "drum-apparatus")
PROFILE_TIMEOUT_S = 600     # reaperd.cmd_profile's timeout
RIFF_TIMEOUT_S = 300        # reaperd.cmd_riff's timeout
GROOVE_GEN_TIMEOUT_S = 120  # reaperd.cmd_groove's generation timeout
GROOVE_INSERT_TIMEOUT_MS = 20000

# profile and riff parse the .rpp FILE ON DISK. REAPER's live, unsaved state is
# invisible to them, so every payload carries this verbatim — a model that drops
# it would report on material the user may already have replaced.
SAVED_PROJECT_CAVEAT = (
    "Read from the SAVED .rpp file on disk, NOT from REAPER's live in-memory "
    "project. Unsaved edits are invisible here: if the project changed since "
    "the last save, this analysis describes stale material. Repeat this caveat "
    "when you report the numbers, or ask the user to save and rerun."
)


def _drum_skill_dir():
    return os.path.join(BRIDGE_ROOT, DRUM_SKILL_SUBDIR)


def _error_result(code, details):
    """The structured failure shape bridge errors already use."""
    return _text(json.dumps({"ok": False,
                             "error": {"code": code, "details": details}},
                            indent=1), is_error=True)


def _child_error_details(proc):
    """The child's own message, never a raw traceback dump.

    groovegen.py and drumgen.profile exit non-zero with a clean `error: ...`
    line, which is exactly what a model needs to fix its input, so it is passed
    through verbatim. A crashing child (drumgen.riff has no top-level guard)
    would otherwise dump a Python traceback into the tool result; keep the
    exception line, drop the frames."""
    text = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    if not text:
        return None
    if "Traceback (most recent call last)" in text:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-1].strip()
    return text[-2000:]


def _saved_project_arg(tool, args):
    """Validate the shared (project, track) selector. Returns (project, track,
    error_result)."""
    project = args.get("project")
    track = args.get("track")
    if not project or not isinstance(project, str):
        return None, None, _text(f"{tool} needs a 'project' path to a saved "
                                 ".rpp file", is_error=True)
    if not track or not isinstance(track, str):
        return None, None, _text(f"{tool} needs a 'track' name", is_error=True)
    project = os.path.abspath(os.path.expanduser(project))
    if not os.path.isfile(project):
        return None, None, _error_result(
            "PROJECT_NOT_FOUND",
            f"no saved project file at {project}. This tool reads the .rpp on "
            "disk; save the project in REAPER first.")
    return project, track, None


def _run_drum_module(kind, argv, timeout_s):
    """Run a drumgen module from the skill dir. Returns (proc, error_result)."""
    skill_dir = _drum_skill_dir()
    if not os.path.isdir(skill_dir):
        return None, _error_result(
            "DRUM_SKILL_MISSING", f"drum skill not found at {skill_dir}")
    try:
        proc = subprocess.run([sys.executable] + argv, cwd=skill_dir,
                              capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, _error_result(
            f"{kind.upper()}_TIMEOUT",
            f"{kind} analysis did not finish in {timeout_s}s (very long stem? "
            "analyze a window with bars/start_bar"
            + ("/max_seconds)" if kind == "profile" else ")"))
    except OSError as e:
        return None, _error_result(f"{kind.upper()}_FAILED", str(e))
    if proc.returncode != 0:
        return None, _error_result(
            f"{kind.upper()}_FAILED",
            _child_error_details(proc) or f"{kind} exited {proc.returncode}")
    return proc, None


def _saved_project_result(kind, project, track, proc):
    """One payload shape for both disk readers, caveat attached."""
    body = {
        "ok": True,
        "analysis": kind,
        "source": "saved_project_file",
        "reads_live_reaper_state": False,
        "project": project,
        "track": track,
        "caveat": SAVED_PROJECT_CAVEAT,
    }
    try:
        body["project_saved_at"] = datetime.fromtimestamp(
            os.path.getmtime(project), timezone.utc).isoformat()
    except OSError:
        body["project_saved_at"] = None
    body["report"] = (proc.stdout or "").rstrip()
    # The CLI throws the child's stderr away on success, which silently eats
    # drumgen's multi-item / windowing warnings. Keep them.
    warnings = (proc.stderr or "").strip()
    if warnings:
        body["warnings"] = warnings[-2000:]
    return _text(json.dumps(body, indent=1))


def tool_profile_track(args):
    project, track, err = _saved_project_arg("profile_track", args)
    if err:
        return err
    # argv lives in reaperd.drum_profile_args so the CLI and MCP builders
    # cannot drift apart (drumgen takes the window positionally, in order).
    argv = reaperd.drum_profile_args(
        project, track, bars=args.get("bars"), start_bar=args.get("start_bar"),
        max_seconds=args.get("max_seconds"))
    proc, err = _run_drum_module("profile", argv, PROFILE_TIMEOUT_S)
    if err:
        return err
    return _saved_project_result("profile", project, track, proc)


def tool_riff_grid(args):
    project, track, err = _saved_project_arg("riff_grid", args)
    if err:
        return err
    bars = args.get("bars")
    start_bar = args.get("start_bar")
    argv = ["-m", "drumgen.riff", project, track,
            str(4 if bars is None else bars), str(start_bar or 0)]
    proc, err = _run_drum_module("riff", argv, RIFF_TIMEOUT_S)
    if err:
        return err
    return _saved_project_result("riff", project, track, proc)


def _render_sender(dry_run=False):
    """The `sender` seam reaperd's render/humanize path takes, wired to this
    server's own envelope so `dry_run` still works. Signature is fixed by
    reaperd._render_insert: (cmd_type, payload, timeout_ms) -> reply dict."""
    def send(cmd_type, payload, timeout_ms):
        return _send(cmd_type, payload, timeout_ms=timeout_ms, dry_run=dry_run)
    return send


def _write_path_result(rc, message, extra=None):
    """One result shape for the four write tools. `message` is reaperd's own
    per-leg line, which already names the track and the position it landed at."""
    body = {"ok": rc == 0, "report": message}
    if extra:
        body.update(extra)
    return _text(json.dumps(body, indent=1), is_error=(rc != 0))


def _reaper_down_result(tool):
    return _error_result(
        "REAPER_DOWN",
        f"no fresh bridge heartbeat — REAPER is not running or the bridge never "
        f"loaded. {tool} generated nothing and inserted nothing.")


def _position_seconds(args, default=None):
    """Seconds only. The riff and band tools cut at a time, and `replace` needs
    a real start to clear from, so a position object is not accepted there."""
    position = args.get("position")
    if isinstance(position, bool) or not isinstance(position, (int, float)):
        return default
    return float(position)


def _insert_position(args):
    """insert_groove's looser rule, kept from before the shared write path: a
    plain number of seconds OR a full position object ({"type":"bar",...}).
    None = the edit cursor."""
    position = args.get("position")
    if isinstance(position, dict):
        return position
    return _position_seconds(args)


def tool_insert_riff(args):
    """One humanized guitar or bass part onto a named track."""
    track = args.get("track")
    if not track or not isinstance(track, str):
        return _text("insert_riff needs a 'track' name", is_error=True)
    part = args.get("part") or "guitar"
    if part not in ("guitar", "bass"):
        return _text("insert_riff: 'part' must be 'guitar' or 'bass'",
                     is_error=True)
    bars_text = args.get("bars_text")
    bars_file = args.get("bars_file")
    if bars_text is not None and bars_file is not None:
        return _text("insert_riff needs ONE riff source: bars_text or "
                     "bars_file, not both", is_error=True)
    if bars_text is not None and (not isinstance(bars_text, str)
                                  or not bars_text.strip()):
        return _text("insert_riff: bars_text is empty", is_error=True)
    if bars_file is not None:
        if not isinstance(bars_file, str) or not bars_file.strip():
            return _text("insert_riff: bars_file is empty", is_error=True)
        bars_file = os.path.abspath(os.path.expanduser(bars_file))
        if not os.path.isfile(bars_file):
            return _error_result("RIFF_NOT_FOUND", f"no riff file at {bars_file}")
    # Heartbeat gate first: a dead REAPER fails in ~0 s instead of after the
    # render plus the 20 s insert wait.
    if not reaperd.status_ok(bridge_root=BRIDGE_ROOT, quiet=True):
        return _reaper_down_result("insert_riff")

    tmp_bars = None
    if bars_text is not None:
        # The engine reads a riff from a FILE. This temp copy only has to
        # outlive the generator, unlike the rendered .mid, which REAPER holds
        # by reference and reaperd therefore persists.
        tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w",
                                          encoding="utf-8")
        tmp.write(bars_text if bars_text.endswith("\n") else bars_text + "\n")
        tmp.close()
        tmp_bars = bars_file = tmp.name
    try:
        rc, msg = reaperd.render_shred(
            BRIDGE_ROOT, part=part, track=track,
            seed=args.get("seed", 0x5152), map_name=args.get("map", "argent_e"),
            tempo=args.get("tempo"), riff=args.get("riff", "demo"),
            bars_file=bars_file, low_string=args.get("low_string"),
            position=_position_seconds(args), replace=bool(args.get("replace")),
            sender=_render_sender(bool(args.get("dry_run"))))
    finally:
        if tmp_bars:
            try:
                os.unlink(tmp_bars)
            except OSError:
                pass
    return _write_path_result(rc, msg, {"part": part, "track": track})


def tool_cut_band(args):
    """Two guitars, bass and drums in one call."""
    if not reaperd.status_ok(bridge_root=BRIDGE_ROOT, quiet=True):
        return _reaper_down_result("cut_band")
    seeds = args.get("seeds") or [101, 202, 303]
    if not isinstance(seeds, list) or not all(
            isinstance(s, int) and not isinstance(s, bool) for s in seeds):
        return _text("cut_band: 'seeds' must be a list of integers", is_error=True)
    bars_file = args.get("bars_file")
    if bars_file is not None:
        bars_file = os.path.abspath(os.path.expanduser(bars_file))
        if not os.path.isfile(bars_file):
            return _error_result("RIFF_NOT_FOUND", f"no riff file at {bars_file}")
    dsl = args.get("dsl_path")
    if dsl is not None:
        dsl = os.path.abspath(os.path.expanduser(dsl))
        if not os.path.isfile(dsl):
            return _error_result("DSL_NOT_FOUND", f"no DSL file at {dsl}")
    rc, results = reaperd.render_band(
        BRIDGE_ROOT,
        guitar_l=args.get("guitar_l", "argent-l"),
        guitar_r=args.get("guitar_r", "argent-r"),
        bass=args.get("bass", "nolly-bass-library"),
        drums=args.get("drums", "rs-drums-monarch"),
        riff=args.get("riff", "demo"), bars_file=bars_file, dsl=dsl,
        map_name=args.get("map", "argent_e"),
        drum_map=args.get("drum_map", "RS Monarch"), seeds=seeds,
        low_string=args.get("low_string"),
        position=_position_seconds(args, default=0.0), tempo=args.get("tempo"),
        replace=args.get("replace", True),
        sender=_render_sender(bool(args.get("dry_run"))))
    # Per-leg results, not one verdict: a jam where three tracks landed and one
    # failed has to say WHICH, or the user re-cuts all four to find out.
    legs = [{"ok": leg_rc == 0, "report": msg} for leg_rc, msg in results]
    return _text(json.dumps({"ok": rc == 0, "legs": legs}, indent=1),
                 is_error=(rc != 0))


def tool_humanize_take(args):
    """Dynamics + micro-timing onto a drum take that is already on the track."""
    track = args.get("track")
    if not track or not isinstance(track, str):
        return _text("humanize_take needs a 'track' name", is_error=True)
    if not reaperd.status_ok(bridge_root=BRIDGE_ROOT, quiet=True):
        return _error_result(
            "REAPER_DOWN",
            "no fresh bridge heartbeat — REAPER is not running or the bridge "
            "never loaded. The take was not read and nothing was written.")
    rc, lines, summary = reaperd.humanize_take(
        BRIDGE_ROOT, track=track, item_index=args.get("item_index"),
        amount=args.get("amount", 25), map_name=args.get("map"),
        seed=args.get("seed", 20260827),
        follow_lead=bool(args.get("follow_lead")),
        example_through_bar=args.get("example_through_bar"),
        # dry_run here means "plan it and report, send no write" — the core
        # skips apply_note_edits itself. The READ must stay real, so the sender
        # is never the dry-run one: a dry-run get_midi_notes returns no take to
        # plan against.
        dry_run=bool(args.get("dry_run")), sender=_render_sender(False))
    body = {"ok": rc == 0, "report": "\n".join(lines),
            "dry_run": bool(args.get("dry_run"))}
    if summary is not None:
        body["summary"] = summary
    return _text(json.dumps(body, indent=1), is_error=(rc != 0))


def tool_insert_groove(args):
    dsl_text = args.get("dsl_text")
    dsl_path = args.get("dsl_path")
    # Two DSL sources, one project: an ambiguous request fails closed before
    # anything is generated or sent, the way conflicting selectors do elsewhere.
    if dsl_text is not None and dsl_path is not None:
        return _text("insert_groove needs ONE DSL source: dsl_text or "
                     "dsl_path, not both", is_error=True)
    if dsl_text is None and dsl_path is None:
        return _text("insert_groove needs dsl_text (inline DSL) or dsl_path "
                     "(a .dsl file)", is_error=True)
    if dsl_text is not None and (not isinstance(dsl_text, str)
                                 or not dsl_text.strip()):
        return _text("insert_groove: dsl_text is empty", is_error=True)
    if dsl_path is not None:
        if not isinstance(dsl_path, str) or not dsl_path.strip():
            return _text("insert_groove: dsl_path is empty", is_error=True)
        dsl_path = os.path.abspath(os.path.expanduser(dsl_path))
        if not os.path.isfile(dsl_path):
            return _error_result("DSL_NOT_FOUND", f"no DSL file at {dsl_path}")
    # Heartbeat gate first (same as reaperd.cmd_groove): a dead REAPER fails
    # here in ~0 s instead of after MIDI generation plus the 20 s insert wait.
    if not reaperd.status_ok(bridge_root=BRIDGE_ROOT, quiet=True):
        return _reaper_down_result("insert_groove")

    cfg = reaperd.load_drum_config(BRIDGE_ROOT) or {}
    selector = {k: v for k, v in _track_payload(args).items() if v is not None}
    if not selector:
        # cmd_groove's fallback chain: named track, else drum-config's default
        # track, else whatever is selected in REAPER.
        selector = ({"target_track_name": cfg["track"]} if cfg.get("track")
                    else {"use_selected_track": True})
    rc, msg = reaperd.render_groove(
        BRIDGE_ROOT, dsl_path=dsl_path, dsl_text=dsl_text, track=selector,
        kit_map=args.get("map") or cfg.get("map"), seed=args.get("seed"),
        position=_insert_position(args), tempo=args.get("tempo"),
        replace=bool(args.get("replace")),
        sender=_render_sender(bool(args.get("dry_run"))))
    return _write_path_result(rc, msg, {"track": selector})


# --- Closed-loop verify / tune ----------------------------------------------

def _verify_sender(cmd_type, payload, timeout_ms=verifyloop.DEFAULT_SENDER_TIMEOUT_MS):
    """Read/capture transport for verifyloop: no name resolution, honors the
    per-call timeout (captures pass their own long render timeout). The
    default is the shared verifyloop constant so the CLI and MCP senders
    cannot drift apart."""
    return reaperd.send_type(cmd_type, payload, bridge_root=BRIDGE_ROOT,
                             timeout_ms=timeout_ms, resolve=False, repair=False)


def _verify_mutator(cmd_type, payload):
    """Mutation transport: the same path the `cmd`/tool handlers use (add_fx
    name resolution, set_fx_param alias repair). 30 s: a premature TIMEOUT
    means 'outcome unknown' (exit 2), not a cheap retry."""
    return reaperd.send_type(cmd_type, payload, bridge_root=BRIDGE_ROOT,
                             timeout_ms=30000, resolve=True, repair=True)


def _verify_result_text(res, note=None):
    # isError ONLY for REFUSED: it is the one status that proves nothing ran.
    if note:
        res = dict(res, console_note=note)
    try:
        body = json.dumps(res, indent=1, allow_nan=False)
    except ValueError:
        body = json.dumps(verifyloop._sanitize_nonfinite(res), indent=1,
                          allow_nan=False)
    return _text(body, is_error=res.get("status") == "REFUSED")


def tool_verify_change(args):
    if args.get("dry_run") is not None:
        # Silently dropping this would invert the caller's intent: the tool
        # has no dry_run and REALLY mutates (two renders plus an applied
        # change). raw_command with dry_run:true is the preview path.
        return _text("verify_change has no dry_run: it REALLY mutates the "
                     "project. Use raw_command with dry_run:true to preview "
                     "a command instead.", is_error=True)
    track = args.get("track")
    cmd_type = args.get("command_type")
    payload = args.get("payload")
    if not track or not isinstance(track, str):
        return _text("verify_change needs a 'track' name", is_error=True)
    if not cmd_type or not isinstance(cmd_type, str):
        return _text("verify_change needs a 'command_type'", is_error=True)
    if not isinstance(payload, dict):
        return _text("verify_change needs 'payload' as an object", is_error=True)
    # Two renders, so the clamp is worth double here.
    seconds, clamp_note = clamp_capture_seconds(args.get("seconds"), 10)
    res = verifyloop.verify(_verify_sender, _verify_mutator, track, cmd_type,
                            payload, seconds=seconds)
    # isError ONLY when provably nothing ran (REFUSED happens before the
    # mutation is sent). Every other outcome — including a bridge rejection,
    # whose handler can still have made a partial mid-edit change — is
    # UNVERIFIED: a real outcome the model must read and relay, never
    # auto-retry.
    return _verify_result_text(res, note=clamp_note)


def tool_tune_param(args):
    if args.get("dry_run") is not None:
        # Same guard as verify_change: this tool applies up to 6 live
        # parameter sets; a dropped dry_run would invert the caller's intent.
        return _text("tune_param has no dry_run: it REALLY mutates the "
                     "project. Use raw_command with dry_run:true to preview "
                     "a command instead.", is_error=True)
    track = args.get("track")
    if not track or not isinstance(track, str):
        return _text("tune_param needs a 'track' name", is_error=True)
    if (args.get("fx_name_contains") is not None
            and args.get("fx_index") is not None):
        # Every iteration is a real mutation: ambiguous targeting fails
        # closed instead of letting the bridge pick a precedence silently.
        return _text("tune_param needs ONE FX selector: fx_name_contains or "
                     "fx_index, not both", is_error=True)
    fx_selector = {}
    if args.get("fx_name_contains") is not None:
        fx_selector["fx_name_contains"] = args["fx_name_contains"]
    if args.get("fx_index") is not None:
        fx_selector["fx_index"] = args["fx_index"]
        fx_selector["fx_scope"] = args.get("fx_scope") or "track"
    if not fx_selector:
        return _text("tune_param needs fx_name_contains or fx_index",
                     is_error=True)
    if (args.get("param_index") is not None
            and args.get("param_name_contains") is not None):
        return _text("tune_param needs ONE parameter selector: param_index "
                     "or param_name_contains, not both", is_error=True)
    param_selector = {}
    if args.get("param_index") is not None:
        param_selector["param_index"] = args["param_index"]
    elif args.get("param_name_contains") is not None:
        param_selector["param_name_contains"] = args["param_name_contains"]
    else:
        return _text("tune_param needs param_index or param_name_contains",
                     is_error=True)
    target = args.get("target")
    if not isinstance(target, dict):
        return _text("tune_param needs 'target' as an object like "
                     '{"metric":"lufs_i","delta":-3.0,"tolerance":0.5}',
                     is_error=True)

    def scanner(base):
        return reaperd.scan_fx_parameter_data(base, BRIDGE_ROOT,
                                              include_values=True)

    # Baseline plus up to five iterations: the clamp is worth six times here.
    seconds, clamp_note = clamp_capture_seconds(args.get("seconds"), 10)
    res = verifyloop.tune_param(_verify_sender, scanner, track, fx_selector,
                                param_selector, target, seconds=seconds)
    if res.get("error") and not res.get("status"):
        return _text(json.dumps(res, indent=1, allow_nan=False), is_error=True)
    # isError ONLY for REFUSED (nothing was set). SET_FAILED/MEASURE_FAILED/
    # IDENTITY_CHANGED/SCOPE_CHANGED happen after sets may have applied —
    # marking them as tool errors would invite a retry on top of live
    # changes. The model must read `final` (the read-back state) instead.
    return _verify_result_text(res, note=clamp_note)


# --- Post Mortem integration -------------------------------------------------

ANALYZE_PREAMBLE = """\
Below is MEASURED data from the user's actual REAPER session: the track's FX
chain with current parameter values, routing (sends, receives, parent bus),
and a post-FX audio snapshot (sample peak, RMS, crest factor, 1/3-octave
spectrum, and when available: integrated LUFS, true peak, LRA, stereo
correlation/mid-side, silence_fraction). Diagnose it like a mix engineer:
be specific, name frequencies and parameters, and propose ONE concrete move,
not five, with a confidence rating (low/medium/high). Honesty contract:
treat null as "not measured", never as a value; this is ONE track, not the
mix — do not diagnose frequency masking or claim anything about how it sits
against other tracks; if silence_fraction is high, all level statistics are
diluted by dead air — say so. Do not suggest moves you can't verify from the
data. The bridge verified this capture is an isolated track, not a full mix.
An honest "I'm not sure" beats a confident wrong answer. After writing the
diagnosis, call complete_postmortem_onboarding with the track name and that
exact diagnosis so Post Mortem can render it in the panel."""

COMPARE_PREAMBLE = """\
Below is MEASURED data for two or more tracks from the user's actual REAPER
session, plus a precomputed masking table: the "contested" 1/3-octave bands
where BOTH tracks have real energy. You may diagnose frequency masking here —
the cross-track data backs it — but stay honest: 1/3-octave bands are coarse,
so a contested band is a CANDIDATE collision region, not proof; a shared band
can be fine by design (kick + bass); name the region, not a false-precise
single Hz. The louder track in a contested band is the likely masker.
Propose ONE concrete move (prefer a complementary carve referencing a real
EQ already in the relevant chain) with a confidence rating. If the tracks
barely overlap, say the masking is minimal and stop — do not invent a
problem to look useful. Every capture was verified isolated before this payload
was returned."""


def _postmortem_cmdline():
    override = os.environ.get("POSTMORTEM_CMD")
    if override:
        return override.split()
    exe = shutil.which("postmortem")
    return [exe] if exe else None


def _postmortem_data_root():
    override = os.environ.get("POSTMORTEM_DATA_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/PostMortem")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "PostMortem")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "postmortem")


def _write_panel_json(filename, payload):
    root = _postmortem_data_root()
    path = os.path.join(root, filename)
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as file:
            json.dump(payload, file)
        os.replace(tmp, path)
        return True
    except OSError as error:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        _log(f"could not write Post Mortem panel state: {error}")
        return False


def _record_panel_mcp_handoff(tracks, seconds):
    """Keep process-local proof that verified measurements reached the client."""
    global _POSTMORTEM_MCP_RECEIPT
    if len(tracks) != 1 or seconds != 10:
        return None
    _POSTMORTEM_MCP_RECEIPT = {
        "receipt_id": secrets.token_hex(32),
        "delivered_at": datetime.now(timezone.utc),
        "tracks": list(tracks),
        "seconds": seconds,
    }
    if not _submit_postmortem_job("record_mcp_measurement", {
        "receipt_id": _POSTMORTEM_MCP_RECEIPT["receipt_id"],
        "tracks": list(tracks),
        "seconds": seconds,
    }):
        _POSTMORTEM_MCP_RECEIPT = None
    return _POSTMORTEM_MCP_RECEIPT


def _submit_postmortem_job(job_type, payload):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    job_id = f"mcp-{stamp}-{os.getpid()}"
    return _write_panel_json(
        os.path.join("jobs", "inbox", job_id + ".json"),
        {
            "id": job_id,
            "type": job_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        },
    )


def _fresh_panel_handoff(track):
    handoff = _POSTMORTEM_MCP_RECEIPT
    if not isinstance(handoff, dict):
        return None
    delivered = handoff.get("delivered_at")
    tracks = handoff.get("tracks")
    if not isinstance(delivered, datetime) or delivered.tzinfo is None:
        return None
    age = (datetime.now(timezone.utc) - delivered).total_seconds()
    if age < 0 or age > 15 * 60:
        return None
    if handoff.get("seconds") != 10 or not isinstance(tracks, list) or len(tracks) != 1:
        return None
    if str(tracks[0]).casefold() != track.casefold():
        return None
    return handoff


def _capture_safety_error(payload):
    """Return an explanation unless every Post Mortem capture is verified safe."""
    if not isinstance(payload, dict):
        return "Post Mortem returned a non-object payload, so capture safety cannot be verified."

    if "tracks" in payload:
        tracks = payload.get("tracks")
        if not isinstance(tracks, list) or not tracks:
            return "Post Mortem returned no track capture provenance."
        captures = [
            (track.get("name") or "unnamed track", track.get("capture"))
            for track in tracks
            if isinstance(track, dict)
        ]
        if len(captures) != len(tracks):
            return "Post Mortem returned malformed track capture provenance."
    else:
        track = payload.get("track")
        name = track.get("name") if isinstance(track, dict) else "track"
        captures = [(name or "track", payload.get("capture"))]

    unsafe = []
    for name, capture in captures:
        scope = capture.get("scope") if isinstance(capture, dict) else None
        verified = isinstance(capture, dict) and capture.get("isolation_verified") is True
        if scope != "isolated_track" or not verified:
            unsafe.append(f"{name}: {scope or 'unknown'}")
    if unsafe:
        return (
            "Post Mortem capture safety refusal: the following capture(s) are not "
            "verified isolated tracks: "
            + ", ".join(unsafe)
            + ". No diagnostic payload was handed to the model."
        )
    return None


def _run_postmortem(tracks, seconds, preamble, note=None):
    cmdline = _postmortem_cmdline()
    if not cmdline:
        return _text(
            "Post Mortem is not installed (no `postmortem` on PATH). Install it:\n"
            "  pipx install git+https://github.com/wretcher207/post-mortem.git\n"
            "or set POSTMORTEM_CMD to its command line.", is_error=True)
    env = dict(os.environ, REAPER_DAEMON_ROOT=BRIDGE_ROOT)
    try:
        proc = subprocess.run(
            cmdline + list(tracks) + ["--payload-only", "--seconds", str(seconds)],
            capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired:
        return _text("postmortem timed out after 600s (stuck render dialog in "
                     "REAPER? its render window needs 'Automatically close when "
                     "finished' ticked).", is_error=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        return _text(f"postmortem failed (exit {proc.returncode}):\n{detail}",
                     is_error=True)
    payload_text = proc.stdout.strip()
    try:
        payload = json.loads(payload_text)
    except ValueError:
        return _text(
            "Post Mortem returned invalid JSON, so capture safety cannot be verified.",
            is_error=True,
        )
    safety_error = _capture_safety_error(payload)
    if safety_error:
        return _text(safety_error, is_error=True)

    _record_panel_mcp_handoff(tracks, seconds)

    warnings = []
    audio_blocks = ([payload.get("audio")] if "audio" in payload
                    else [t.get("audio") for t in payload.get("tracks", [])])
    for block in audio_blocks:
        if not block:
            continue
        frac = block.get("silence_fraction") or 0
        rms = block.get("rms_db")
        if frac >= 0.85 or (rms is not None and rms <= -60):
            warnings.append(
                "WARNING: this capture is mostly silence "
                f"(rms_db={rms}, silence_fraction={frac}). Tell the user to "
                "park the edit cursor where the track is playing and rerun.")
    parts = [preamble]
    if note:
        parts.append(note)
    parts.extend(warnings)
    parts.append(payload_text)
    return _text("\n\n".join(parts))


def tool_analyze_track(args):
    track = args.get("track")
    if not track or not isinstance(track, str):
        return _text("analyze_track needs a 'track' name", is_error=True)
    seconds, clamp_note = clamp_capture_seconds(args.get("seconds"), 10)
    return _run_postmortem([track], seconds if seconds is not None else 10,
                           ANALYZE_PREAMBLE, note=clamp_note)


def tool_compare_tracks(args):
    tracks = args.get("tracks") or []
    if len(tracks) < 2:
        return _text("compare_tracks needs at least two track names", is_error=True)
    seconds, clamp_note = clamp_capture_seconds(args.get("seconds"), 30)
    return _run_postmortem(tracks, seconds if seconds is not None else 30,
                           COMPARE_PREAMBLE, note=clamp_note)


def tool_complete_postmortem_onboarding(args):
    track = args.get("track")
    diagnosis = args.get("diagnosis")
    if not isinstance(track, str) or not track.strip():
        return _text("complete_postmortem_onboarding needs the analyzed track name", is_error=True)
    if not isinstance(diagnosis, str) or len(diagnosis.strip()) < 20:
        return _text("complete_postmortem_onboarding needs the full diagnosis", is_error=True)
    if len(diagnosis) > 12000:
        return _text("the diagnosis is too long to render in the panel", is_error=True)
    track = track.strip()
    receipt = _fresh_panel_handoff(track)
    if receipt is None:
        return _text(
            "No fresh 10-second single-track Post Mortem handoff matches this track. "
            "Run analyze_track first.",
            is_error=True,
        )
    saved = _submit_postmortem_job(
        "record_mcp_handoff",
        {"receipt_id": receipt["receipt_id"], "tracks": [track],
         "diagnosis_summary": diagnosis.strip()},
    )
    if not saved:
        return _text("The diagnosis could not be written to the Post Mortem panel.", is_error=True)
    return _text("Diagnosis sent to the Post Mortem panel.")


# ---------------------------------------------------------------------------
# Tool registry (name, description, inputSchema, handler)
# ---------------------------------------------------------------------------

def _schema(props, required=None):
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


POSITION_PROP = {
    "type": "object",
    "description": ("Position object, e.g. {\"type\":\"cursor\"}, "
                    "{\"type\":\"bar\",\"bar\":33}, {\"type\":\"time\",\"seconds\":12.5}, "
                    "{\"type\":\"marker\",\"name\":\"Chorus\"}, {\"type\":\"region\",\"name\":\"Verse 1\"}, "
                    "{\"type\":\"time_selection\"}"),
}

TOOLS = [
    {
        "name": "get_status",
        "description": ("Check the bridge is alive inside REAPER (heartbeat, open "
                        "project, whether risk-level-3 commands like audio capture "
                        "are enabled). Call this first."),
        "inputSchema": _schema({}),
        "handler": tool_get_status,
    },
    {
        "name": "get_context",
        "description": ("Read the open REAPER project: name, tempo, transport, "
                        "cursor, time selection, every track (with FX names), "
                        "markers, regions. Read-only. Run before ambiguous edits."),
        "inputSchema": _schema({
            "include_fx": {"type": "boolean", "description": "Include each track's FX names (default true)."},
        }),
        "handler": tool_get_context,
    },
    {
        "name": "scan_fx",
        "description": ("Enumerate FX and their parameters — one track or the whole "
                        "project (omit the track selector). Read-only. Lists only FX "
                        "ALREADY LOADED in the project, NOT your installed-plugin "
                        "library. Never use it to check whether a plugin exists before "
                        "adding one — that is the fx add resolver's job."),
        "inputSchema": _schema({
            **TRACK_PROPS,
            "include_values": {"type": "boolean", "description": "Add current/formatted value per parameter (much larger reply)."},
        }),
        "handler": tool_scan_fx,
    },
    {
        "name": "get_fx_parameters",
        "description": ("Full parameter list for ONE FX (auto-paginated): index, "
                        "name, normalized value, formatted display value. Scan "
                        "before setting parameters; prefer param_index from this "
                        "scan over name matching."),
        "inputSchema": _schema({
            **TRACK_PROPS,
            "fx_name_contains": {"type": "string"},
            "fx_index": {"type": "integer"},
            "fx_scope": {"type": "string", "enum": ["track", "input", "all"]},
            "param_name_contains": {"type": "string"},
            "include_values": {"type": "boolean", "description": "Default true."},
        }),
        "handler": tool_get_fx_parameters,
    },
    {
        "name": "get_track_routing",
        "description": ("Read a track's routing: sends, receives, parent bus, "
                        "volume, pan, phase, automation mode. Read-only."),
        "inputSchema": _schema(dict(TRACK_PROPS)),
        "handler": tool_get_track_routing,
    },
    {
        "name": "transport",
        "description": ("Transport and project timing: play, stop, pause, record, "
                        "set_cursor, set_time_selection, set_tempo. Mutations run "
                        "in an undo block (Ctrl/Cmd+Z reverts)."),
        "inputSchema": _schema({
            "action": {"type": "string",
                       "enum": ["play", "stop", "pause", "record", "set_cursor",
                                "set_time_selection", "set_tempo"]},
            "position": POSITION_PROP,
            "seek_play": {"type": "boolean"},
            "start": POSITION_PROP, "end": POSITION_PROP,
            "length_bars": {"type": "number"},
            "clear": {"type": "boolean", "description": "set_time_selection: clear it."},
            "bpm": {"type": "number"},
        }, required=["action"]),
        "handler": tool_transport,
    },
    {
        "name": "track",
        "description": ("Track operations: add, delete, rename, select, set_volume "
                        "(dB), set_pan (-1..1), mute, solo, arm, set_color. Every "
                        "mutation runs in a REAPER undo block. Supports dry_run. "
                        "delete is destructive — confirm intent first."),
        "inputSchema": _schema({
            "action": {"type": "string", "enum": list(_TRACK_ACTIONS)},
            **TRACK_PROPS, **DRY_RUN_PROP,
            "name": {"type": "string", "description": "add: new track name."},
            "index": {"type": "integer", "description": "add: 1-based insert position (omit to append)."},
            "new_name": {"type": "string"},
            "volume_db": {"type": "number"},
            "pan": {"type": "number"},
            "mute": {"type": "boolean"}, "solo": {"type": "boolean"},
            "armed": {"type": "boolean"},
            "exclusive": {"type": "boolean", "description": "select: deselect everything else."},
            "select": {"type": "boolean"},
            "color": {"type": "object", "description": "{r,g,b} 0-255."},
        }, required=["action"]),
        "handler": tool_track,
    },
    {
        "name": "fx",
        "description": ("FX chain operations: add (fuzzy plugin-name resolution "
                        "against REAPER's installed-plugin cache), remove, bypass, "
                        "move. Undo-block wrapped; supports dry_run (dry_run add "
                        "needs the exact fx_name, no fuzzy resolution). "
                        "TO ADD A PLUGIN, JUST RUN add IN ONE CALL. The resolver "
                        "against the installed-plugin cache IS the check — a plugin "
                        "being absent from the project is normal and expected (adding "
                        "it is the whole point). Do NOT precheck with scan_fx, do NOT "
                        "hedge about whether it's installed, do NOT narrate a plan. "
                        "Target the master with track:\"master\". The user may be "
                        "recording live; extra steps and preamble ruin the take."),
        "inputSchema": _schema({
            "action": {"type": "string", "enum": list(_FX_ACTIONS)},
            **TRACK_PROPS, **DRY_RUN_PROP,
            "fx_name": {"type": "string", "description": "add: plugin name or fuzzy query (e.g. 'pro q 4')."},
            "fx_name_contains": {"type": "string", "description": "remove/bypass/move: substring selector."},
            "fx_index": {"type": "integer"},
            "fx_scope": {"type": "string", "enum": ["track", "input", "all"],
                         "description": "Required with fx_index."},
            "bypass": {"type": "boolean"},
            "to_index": {"type": "integer"},
            "show": {"type": "boolean", "description": "add: open the FX window."},
        }, required=["action"]),
        "handler": tool_fx,
    },
    {
        "name": "set_fx_param",
        "description": ("Set one FX parameter. Give normalized_value (0-1), "
                        "formatted_value (e.g. '-16.00 dB', '80 Hz' — the bridge "
                        "binary-searches the normalized value whose display "
                        "matches), or relative ('+0.1'). Scan with "
                        "get_fx_parameters first and prefer param_index. "
                        "Undo-block wrapped; supports dry_run."),
        "inputSchema": _schema({
            **TRACK_PROPS, **DRY_RUN_PROP,
            "fx_name_contains": {"type": "string"},
            "fx_index": {"type": "integer"},
            "fx_scope": {"type": "string", "enum": ["track", "input", "all"]},
            "param_name_contains": {"type": "string"},
            "param_index": {"type": "integer"},
            "normalized_value": {"type": "number"},
            "formatted_value": {"type": "string"},
            "relative": {"type": "string"},
        }),
        "handler": tool_set_fx_param,
    },
    {
        "name": "get_fx_param_automation",
        "description": ("Read one existing FX-parameter envelope without creating "
                        "or arming it. Returns envelope state, inclusive-range "
                        "points, optional neighbors, automation items, duplicate "
                        "times, and a canonical content hash."),
        "inputSchema": _schema({
            **TRACK_PROPS,
            "fx_guid": {"type": "string"},
            "fx_name_contains": {"type": "string"},
            "fx_index": {"type": "integer"},
            "fx_scope": {"type": "string", "enum": ["track", "input", "all"]},
            "param_name_contains": {"type": "string"},
            "param_index": {"type": "integer"},
            "start_time": {"type": "number"},
            "end_time": {"type": "number"},
            "include_neighbors": {"type": "boolean"},
        }),
        "handler": tool_get_fx_param_automation,
    },
    {
        "name": "write_automation",
        "description": ("Write one FX-parameter envelope transactionally: "
                        "snapshot, inclusive-range replacement, reread "
                        "confirmation, and automatic rollback on any failure. "
                        "A declared range always means replacement."),
        "inputSchema": _schema({
            **TRACK_PROPS, **DRY_RUN_PROP,
            "fx_name_contains": {"type": "string"},
            "fx_index": {"type": "integer"},
            "fx_scope": {"type": "string", "enum": ["track", "input", "all"]},
            "param_name_contains": {"type": "string"},
            "param_index": {"type": "integer"},
            "points": {"type": "array", "items": {"type": "object"},
                       "description": "[{bar, beat, value, shape} | {time|seconds, value, shape}]"},
            "ranges": {"type": "array", "items": {"type": "object"},
                       "description": "[{start_time, end_time}] inclusive replacement scope"},
            "clear_existing_in_range": {"type": "boolean"},
        }, required=["points"]),
        "handler": tool_write_automation,
    },
    {
        "name": "apply_automation_transaction",
        "description": ("Atomically write several FX-parameter envelopes (e.g. "
                        "a stereo pair) in one undo block. Every envelope is "
                        "snapshotted before mutation; any failure restores all "
                        "of them and rereads the restoration before reporting."),
        "inputSchema": _schema({
            **DRY_RUN_PROP,
            "writes": {"type": "array", "items": {"type": "object"},
                       "description": ("[{target_track_guid|target_track_name|"
                                       "target_track_index, fx_guid|fx_name_contains|"
                                       "fx_index, param_index, points, ranges}]")},
            "transaction_id": {"type": "string"},
        }, required=["writes"]),
        "handler": tool_apply_automation_transaction,
    },
    {
        "name": "markers",
        "description": "Add or delete markers and regions (add_marker, add_region, delete_marker).",
        "inputSchema": _schema({
            "action": {"type": "string", "enum": sorted(_MARKER_ACTIONS)},
            **DRY_RUN_PROP,
            "position": POSITION_PROP,
            "start": POSITION_PROP, "end": POSITION_PROP,
            "length_bars": {"type": "number"},
            "name": {"type": "string"},
            "color": {"type": "object", "description": "{r,g,b} 0-255."},
            "marker_index": {"type": "integer"},
            "is_region": {"type": "boolean"},
        }, required=["action"]),
        "handler": tool_markers,
    },
    {
        "name": "insert_midi_file",
        "description": ("Insert a .mid file from disk onto a track at a position. "
                        "Write the MIDI yourself, then insert. Never overwrites "
                        "existing items unless replace_existing_in_range is true."),
        "inputSchema": _schema({
            **TRACK_PROPS, **DRY_RUN_PROP,
            "midi_path": {"type": "string", "description": "Absolute path to the .mid file."},
            "position": POSITION_PROP,
            "length": {"type": "object",
                       "description": "{type: bars|region|time_selection|seconds|as_generated, ...}"},
            "loop": {"type": "boolean"},
            "replace_existing_in_range": {"type": "boolean"},
        }, required=["midi_path"]),
        "handler": tool_insert_midi_file,
    },
    {
        "name": "delete_items_in_range",
        "description": ("Delete media items in a time range on one track (or "
                        "all_tracks). Destructive — confirm intent; undo-block "
                        "wrapped; supports dry_run."),
        "inputSchema": _schema({
            **TRACK_PROPS, **DRY_RUN_PROP,
            "range": POSITION_PROP,
            "length_bars": {"type": "number"},
            "length_seconds": {"type": "number"},
            "all_tracks": {"type": "boolean"},
        }),
        "handler": tool_delete_items_in_range,
    },
    {
        "name": "batch",
        "description": ("Run several bridge commands as ONE undo block and one "
                        "round-trip. commands: [{type, payload}]. Use for "
                        "multi-step edits (e.g. several set_fx_param calls) so a "
                        "failure stops cleanly and one Ctrl+Z reverts everything."),
        "inputSchema": _schema({
            **DRY_RUN_PROP,
            "commands": {"type": "array", "items": {"type": "object"},
                         "description": "[{\"type\": \"add_track\", \"payload\": {...}}, ...]"},
            "stop_on_error": {"type": "boolean", "description": "Default true."},
            "undo_label": {"type": "string"},
        }, required=["commands"]),
        "handler": tool_batch,
    },
    {
        "name": "capture_track_audio",
        "description": ("Render a track capture to WAV and return its evidence scope. "
                        "Only isolated_track with isolation_verified=true is per-track "
                        "audio; item-based tracks can report a full_mix fallback. "
                        "Gated: needs allow_risk_level_3=true in "
                        "bridge/bridge_config.json AND a bridge restart (relaunch "
                        "REAPER) after changing it. Synchronous — blocks the "
                        "bridge for the render duration."),
        "inputSchema": _schema({
            **TRACK_PROPS,
            "duration_seconds": {"type": "integer", "description": "Default 30, max 600. Starts at the edit cursor (or active time selection)."},
            "start_seconds": {"type": "number"},
            "output_file": {"type": "string", "description": "Optional; defaults to a unique temp path."},
        }),
        "handler": tool_capture_track_audio,
    },
    {
        "name": "profile_track",
        "description": ("Daemon Beater: profile a guitar stem bar by bar to plan "
                        "drums — onset density, IOI regularity, decay ratio "
                        "(palm-mute vs ringing), silence, low/bright balance, "
                        "RMS/crest, a 16th accent grid, plus suggested section "
                        "boundaries and repeated-section groups (A/B/A). "
                        "READS THE SAVED .rpp FILE ON DISK, NOT REAPER's live "
                        "project: unsaved edits are invisible, so on a dirty "
                        "project this analyzes stale material. The result "
                        "repeats that caveat — relay it, never present these "
                        "numbers as the current session. Numbers, not verdicts: "
                        "YOU propose the section labels and the user corrects "
                        "them. Point it at the DI track, not the amped stem "
                        "(distortion flattens the decay contrast) and say which "
                        "you used. Slow (up to 600 s on a long stem) — window it "
                        "with bars / start_bar / max_seconds."),
        "inputSchema": _schema({
            "project": {"type": "string",
                        "description": "Absolute path to the SAVED .rpp project file."},
            "track": {"type": "string",
                      "description": "Name of the guitar track to profile (prefer the DI)."},
            "start_bar": {"type": "integer",
                          "description": "First bar to profile, 0-indexed (default 0)."},
            "bars": {"type": "integer",
                     "description": "Bars to profile (omit for the whole first item)."},
            "max_seconds": {"type": "number",
                            "description": ("Analyze only N seconds from start_bar. Counts whole "
                                            "bars that fit; a cap shorter than one bar is refused, "
                                            "not padded.")},
        }, required=["project", "track"]),
        "handler": tool_profile_track,
    },
    {
        "name": "riff_grid",
        "description": ("Step 1 of the drum workflow: read a guitar stem's "
                        "transients into a proposed KICK GRID, printed at "
                        "100/50/30% attack strength. READS THE SAVED .rpp FILE "
                        "ON DISK, NOT REAPER's live project — same stale-material "
                        "caveat as profile_track, and the result carries it. "
                        "This is a PROPOSAL the user corrects, not an auto-beat: "
                        "transients say WHEN a note is picked, never whether it "
                        "rings open or is palm-muted, so the percentile is an "
                        "attack-strength heuristic. The 30% row is the sparse "
                        "slam/breakdown feel (the default); the 100% row turns "
                        "every pick attack into a kick (gallops/triplets). The "
                        "grid is anchored to item time 0, so a stem whose "
                        "downbeat sits a step off reads a 16th early — check the "
                        "first render. Transcribe the row the user picks into a "
                        "DSL, then insert_groove."),
        "inputSchema": _schema({
            "project": {"type": "string",
                        "description": "Absolute path to the SAVED .rpp project file."},
            "track": {"type": "string",
                      "description": "Name of the guitar track to read."},
            "bars": {"type": "integer", "description": "Bars to read (default 4)."},
            "start_bar": {"type": "integer",
                          "description": "First bar to read, 0-indexed (default 0)."},
        }, required=["project", "track"]),
        "handler": tool_riff_grid,
    },
    {
        "name": "insert_groove",
        "description": ("Render a drum DSL to MIDI and insert it on a track in "
                        "ONE call — the engine (skills/drum-apparatus/) humanizes "
                        "velocity, fatigue and timing at placement. Give the DSL "
                        "EITHER inline as dsl_text OR as a file with dsl_path, "
                        "never both. MUTATES the project (undo-block wrapped, one "
                        "Ctrl/Cmd+Z reverts); supports dry_run. Refuses in ~0 s "
                        "when the bridge heartbeat is dead, before generating "
                        "anything. The rendered .mid is KEPT under "
                        "rendered-midi/ — REAPER imports MIDI by reference on "
                        "this build, so a deleted render leaves an empty take. "
                        "Kit map: the "
                        "DSL's own @map wins, then this map argument, then "
                        "drum-config.json, then GM Standard. Track: named track, "
                        "else drum-config.json's default, else REAPER's selected "
                        "track. A bad DSL comes back as DSL_ERROR with the "
                        "engine's own message — fix the DSL from it."),
        "inputSchema": _schema({
            **TRACK_PROPS, **DRY_RUN_PROP,
            "dsl_text": {"type": "string",
                         "description": "The groove DSL inline. Mutually exclusive with dsl_path."},
            "dsl_path": {"type": "string",
                         "description": "Absolute path to a .dsl file. Mutually exclusive with dsl_text."},
            "position": {"description": ("Where to insert: a position object like "
                                         "{\"type\":\"bar\",\"bar\":33} (see insert_midi_file) or a "
                                         "plain number of seconds. Default: the edit cursor.")},
            "map": {"type": "string",
                    "description": "Drum-kit map name (see reaperd.py list-maps). The DSL's @map wins over this."},
            "seed": {"type": "integer",
                     "description": "RNG seed for the humanizer; same seed + same DSL = same MIDI."},
        }),
        "handler": tool_insert_groove,
    },
    {
        "name": "insert_riff",
        "description": ("Render ONE humanized guitar or bass part and insert it "
                        "on a track (skills/guitar-apparatus/). Write the riff "
                        "as bars_text — one 16-step bar per line, e.g. "
                        "'x.x.x.x.x.x.x.x.' — or point at a file with "
                        "bars_file, never both; omit both to use the built-in "
                        "'demo' riff. Step alphabet: '.' rest, 'x' muted chug, "
                        "'X' accented root, 'o' let-ring root, 'g' ghost, '_' "
                        "tie the previous note through this step, '~' slide "
                        "into the next; UPPERCASE note names (E F G A B C D) "
                        "are power chords in the key of E and lowercase are "
                        "single notes, so case matters. Read "
                        "skills/guitar-apparatus/SKILL.md for the full table "
                        "before writing anything beyond chugs. Double tracking "
                        "is two performances, not a copy: call this twice with "
                        "DIFFERENT seeds for the left and right guitar. MUTATES "
                        "the project (undo-block wrapped, one Ctrl/Cmd+Z "
                        "reverts). Refuses in ~0 s when the bridge heartbeat is "
                        "dead. The rendered .mid is kept under rendered-midi/, "
                        "including on a dry run, because REAPER holds it by "
                        "reference."),
        "inputSchema": _schema({
            **DRY_RUN_PROP,
            "track": {"type": "string",
                      "description": "Exact name of the track to insert on."},
            "part": {"type": "string", "enum": ["guitar", "bass"],
                     "description": "Which engine to play the riff on. Default guitar."},
            "bars_text": {"type": "string",
                          "description": "The riff inline, one bar per line. Mutually exclusive with bars_file."},
            "bars_file": {"type": "string",
                          "description": "Absolute path to a riff text file. Mutually exclusive with bars_text."},
            "riff": {"type": "string",
                     "description": "Built-in riff when neither bars source is given: 'demo' or 'probe'."},
            "map": {"type": "string",
                    "description": "Tuning/keyswitch map: argent_e (default), argent_csharp, nolly_e, nolly_csharp."},
            "seed": {"type": "integer",
                     "description": "RNG seed. Same riff + same seed = the same performance."},
            "low_string": {"type": "integer",
                           "description": "Override the map's low-string MIDI note (e.g. 52 to lift a lead into register)."},
            "position": {"type": "number",
                         "description": "Seconds from project start. Default: the edit cursor."},
            "tempo": {"type": "integer", "description": "Project tempo override (BPM)."},
            "replace": {"type": "boolean",
                        "description": "Clear the track from `position` first, so a re-cut does not stack takes. Needs a numeric position."},
        }, ["track"]),
        "handler": tool_insert_riff,
    },
    {
        "name": "cut_band",
        "description": ("Lay down or re-cut a whole four-track jam in ONE call: "
                        "two double-tracked guitars, a bass locked to them, and "
                        "drums. The two guitars get different seeds "
                        "automatically, so the width is two performances rather "
                        "than a stereo copy. By default it REPLACES what is on "
                        "those four tracks from `position` — pass replace=false "
                        "to append instead. Every leg is attempted even if an "
                        "earlier one fails, and the result reports each leg "
                        "separately, so a partial cut says which tracks landed. "
                        "MUTATES the project (undo-block wrapped). Refuses in "
                        "~0 s when the bridge heartbeat is dead."),
        "inputSchema": _schema({
            **DRY_RUN_PROP,
            "guitar_l": {"type": "string", "description": "Left guitar track name. Default argent-l."},
            "guitar_r": {"type": "string", "description": "Right guitar track name. Default argent-r."},
            "bass": {"type": "string", "description": "Bass track name. Default nolly-bass-library."},
            "drums": {"type": "string", "description": "Drum track name. Default rs-drums-monarch."},
            "bars_file": {"type": "string",
                          "description": "Absolute path to a riff text file for all three string parts."},
            "riff": {"type": "string",
                     "description": "Built-in riff when bars_file is absent: 'demo' or 'probe'."},
            "dsl_path": {"type": "string",
                         "description": "Absolute path to the drum DSL. Default: the bundled examples/jam-e.dsl."},
            "map": {"type": "string", "description": "Guitar tuning map. Default argent_e."},
            "drum_map": {"type": "string", "description": "Drum-kit map name. Default 'RS Monarch'."},
            "seeds": {"type": "array", "items": {"type": "integer"},
                      "description": "Seeds for [guitar_l, guitar_r, bass]. Default [101, 202, 303]."},
            "low_string": {"type": "integer", "description": "Override the map's low-string MIDI note."},
            "position": {"type": "number", "description": "Seconds from project start. Default 0 (bar 1)."},
            "tempo": {"type": "integer", "description": "Project tempo override (BPM)."},
            "replace": {"type": "boolean",
                        "description": "Clear each track from `position` first. Default true."},
        }),
        "handler": tool_cut_band,
    },
    {
        "name": "humanize_take",
        "description": ("Put a dynamic contour and micro-timing into a drum "
                        "take that is ALREADY on a track — the take is read, "
                        "planned, and written back in place through "
                        "index-addressed note edits, so stacked hits (flams, "
                        "double triggers) are humanized too. The contour is "
                        "always applied; `amount` only scales the random spread "
                        "and timing looseness on top of it. Fills build as a "
                        "crescendo peaking at the resolve, and the golden rule "
                        "is enforced: no drum hits the same velocity twice in a "
                        "row. Use follow_lead when the user has already "
                        "humanized the opening bars BY HAND and wants that hand "
                        "carried across the rest — it learns from their bars "
                        "instead of applying the shared taste model, and fails "
                        "if there is no flat region left to follow into. Prefer "
                        "dry_run first: it plans and returns the full summary "
                        "without writing. MUTATES the project (undo-block "
                        "wrapped, one Ctrl/Cmd+Z reverts the whole pass)."),
        "inputSchema": _schema({
            **DRY_RUN_PROP,
            "track": {"type": "string", "description": "Exact name of the drum track."},
            "item_index": {"type": "integer",
                           "description": "Which item on the track. Default: the only item."},
            "amount": {"type": "integer",
                       "description": "0-100 random spread and timing looseness. Default 25."},
            "map": {"type": "string",
                    "description": "Drum-kit map for exact per-role velocity bands. Default: infer roles from the take's MIDI note names."},
            "seed": {"type": "integer", "description": "RNG seed; a run is reproducible."},
            "follow_lead": {"type": "boolean",
                            "description": "Learn the velocity hand from the bars the user humanized and carry it across the rest."},
            "example_through_bar": {"type": "integer",
                                    "description": "With follow_lead: the last bar the user humanized (1-based). Default: auto-detect the first flat bar."},
        }, ["track"]),
        "handler": tool_humanize_take,
    },
    {
        "name": "verify_change",
        "description": ("Run ONE mutating bridge command with MEASURED proof: "
                        "capture the track, apply the command, capture the "
                        "same frozen window again (track pinned by GUID), and "
                        "report audio deltas (LUFS-I always; spectrum/peak/"
                        "stereo with Post Mortem). This REALLY mutates the "
                        "project (undo-block wrapped; one Ctrl/Cmd+Z reverts) "
                        "— confirm intent first for destructive command types "
                        "(delete_track, delete_items_in_range, remove_fx). "
                        "Costs TWO renders; each blocks REAPER's UI for the "
                        "capture duration. Statuses: VERIFIED (deltas are "
                        "real measurements); REFUSED (refused before the "
                        "mutation was sent, no mutation ran); UNVERIFIED "
                        "(the project MAY have changed: applied, partial "
                        "batch, rejected by the bridge with a possible "
                        "mid-edit partial change, or unknown outcome; not "
                        "measured either way; NOT rolled back; do NOT retry "
                        "blindly). Relay the "
                        "status honestly; never present UNVERIFIED as "
                        "success. Needs allow_risk_level_3 (see "
                        "capture_track_audio)."),
        "inputSchema": _schema({
            "track": {"type": "string",
                      "description": "Exact track name (case-insensitive) or 'master'."},
            "command_type": {"type": "string",
                             "description": "Bridge command to run (set_fx_param, set_track_volume, batch, ...)."},
            "payload": {"type": "object",
                        "description": "The command's payload, as for raw_command."},
            "seconds": {"type": "number",
                        "description": "Capture length 1-60 (default 10)."},
        }, required=["track", "command_type", "payload"]),
        "handler": tool_verify_change,
    },
    {
        "name": "tune_param",
        "description": ("Outcome-driven parameter search: iteratively set ONE "
                        "FX parameter and re-measure until a target audio "
                        "outcome is hit (e.g. bass LUFS-I down 3 dB), then "
                        "report the measured result. Target: {\"metric\": "
                        "\"lufs_i\"|\"band_db\", \"delta\": -3.0, "
                        "\"tolerance\": 0.5, \"band_hz\": [lo,hi] for "
                        "band_db (needs Post Mortem)}. delta is relative to "
                        "the baseline measurement. ASSUMES the metric moves "
                        "monotonically with the parameter (gain-like params); "
                        "stops with NON_MONOTONE and restores the initial "
                        "value when that is violated. EXPENSIVE: baseline + "
                        "up to 5 iterations, each a render that blocks "
                        "REAPER's UI — warn the user before calling. Each "
                        "set is one undo point. On UNCONVERGED/UNREACHABLE "
                        "the best-observed value stays applied and the "
                        "result says so honestly; `final` always carries the "
                        "READ-BACK live parameter state. Requires ONE FX "
                        "selector (fx_name_contains or fx_index) AND one "
                        "parameter selector (param_index or "
                        "param_name_contains). Needs allow_risk_level_3."),
        "inputSchema": _schema({
            "track": {"type": "string",
                      "description": "Exact track name (case-insensitive)."},
            "fx_name_contains": {"type": "string",
                                 "description": "FX selector by substring."},
            "fx_index": {"type": "integer",
                         "description": "FX selector by index (with fx_scope)."},
            "fx_scope": {"type": "string", "enum": ["track", "input"],
                         "description": "Required meaning for fx_index (default track)."},
            "param_index": {"type": "integer",
                            "description": "Parameter index (preferred; scan first)."},
            "param_name_contains": {"type": "string",
                                    "description": "Parameter by unique substring."},
            "target": {
                "type": "object",
                "description": ("The measured outcome to hit, relative to "
                                "the baseline capture."),
                "properties": {
                    "metric": {"type": "string",
                               "enum": ["lufs_i", "band_db"]},
                    "delta": {"type": "number",
                              "description": "Nonzero change vs baseline (e.g. -3.0)."},
                    "tolerance": {"type": "number",
                                  "description": "Convergence window (default 0.5)."},
                    "band_hz": {"type": "array",
                                "items": {"type": "number"},
                                "minItems": 2, "maxItems": 2,
                                "description": "[low, high] Hz — required for band_db."},
                },
                "required": ["metric", "delta"],
            },
            "seconds": {"type": "number",
                        "description": "Capture length 1-60 (default 10)."},
        }, required=["track", "target"]),
        "handler": tool_tune_param,
    },
    {
        "name": "analyze_track",
        "description": ("Post Mortem: capture one verified-isolated track and "
                        "return MEASURED mix data (FX chain with values, routing, "
                        "LUFS, true peak, crest, 1/3-octave spectrum, stereo "
                        "image, silence fraction) for YOU to diagnose. Requires "
                        "Post Mortem installed and capture enabled (see "
                        "capture_track_audio gating). Full-mix fallbacks are refused. "
                        "Park the edit cursor where "
                        "the track is playing first."),
        "inputSchema": _schema({
            "track": {"type": "string", "description": "Track name (case-insensitive, unique substring ok)."},
            "seconds": {"type": "integer", "description": "Capture length, default 10."},
        }, required=["track"]),
        "handler": tool_analyze_track,
    },
    {
        "name": "compare_tracks",
        "description": ("Post Mortem cross-track masking: capture 2+ verified-isolated "
                        "tracks "
                        "and return their spectra plus a contested-band masking "
                        "table for YOU to diagnose. Full-mix fallbacks are refused. "
                        "Same requirements as "
                        "analyze_track."),
        "inputSchema": _schema({
            "tracks": {"type": "array", "items": {"type": "string"},
                       "description": "Two or more track names."},
            "seconds": {"type": "integer", "description": "Capture length per track, default 30."},
        }, required=["tracks"]),
        "handler": tool_compare_tracks,
    },
    {
        "name": "complete_postmortem_onboarding",
        "description": ("After analyze_track, send the exact diagnosis you wrote "
                        "to the Post Mortem panel. Requires a fresh matching "
                        "10-second single-track handoff; comparisons cannot complete "
                        "first-run onboarding."),
        "inputSchema": _schema({
            "track": {"type": "string", "description": "The track analyzed by analyze_track."},
            "diagnosis": {"type": "string", "description": "The full diagnosis to render in the panel."},
        }, required=["track", "diagnosis"]),
        "handler": tool_complete_postmortem_onboarding,
    },
    {
        "name": "raw_command",
        "description": ("Escape hatch: send any bridge command by type + payload "
                        "(full reference: bridge/command_schema.md). Use when no "
                        "dedicated tool covers it."),
        "inputSchema": _schema({
            **DRY_RUN_PROP,
            "type": {"type": "string"},
            "payload": {"type": "object"},
            "timeout_ms": {"type": "integer"},
        }, required=["type"]),
        "handler": tool_raw_command,
    },
]

_TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------

def _rpc_result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle_message(msg):
    """One parsed JSON-RPC message -> response dict, or None (notification)."""
    method = msg.get("method")
    mid = msg.get("id")

    # A notification (no id) must never get a response — and never executes,
    # so a malformed fire-and-forget tools/call can't silently mutate the
    # project with nobody reading the result.
    if "id" not in msg:
        return None

    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return _rpc_result(mid, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "ping":
        return _rpc_result(mid, {})
    if method == "tools/list":
        tools = [{"name": t["name"], "description": t["description"],
                  "inputSchema": t["inputSchema"]} for t in TOOLS]
        return _rpc_result(mid, {"tools": tools})
    if method == "tools/call":
        params = msg.get("params") or {}
        tool = _TOOL_BY_NAME.get(params.get("name"))
        if tool is None:
            return _rpc_error(mid, -32602, f"unknown tool: {params.get('name')!r}")
        args = params.get("arguments") or {}
        try:
            return _rpc_result(mid, tool["handler"](args))
        except Exception as e:  # tool bug -> tool-level error, keep serving
            _log(f"tool {params.get('name')} crashed: {e!r}")
            return _rpc_result(mid, _text(f"tool error: {e!r}", is_error=True))
    return _rpc_error(mid, -32601, f"method not found: {method!r}")


def main():
    # JSON-RPC frames must be clean UTF-8 lines; logs go to stderr only.
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    _log(f"serving bridge root {BRIDGE_ROOT}")
    while True:
        line = sys.stdin.readline()
        if not line:  # EOF: client is gone
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            response = _rpc_error(None, -32700, "parse error")
        else:
            if not isinstance(msg, dict):
                response = _rpc_error(None, -32600, "invalid request")
            else:
                response = handle_message(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
