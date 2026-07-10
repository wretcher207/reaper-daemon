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
command is on PATH (or set POSTMORTEM_CMD). They return measured payloads;
the model on the client side does the diagnosing.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SERVER_DIR)

import reaperd  # noqa: E402  (single-file CLI next to this script; import is safe)

BRIDGE_ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("REAPER_DAEMON_ROOT") or SERVER_DIR
))

SERVER_INFO = {"name": "reaper-daemon", "version": "1.0.0"}
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
DEFAULT_PROTOCOL = "2025-06-18"

DEFAULT_TIMEOUT_MS = 15000
CAPTURE_TIMEOUT_MS = 180000


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


def _reply_result(reply):
    """Format a bridge reply as an MCP tool result."""
    if reply.get("ok"):
        body = {"ok": True}
        if reply.get("message"):
            body["message"] = reply["message"]
        if reply.get("data") is not None:
            body["data"] = reply["data"]
        if reply.get("dry_run"):
            body["dry_run"] = True
        return _text(json.dumps(body, indent=1))
    return _text(json.dumps({"ok": False, "error": reply.get("error")}, indent=1),
                 is_error=True)


def _track_payload(args):
    """Shared track selector -> bridge payload fields."""
    return {
        "target_track_name": args.get("track"),
        "track_name_contains": args.get("track_contains"),
        "use_selected_track": True if args.get("use_selected_track") else None,
    }


TRACK_PROPS = {
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
    return _reply_result(_send("get_context",
                               {"include_fx": args.get("include_fx", True)}))


def tool_scan_fx(args):
    payload = _track_payload(args)
    payload["include_values"] = args.get("include_values", False)
    return _reply_result(_send("scan_fx", payload, timeout_ms=30000))


def tool_get_fx_parameters(args):
    base = _track_payload(args)
    for key in ("fx_name_contains", "fx_index", "fx_scope", "param_name_contains"):
        if args.get(key) is not None:
            base[key] = args[key]
    base = {k: v for k, v in base.items() if v is not None}
    data, err = reaperd.scan_fx_parameter_data(
        base, BRIDGE_ROOT, include_values=args.get("include_values", True))
    if err:
        return _text(json.dumps({"ok": False, "error": err}, indent=1), is_error=True)
    return _text(json.dumps({"ok": True, **data}, indent=1))


def tool_get_track_routing(args):
    return _reply_result(_send("get_track_routing", _track_payload(args)))


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
    payload = _track_payload(args)
    payload.update({
        "fx_name_contains": args.get("fx_name_contains"),
        "fx_index": args.get("fx_index"), "fx_scope": args.get("fx_scope"),
        "param_name_contains": args.get("param_name_contains"),
        "param_index": args.get("param_index"),
        "normalized_value": args.get("normalized_value"),
        "formatted_value": args.get("formatted_value"),
        "relative": args.get("relative"),
    })
    return _reply_result(_send("set_fx_param", payload,
                               dry_run=bool(args.get("dry_run"))))


def tool_write_automation(args):
    payload = _track_payload(args)
    payload.update({
        "fx_name_contains": args.get("fx_name_contains"),
        "fx_index": args.get("fx_index"), "fx_scope": args.get("fx_scope"),
        "param_name_contains": args.get("param_name_contains"),
        "param_index": args.get("param_index"),
        "points": args.get("points"),
        "clear_existing_in_range": args.get("clear_existing_in_range"),
    })
    return _reply_result(_send("write_fx_param_automation", payload,
                               timeout_ms=30000, dry_run=bool(args.get("dry_run"))))


_MARKER_ACTIONS = {"add_marker", "add_region", "delete_marker"}


def tool_markers(args):
    action = args.get("action")
    if action not in _MARKER_ACTIONS:
        return _text(f"unknown markers action: {action!r}", is_error=True)
    payload = {
        "position": args.get("position"), "start": args.get("start"),
        "end": args.get("end"), "length_bars": args.get("length_bars"),
        "name": args.get("name"), "color": args.get("color"),
        "marker_index": args.get("marker_index"),
        "is_region": args.get("is_region"),
    }
    return _reply_result(_send(action, payload, dry_run=bool(args.get("dry_run"))))


def tool_insert_midi_file(args):
    payload = _track_payload(args)
    payload.update({
        "midi_path": args.get("midi_path"),
        "position": args.get("position") or {"type": "cursor"},
        "length": args.get("length"), "loop": args.get("loop"),
        "replace_existing_in_range": args.get("replace_existing_in_range"),
    })
    return _reply_result(_send("insert_midi_file", payload, timeout_ms=30000,
                               dry_run=bool(args.get("dry_run"))))


def tool_delete_items_in_range(args):
    payload = _track_payload(args)
    payload.update({
        "range": args.get("range"), "length_bars": args.get("length_bars"),
        "length_seconds": args.get("length_seconds"),
        "all_tracks": args.get("all_tracks"),
    })
    return _reply_result(_send("delete_items_in_range", payload,
                               dry_run=bool(args.get("dry_run"))))


def tool_batch(args):
    payload = {
        "commands": args.get("commands"),
        "stop_on_error": args.get("stop_on_error", True),
        "undo_label": args.get("undo_label"),
    }
    return _reply_result(_send("batch", payload, timeout_ms=60000,
                               dry_run=bool(args.get("dry_run"))))


def tool_capture_track_audio(args):
    payload = _track_payload(args)
    seconds = args.get("duration_seconds", 30)
    output = args.get("output_file")
    if not output:
        temp_dir = os.path.join(tempfile.gettempdir(), "reaper-mcp")
        os.makedirs(temp_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        output = os.path.join(temp_dir, f"capture-{stamp}.wav")
    payload.update({"duration_seconds": seconds, "output_file": output,
                    "start_seconds": args.get("start_seconds")})
    return _reply_result(_send("capture_track_audio", payload,
                               timeout_ms=CAPTURE_TIMEOUT_MS))


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
data. An honest "I'm not sure" beats a confident wrong answer."""

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
problem to look useful."""


def _postmortem_cmdline():
    override = os.environ.get("POSTMORTEM_CMD")
    if override:
        return override.split()
    exe = shutil.which("postmortem")
    return [exe] if exe else None


def _run_postmortem(tracks, seconds, preamble):
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
    warnings = []
    try:
        payload = json.loads(payload_text)
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
    except ValueError:
        pass  # not fatal; pass the raw payload through
    parts = [preamble]
    parts.extend(warnings)
    parts.append(payload_text)
    return _text("\n\n".join(parts))


def tool_analyze_track(args):
    track = args.get("track")
    if not track or not isinstance(track, str):
        return _text("analyze_track needs a 'track' name", is_error=True)
    return _run_postmortem([track], args.get("seconds", 30), ANALYZE_PREAMBLE)


def tool_compare_tracks(args):
    tracks = args.get("tracks") or []
    if len(tracks) < 2:
        return _text("compare_tracks needs at least two track names", is_error=True)
    return _run_postmortem(tracks, args.get("seconds", 30), COMPARE_PREAMBLE)


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
        "name": "write_automation",
        "description": ("Write an FX-parameter automation envelope: points with "
                        "bar/beat or time, normalized 0-1 values, shapes (linear, "
                        "square, slow, fast, bezier). Undo-block wrapped."),
        "inputSchema": _schema({
            **TRACK_PROPS, **DRY_RUN_PROP,
            "fx_name_contains": {"type": "string"},
            "fx_index": {"type": "integer"},
            "fx_scope": {"type": "string", "enum": ["track", "input", "all"]},
            "param_name_contains": {"type": "string"},
            "param_index": {"type": "integer"},
            "points": {"type": "array", "items": {"type": "object"},
                       "description": "[{bar, beat, value, shape} | {time|seconds, value, shape}]"},
            "clear_existing_in_range": {"type": "boolean"},
        }, required=["points"]),
        "handler": tool_write_automation,
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
        "description": ("Render one track's post-FX output to a WAV (stems render, "
                        "pre-master; user's selection/render settings restored "
                        "after). Gated: needs allow_risk_level_3=true in "
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
        "name": "analyze_track",
        "description": ("Post Mortem: capture a 30s post-FX stem of one track and "
                        "return MEASURED mix data (FX chain with values, routing, "
                        "LUFS, true peak, crest, 1/3-octave spectrum, stereo "
                        "image, silence fraction) for YOU to diagnose. Requires "
                        "Post Mortem installed and capture enabled (see "
                        "capture_track_audio gating). Park the edit cursor where "
                        "the track is playing first."),
        "inputSchema": _schema({
            "track": {"type": "string", "description": "Track name (case-insensitive, unique substring ok)."},
            "seconds": {"type": "integer", "description": "Capture length, default 30."},
        }, required=["track"]),
        "handler": tool_analyze_track,
    },
    {
        "name": "compare_tracks",
        "description": ("Post Mortem cross-track masking: capture 2+ tracks' stems "
                        "and return their spectra plus a contested-band masking "
                        "table for YOU to diagnose. Same requirements as "
                        "analyze_track."),
        "inputSchema": _schema({
            "tracks": {"type": "array", "items": {"type": "string"},
                       "description": "Two or more track names."},
            "seconds": {"type": "integer", "description": "Capture length per track, default 30."},
        }, required=["tracks"]),
        "handler": tool_compare_tracks,
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
            if isinstance(msg, list):
                # JSON-RPC batch (allowed by the pre-2025-06-18 protocol
                # versions we advertise): answer each request, one array back.
                # An empty batch is invalid; all-notifications gets no reply.
                if not msg:
                    response = _rpc_error(None, -32600, "invalid request")
                else:
                    answers = [r for r in (
                        handle_message(m) if isinstance(m, dict)
                        else _rpc_error(None, -32600, "invalid request")
                        for m in msg
                    ) if r is not None]
                    response = answers or None
            elif not isinstance(msg, dict):
                response = _rpc_error(None, -32600, "invalid request")
            else:
                response = handle_message(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
