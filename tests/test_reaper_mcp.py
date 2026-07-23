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
from bridge_fakes import fake_bridge, fake_bridge_script, write_test_wav  # noqa: E402


@pytest.fixture
def root(root, monkeypatch):
    """Overrides conftest's root: same folders, plus the MCP module pointed
    at it (BRIDGE_ROOT is resolved at import time from the environment)."""
    monkeypatch.setattr(reaper_mcp, "BRIDGE_ROOT", root)
    monkeypatch.setenv("POSTMORTEM_DATA_DIR", os.path.join(root, "postmortem-data"))
    monkeypatch.setattr(reaper_mcp, "_POSTMORTEM_MCP_RECEIPT", None)
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
                     "verify_change", "tune_param",
                     "analyze_track", "compare_tracks",
                     "complete_postmortem_onboarding", "raw_command"):
        assert expected in tools
        assert tools[expected]["inputSchema"]["type"] == "object"
        assert tools[expected]["description"]
    assert tools["verify_change"]["inputSchema"]["required"] == [
        "track", "command_type", "payload"]
    assert tools["tune_param"]["inputSchema"]["required"] == ["track", "target"]


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
    handoff = reaper_mcp._POSTMORTEM_MCP_RECEIPT
    assert len(handoff["receipt_id"]) == 64
    assert handoff["tracks"] == ["Kick"]
    assert handoff["seconds"] == 10
    assert handoff["delivered_at"].tzinfo is not None
    diagnosis = "The kick has a measured low-mid buildup around 200 Hz. Try a small cut."
    completed = call("complete_postmortem_onboarding", {
        "track": "Kick", "diagnosis": diagnosis,
    })
    assert "isError" not in completed["result"]
    jobs = [json.loads(path.read_text(encoding="utf-8"))
            for path in (tmp_path / "jobs" / "inbox").glob("*.json")]
    assert len(jobs) == 2
    measured = next(job for job in jobs if job["type"] == "record_mcp_measurement")
    rendered = next(job for job in jobs if job["type"] == "record_mcp_handoff")
    assert measured["payload"]["receipt_id"] == handoff["receipt_id"]
    assert measured["payload"]["seconds"] == 10
    assert rendered["type"] == "record_mcp_handoff"
    assert rendered["payload"]["tracks"] == ["Kick"]
    assert rendered["payload"]["receipt_id"] == handoff["receipt_id"]
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


# --- verify_change / tune_param (closed-loop tools) --------------------------

PRE_OK = {"ok": True, "data": {
    "capture_allowed": True, "blockers": [], "warnings": [],
    "risk_gate": {"allow_risk_level_3": True, "requires_restart_to_change": True},
    "sws_installed": True, "render_autoclose": True}}
PRE_GATED = {"ok": True, "data": {
    "capture_allowed": False,
    "blockers": [{"code": "capture_gated", "message": "risk gate off"}],
    "warnings": [],
    "risk_gate": {"allow_risk_level_3": False, "requires_restart_to_change": True},
    "sws_installed": True, "render_autoclose": True}}
CTX = {"ok": True, "data": {"cursor": {"seconds": 2.0, "bar": 1},
                            "time_selection": {"active": False, "start": 0,
                                               "end": 0}}}


def _auto_bridge(root, lufs_fn, n0=0.5, count=40, record=None):
    """A stateful scripted bridge: set_fx_param updates the 'knob', captures
    report LUFS as a function of the current knob value. Mimics the real
    feedback loop the tuner drives."""
    state = {"n": n0}

    def respond(cmd):
        t, p = cmd["type"], cmd.get("payload", {})
        if t == "get_capture_preflight":
            return PRE_OK
        if t == "get_context":
            return CTX
        if t == "set_fx_param":
            state["n"] = p["normalized_value"]
            return {"ok": True, "data": {"applied": True}}
        if t == "get_fx_parameters":
            return {"ok": True, "data": {
                "track": {"index": 2, "name": "Bass", "guid": "{B}"},
                "fx": {"index": 0, "api_index": 0, "scope": "track",
                       "name": "VST: TestGain", "guid": "{F}",
                       "parameter_count": 4},
                "parameters": [
                    {"index": 3, "name": "Gain",
                     "normalized_value": state["n"],
                     "formatted_value": f"norm {state['n']:.4f}"},
                    {"index": 0, "name": "Bypass",
                     "normalized_value": 0.0, "formatted_value": "Off"},
                ],
                "paging": {"has_more": False}}}
        if t == "capture_track_audio":
            out = p["output_file"]
            write_test_wav(out)
            lufs = lufs_fn(state["n"])
            return {"ok": True, "data": {
                "track": {"index": 2, "name": "Bass", "guid": "{B}"},
                "file_path": out, "file_size_bytes": os.path.getsize(out),
                "render_loudness_lufs": lufs,
                "render_stats_raw": f"LUFSI:{lufs}",
                "capture_scope": "isolated_track", "isolation_verified": True,
                "start_seconds": p.get("start_seconds"),
                "duration_seconds": p.get("duration_seconds")}}
        return {"ok": False, "error": {"code": "UNEXPECTED_COMMAND",
                                       "details": t}}

    return fake_bridge_script(root, [respond] * count, record=record)


def test_verify_change_happy_path(root):
    # The mutation moves the auto bridge's knob from 0.5 to 0.2; captures
    # before/after report the LUFS for the current knob value.
    record = []
    _auto_bridge(root, lambda n: -14.1 if n == 0.5 else -14.9, count=6,
                 record=record)
    resp = call("verify_change", {
        "track": "Bass", "command_type": "set_fx_param",
        "payload": {"target_track_name": "Bass", "param_index": 3,
                    "normalized_value": 0.2}})
    body = json.loads(result_text(resp))
    assert body["status"] == "VERIFIED"
    assert body["exit_code"] == 0
    assert body["deltas"]["lufs_i_delta"] == -0.8
    assert resp["result"].get("isError") is not True
    # post-capture was GUID-pinned to the pre-resolved track
    post_capture = [c for c in record if c["type"] == "capture_track_audio"][-1]
    assert post_capture["payload"]["target_track_guid"] == "{B}"


def test_verify_change_refused_is_tool_error(root):
    fake_bridge_script(root, [PRE_GATED])
    resp = call("verify_change", {
        "track": "Bass", "command_type": "set_fx_param",
        "payload": {"param_index": 3, "normalized_value": 0.2}})
    assert resp["result"]["isError"] is True
    body = json.loads(result_text(resp))
    assert body["status"] == "REFUSED"


@pytest.mark.parametrize("bad_args", [
    {},                                     # nothing
    {"track": "Bass"},                      # no command_type/payload
    {"track": "Bass", "command_type": "set_fx_param", "payload": "oops"},
])
def test_verify_change_schema_validation(root, bad_args):
    resp = call("verify_change", bad_args)
    assert resp["result"]["isError"] is True


@pytest.mark.parametrize("bad_args", [
    {},                                                       # nothing
    {"track": "Bass"},                                        # no target
    {"track": "Bass", "target": {"metric": "lufs_i", "delta": -3}},  # no fx
    {"track": "Bass", "fx_name_contains": "Gain",
     "target": {"metric": "lufs_i", "delta": -3}},            # no param
])
def test_tune_param_schema_validation(root, bad_args):
    resp = call("tune_param", bad_args)
    assert resp["result"]["isError"] is True


def test_tune_param_bad_target_rejected(root):
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "Gain", "param_index": 3,
        "target": {"metric": "sparkle", "delta": -3}})
    assert resp["result"]["isError"] is True
    assert "BAD_TARGET" in result_text(resp)


def test_tune_param_converges(root):
    # metric = -20 + 12n: baseline at n0=0.5 is -14; delta -3 -> target -17,
    # which sits exactly at n=0.25. Boundary probe (n=0) then one bisection.
    record = []
    _auto_bridge(root, lambda n: round(-20 + 12 * n, 4), count=12,
                 record=record)
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": -3.0, "tolerance": 0.5}})
    body = json.loads(result_text(resp))
    assert body["status"] == "CONVERGED"
    assert body["iterations_used"] == 2
    assert body["baseline"] == -14.0
    assert body["target_value"] == -17.0
    assert body["final"]["normalized"] == 0.25
    assert body["final"]["metric"] == -17.0
    assert body["final"]["formatted_value"] == "norm 0.2500"
    assert resp["result"].get("isError") is not True
    # every set was pinned: GUID + fx index/scope + param index
    sets = [c["payload"] for c in record if c["type"] == "set_fx_param"]
    assert sets and all(
        s["target_track_guid"] == "{B}" and s["fx_index"] == 0
        and s["fx_scope"] == "track" and s["param_index"] == 3 for s in sets)


def test_tune_param_iteration_cap_and_unconverged_report(root):
    # metric = -20 + 12n^2: baseline -17 at n0=0.5; delta -2 -> target -19.
    # With a 0.02 tolerance, five iterations get close but not inside.
    _auto_bridge(root, lambda n: round(-20 + 12 * n * n, 6), count=24)
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": -2.0, "tolerance": 0.02}})
    body = json.loads(result_text(resp))
    assert body["status"] == "UNCONVERGED"
    assert body["iterations_used"] == 5
    assert "not within tolerance" in body["note"]
    # honest report: best-observed value left applied, error stated
    assert abs(body["final"]["error_from_target"]) <= 0.1
    assert resp["result"].get("isError") is not True


def test_tune_param_non_monotone_aborts_and_restores(root):
    # metric peaks at n0=0.5 and falls toward both boundaries: both probe
    # directions contradict a monotone map -> abort, restore initial value.
    def peaked(n):
        return round(-20 + 20 * (n if n <= 0.5 else 1.0 - n), 4)

    record = []
    _auto_bridge(root, peaked, count=14, record=record)
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": 3.0, "tolerance": 0.5}})
    body = json.loads(result_text(resp))
    assert body["status"] == "NON_MONOTONE"
    assert "refusing to thrash" in body["note"].lower()
    assert body["final"]["normalized"] == 0.5  # initial value restored
    sets = [c["payload"]["normalized_value"] for c in record
            if c["type"] == "set_fx_param"]
    assert sets[-1] == 0.5
    assert resp["result"].get("isError") is not True


def test_tune_param_refused_on_gated_capture(root):
    fake_bridge_script(root, [PRE_GATED])
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": -3.0}})
    assert resp["result"]["isError"] is True
    body = json.loads(result_text(resp))
    assert body["status"] == "REFUSED"
    assert "nothing was changed" in body["note"].lower()


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
