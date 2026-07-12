"""Tests for the MCP stdio server (reaper_mcp.py).

Protocol behavior is tested in-process via handle_message; the wire path is
tested end-to-end through the real file queue with a fake bridge thread (same
pattern as test_reaperd.py); and one subprocess smoke test proves the stdio
framing (stdout carries only JSON-RPC lines). No live REAPER needed.
"""

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reaper_mcp  # noqa: E402
from bridge_fakes import fake_bridge  # noqa: E402


@pytest.fixture
def root(root, monkeypatch):
    """Overrides conftest's root: same folders, plus the MCP module pointed
    at it (BRIDGE_ROOT is resolved at import time from the environment)."""
    monkeypatch.setattr(reaper_mcp, "BRIDGE_ROOT", root)
    monkeypatch.setenv("POSTMORTEM_DATA_DIR", os.path.join(root, "postmortem-data"))
    return root


def rpc(method, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "method": method, "id": mid}
    if params is not None:
        msg["params"] = params
    return msg


def call(name, arguments=None, mid=1):
    return reaper_mcp.handle_message(
        rpc("tools/call", {"name": name, "arguments": arguments or {}}, mid))


def result_text(response):
    return response["result"]["content"][0]["text"]


# --- protocol basics --------------------------------------------------------

def test_initialize_echoes_supported_version():
    resp = reaper_mcp.handle_message(
        rpc("initialize", {"protocolVersion": "2025-03-26"}))
    assert resp["result"]["protocolVersion"] == "2025-03-26"
    assert resp["result"]["serverInfo"]["name"] == "reaper-daemon"
    assert "tools" in resp["result"]["capabilities"]


def test_initialize_falls_back_on_unknown_version():
    resp = reaper_mcp.handle_message(
        rpc("initialize", {"protocolVersion": "1999-01-01"}))
    assert resp["result"]["protocolVersion"] == reaper_mcp.DEFAULT_PROTOCOL


def test_tools_list_names_and_schemas():
    resp = reaper_mcp.handle_message(rpc("tools/list"))
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    for expected in ("get_status", "get_context", "scan_fx", "track", "fx",
                     "set_fx_param", "batch", "capture_track_audio",
                     "analyze_track", "compare_tracks",
                     "complete_postmortem_onboarding", "raw_command"):
        assert expected in tools
        assert tools[expected]["inputSchema"]["type"] == "object"
        assert tools[expected]["description"]


def test_unknown_method_is_32601():
    resp = reaper_mcp.handle_message(rpc("resources/list"))
    assert resp["error"]["code"] == -32601


def test_notifications_get_no_response():
    assert reaper_mcp.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_notification_tools_call_is_not_executed(monkeypatch):
    calls = []
    monkeypatch.setitem(
        reaper_mcp._TOOL_BY_NAME, "get_status",
        {"name": "get_status", "handler": lambda a: calls.append(a)})
    resp = reaper_mcp.handle_message(
        {"jsonrpc": "2.0", "method": "tools/call",
         "params": {"name": "get_status", "arguments": {}}})
    assert resp is None
    assert calls == []  # fire-and-forget calls must not mutate anything


def test_ping_returns_empty_result():
    assert reaper_mcp.handle_message(rpc("ping"))["result"] == {}


def test_unknown_tool_is_32602():
    resp = call("no_such_tool")
    assert resp["error"]["code"] == -32602


def test_tool_crash_becomes_tool_error_not_server_death(monkeypatch):
    monkeypatch.setitem(
        reaper_mcp._TOOL_BY_NAME,
        "get_status",
        {"name": "get_status", "handler": lambda a: 1 / 0},
    )
    resp = call("get_status")
    assert resp["result"]["isError"] is True
    assert "tool error" in result_text(resp)


# --- wire path through the real file queue ----------------------------------

def test_get_context_round_trip(root):
    fake_bridge(root, {"ok": True, "type": "get_context",
                       "data": {"project_name": "Song", "tempo": 174}})
    resp = call("get_context")
    body = json.loads(result_text(resp))
    assert body["ok"] is True
    assert body["data"]["tempo"] == 174
    assert "isError" not in resp["result"]


def test_scan_fx_round_trip_preserves_track_and_fx_guids(root):
    data = {
        "tracks": [{
            "index": 2,
            "name": "Guitar",
            "guid": "{TRACK-GUITAR}",
            "fx": [{
                "index": 0,
                "api_index": 0,
                "scope": "track",
                "name": "VST3: Amp",
                "guid": "{FX-AMP}",
            }],
        }],
    }
    fake_bridge(root, {"ok": True, "type": "scan_fx", "data": data})
    resp = call("scan_fx", {"track": "Guitar"})
    body = json.loads(result_text(resp))
    track = body["data"]["tracks"][0]
    assert track["guid"] == "{TRACK-GUITAR}"
    assert track["fx"][0]["guid"] == "{FX-AMP}"
    assert track["fx"][0]["scope"] == "track"


def test_get_fx_parameters_round_trip_preserves_track_and_fx_guids(root):
    data = {
        "track": {"index": 2, "name": "Guitar", "guid": "{TRACK-GUITAR}"},
        "fx": {
            "index": 0,
            "api_index": 0,
            "scope": "track",
            "name": "VST3: Amp",
            "guid": "{FX-AMP}",
            "parameter_count": 1,
        },
        "parameters": [{"index": 0, "name": "Gain"}],
        "paging": {"has_more": False},
    }
    fake_bridge(root, {"ok": True, "type": "get_fx_parameters", "data": data})
    resp = call("get_fx_parameters", {"track": "Guitar", "fx_index": 0,
                                       "fx_scope": "track"})
    body = json.loads(result_text(resp))
    assert body["track"]["guid"] == "{TRACK-GUITAR}"
    assert body["fx"]["guid"] == "{FX-AMP}"
    assert body["fx"]["api_index"] == 0
    assert body["parameters"][0]["name"] == "Gain"


def test_bridge_error_becomes_is_error(root):
    fake_bridge(root, {"ok": False, "type": "get_track_routing",
                       "error": {"code": "NO_TARGET_TRACK", "details": "no track"}})
    resp = call("get_track_routing", {"track": "Nope"})
    assert resp["result"]["isError"] is True
    assert "NO_TARGET_TRACK" in result_text(resp)


def test_dry_run_reaches_the_envelope_not_the_payload(root):
    seen = []
    fake_bridge(root, {"ok": True, "type": "set_track_volume", "data": {}},
                record=seen)
    resp = call("track", {"action": "set_volume", "track": "Drums",
                          "volume_db": -3.0, "dry_run": True})
    assert "isError" not in resp["result"]
    assert seen and seen[0]["dry_run"] is True
    assert seen[0]["payload"] == {"target_track_name": "Drums", "volume_db": -3.0}
    assert seen[0]["created_by"] == "mcp"


def test_none_valued_optionals_are_dropped_from_payload(root):
    seen = []
    fake_bridge(root, {"ok": True, "type": "mute_track", "data": {}}, record=seen)
    call("track", {"action": "mute", "track": "Bass", "mute": False})
    assert seen[0]["payload"] == {"target_track_name": "Bass", "mute": False}


def test_unknown_track_action_is_tool_error_without_bridge(root):
    resp = call("track", {"action": "explode"})
    assert resp["result"]["isError"] is True
    assert "explode" in result_text(resp)


def test_raw_command_requires_type(root):
    resp = call("raw_command", {})
    assert resp["result"]["isError"] is True


def test_raw_command_bad_timeout_rejected_before_sending(root):
    resp = call("raw_command", {"type": "play", "timeout_ms": "abc"})
    assert resp["result"]["isError"] is True
    assert "timeout_ms" in result_text(resp)
    # Nothing may reach the queue: an orphaned command would execute later.
    assert os.listdir(os.path.join(root, "inbox")) == []


def test_analyze_track_requires_track_name():
    resp = call("analyze_track", {})
    assert resp["result"]["isError"] is True
    assert "track" in result_text(resp)


def test_get_status_reports_dead_bridge(root, monkeypatch):
    monkeypatch.setattr(reaper_mcp.reaperd, "reaper_running", lambda: False)
    resp = call("get_status")
    info = json.loads(result_text(resp))
    assert info["alive"] is False
    assert resp["result"]["isError"] is True
    assert info["bridge_root"] == root


def test_get_status_reports_live_bridge_and_risk_gate(root, monkeypatch):
    hb = os.path.join(root, "bridge", "heartbeat.json")
    with open(hb, "w", encoding="utf-8") as f:
        json.dump({"alive_at": "now", "bridge_version": 3,
                   "project_name": "Song"}, f)
    with open(os.path.join(root, "bridge", "bridge_config.json"), "w",
              encoding="utf-8") as f:
        json.dump({"allow_risk_level_3": False}, f)
    monkeypatch.setattr(reaper_mcp.reaperd, "reaper_running", lambda: True)
    resp = call("get_status")
    info = json.loads(result_text(resp))
    assert info["alive"] is True
    assert info["allow_risk_level_3"] is False
    assert info["heartbeat"]["project_name"] == "Song"


def test_compare_tracks_requires_two_names():
    resp = call("compare_tracks", {"tracks": ["OnlyOne"]})
    assert resp["result"]["isError"] is True


def test_analyze_track_without_postmortem_installed(monkeypatch):
    monkeypatch.setattr(reaper_mcp, "_postmortem_cmdline", lambda: None)
    resp = call("analyze_track", {"track": "Kick"})
    assert resp["result"]["isError"] is True
    assert "pipx install" in result_text(resp)


def test_analyze_track_refuses_full_mix_payload(monkeypatch):
    payload = {
        "track": {"name": "Kick"},
        "capture": {"scope": "full_mix", "isolation_verified": False},
        "audio": {"rms_db": -12.0, "silence_fraction": 0.0},
    }

    def fake_run(cmd, capture_output, text, timeout, env):
        class P:
            returncode = 0
            stdout = json.dumps(payload)
            stderr = ""
        return P()

    monkeypatch.setattr(reaper_mcp, "_postmortem_cmdline", lambda: ["postmortem"])
    monkeypatch.setattr(reaper_mcp.subprocess, "run", fake_run)
    resp = call("analyze_track", {"track": "Kick"})
    assert resp["result"]["isError"] is True
    assert "full_mix" in result_text(resp)


def test_analyze_track_refuses_missing_capture_provenance(monkeypatch):
    payload = {"track": {"name": "Kick"}, "audio": {"rms_db": -12.0}}

    def fake_run(cmd, capture_output, text, timeout, env):
        class P:
            returncode = 0
            stdout = json.dumps(payload)
            stderr = ""
        return P()

    monkeypatch.setattr(reaper_mcp, "_postmortem_cmdline", lambda: ["postmortem"])
    monkeypatch.setattr(reaper_mcp.subprocess, "run", fake_run)
    resp = call("analyze_track", {"track": "Kick"})
    assert resp["result"]["isError"] is True
    assert "Kick: unknown" in result_text(resp)


def test_compare_tracks_refuses_any_unverified_capture(monkeypatch):
    payload = {
        "tracks": [
            {"name": "Kick", "capture": {"scope": "isolated_track", "isolation_verified": True}},
            {"name": "Bass", "capture": {"scope": "master_output", "isolation_verified": False}},
        ]
    }

    def fake_run(cmd, capture_output, text, timeout, env):
        class P:
            returncode = 0
            stdout = json.dumps(payload)
            stderr = ""
        return P()

    monkeypatch.setattr(reaper_mcp, "_postmortem_cmdline", lambda: ["postmortem"])
    monkeypatch.setattr(reaper_mcp.subprocess, "run", fake_run)
    resp = call("compare_tracks", {"tracks": ["Kick", "Bass"]})
    assert resp["result"]["isError"] is True
    assert "Bass: master_output" in result_text(resp)


def test_analyze_track_wraps_payload_and_records_panel_handoff(
    root, monkeypatch, tmp_path
):
    payload = {"track": {"name": "Kick"},
               "capture": {"scope": "isolated_track", "isolation_verified": True},
               "audio": {"rms_db": -12.0, "silence_fraction": 0.0}}

    def fake_run(cmd, capture_output, text, timeout, env):
        assert "--payload-only" in cmd
        assert env["REAPER_DAEMON_ROOT"] == root

        class P:
            returncode = 0
            stdout = json.dumps(payload)
            stderr = ""
        return P()

    monkeypatch.setattr(reaper_mcp, "_postmortem_cmdline", lambda: ["postmortem"])
    monkeypatch.setattr(reaper_mcp.subprocess, "run", fake_run)
    monkeypatch.setenv("POSTMORTEM_DATA_DIR", str(tmp_path))
    resp = call("analyze_track", {"track": "Kick"})
    text = result_text(resp)
    assert "isError" not in resp["result"]
    assert "ONE track" in text            # hedge contract preamble
    assert '"rms_db": -12.0' in text      # payload passed through
    assert "WARNING" not in text
    handoff = json.loads((tmp_path / "mcp-handoff.json").read_text(encoding="utf-8"))
    assert handoff["tracks"] == ["Kick"]
    assert handoff["seconds"] == 10
    assert handoff["delivered_at"]
    diagnosis = "The kick has a measured low-mid buildup around 200 Hz. Try a small cut."
    completed = call("complete_postmortem_onboarding", {
        "track": "Kick", "diagnosis": diagnosis,
    })
    assert "isError" not in completed["result"]
    jobs = list((tmp_path / "jobs" / "inbox").glob("*.json"))
    assert len(jobs) == 1
    rendered = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert rendered["type"] == "record_mcp_handoff"
    assert rendered["payload"]["tracks"] == ["Kick"]
    assert rendered["payload"]["diagnosis_summary"] == diagnosis


def test_mcp_onboarding_completion_requires_a_fresh_matching_handoff(
    root, monkeypatch
):
    resp = call("complete_postmortem_onboarding", {
        "track": "Kick",
        "diagnosis": "The kick diagnosis is long enough but has no measured handoff.",
    })
    assert resp["result"]["isError"] is True
    assert "Run analyze_track first" in result_text(resp)


def test_analyze_track_flags_mostly_silent_capture(root, monkeypatch):
    payload = {"track": {"name": "Kick"},
               "capture": {"scope": "isolated_track", "isolation_verified": True},
               "audio": {"rms_db": -71.0, "silence_fraction": 0.97}}

    def fake_run(cmd, capture_output, text, timeout, env):
        class P:
            returncode = 0
            stdout = json.dumps(payload)
            stderr = ""
        return P()

    monkeypatch.setattr(reaper_mcp, "_postmortem_cmdline", lambda: ["postmortem"])
    monkeypatch.setattr(reaper_mcp.subprocess, "run", fake_run)
    text = result_text(call("analyze_track", {"track": "Kick"}))
    assert "WARNING" in text and "mostly silence" in text


def test_analyze_track_surfaces_postmortem_failure(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout, env):
        class P:
            returncode = 2
            stdout = ""
            stderr = "No track matches 'Kik'.\nDid you mean: \"Kick\"?"
        return P()

    monkeypatch.setattr(reaper_mcp, "_postmortem_cmdline", lambda: ["postmortem"])
    monkeypatch.setattr(reaper_mcp.subprocess, "run", fake_run)
    resp = call("analyze_track", {"track": "Kik"})
    assert resp["result"]["isError"] is True
    assert "Did you mean" in result_text(resp)


# --- stdio subprocess smoke test ---------------------------------------------

def test_stdio_framing_end_to_end(root):
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "reaper_mcp.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
        env=dict(os.environ, REAPER_DAEMON_ROOT=root),
    )
    try:
        messages = [
            rpc("initialize", {"protocolVersion": "2025-06-18"}, mid=1),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            rpc("tools/list", mid=2),
            [rpc("ping", mid=3), rpc("ping", mid=4)],  # JSON-RPC batch
            "this is not json",
        ]
        stdin_data = "\n".join(
            m if isinstance(m, str) else json.dumps(m) for m in messages) + "\n"
        out, err = proc.communicate(stdin_data, timeout=30)
    finally:
        proc.kill()

    frames = [json.loads(line) for line in out.splitlines() if line.strip()]
    lines = []
    for frame in frames:
        lines.extend(frame if isinstance(frame, list) else [frame])
    by_id = {m.get("id"): m for m in lines}
    assert by_id[3]["result"] == {} and by_id[4]["result"] == {}  # batch answered
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    assert any(t["name"] == "get_context"
               for t in by_id[2]["result"]["tools"])
    parse_errors = [m for m in lines if m.get("error", {}).get("code") == -32700]
    assert parse_errors, "malformed line must produce a -32700 error"
    # stdout carried only valid JSON-RPC (the log line went to stderr).
    assert "[reaper-mcp]" not in out
    assert "[reaper-mcp]" in err
