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


def _auto_bridge(root, lufs_fn, n0=0.5, count=40, record=None,
                 scope_fn=None, fx_guid_fn=None, fail_set_calls=()):
    """A stateful scripted bridge: set_fx_param updates the 'knob', captures
    report LUFS as a function of the current knob value. Mimics the real
    feedback loop the tuner drives, and validates sets like the bridge does
    (range check, GUID check when supplied). scope_fn(capture_index) ->
    (scope, verified) and fx_guid_fn(scan_index) -> guid let tests simulate
    scope drift and FX-chain edits mid-run."""
    state = {"n": n0, "captures": 0, "scans": 0, "sets": 0}

    def respond(cmd):
        t, p = cmd["type"], cmd.get("payload", {})
        if t == "get_capture_preflight":
            return PRE_OK
        if t == "get_context":
            return CTX
        if t == "set_fx_param":
            state["sets"] += 1
            if state["sets"] in fail_set_calls:
                return {"ok": False, "error": {"code": "SET_REJECTED",
                                               "details": "scripted rejection"}}
            n = p.get("normalized_value")
            if not isinstance(n, (int, float)) or not 0.0 <= n <= 1.0:
                return {"ok": False, "error": {"code": "BAD_PAYLOAD",
                                               "details": f"normalized {n!r}"}}
            if "target_track_guid" in p and p["target_track_guid"] != "{B}":
                return {"ok": False, "error": {"code": "NO_TARGET_TRACK",
                                               "details": "unknown guid"}}
            state["n"] = n
            return {"ok": True, "data": {"applied": True}}
        if t == "get_fx_parameters":
            idx = state["scans"]
            state["scans"] += 1
            guid = fx_guid_fn(idx) if fx_guid_fn else "{F}"
            return {"ok": True, "data": {
                "track": {"index": 2, "name": "Bass", "guid": "{B}"},
                "fx": {"index": 0, "api_index": 0, "scope": "track",
                       "name": "VST: TestGain", "guid": guid,
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
            idx = state["captures"]
            state["captures"] += 1
            scope, verified = (scope_fn(idx) if scope_fn
                               else ("isolated_track", True))
            out = p["output_file"]
            write_test_wav(out)
            lufs = lufs_fn(state["n"])
            return {"ok": True, "data": {
                "track": {"index": 2, "name": "Bass", "guid": "{B}"},
                "file_path": out, "file_size_bytes": os.path.getsize(out),
                "render_loudness_lufs": lufs,
                "render_stats_raw": f"LUFSI:{lufs}",
                "capture_scope": scope, "isolation_verified": verified,
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
    _auto_bridge(root, lambda n: round(-20 + 12 * n, 4), record=record)
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
    _auto_bridge(root, lambda n: round(-20 + 12 * n * n, 6))
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


def test_tune_param_peak_at_baseline_is_unreachable_restores_best(root):
    # metric peaks at n0=0.5 and falls toward both boundaries. Both probes
    # move AWAY from the target — that is direction detection, not proof of
    # non-monotonicity, and the honest verdict is UNREACHABLE (baseline is
    # the closest achievable). Best-observed (= initial) value re-applied.
    def peaked(n):
        return round(-20 + 20 * (n if n <= 0.5 else 1.0 - n), 4)

    record = []
    _auto_bridge(root, peaked, record=record)
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": 3.0, "tolerance": 0.5}})
    body = json.loads(result_text(resp))
    assert body["status"] == "UNREACHABLE"
    assert "away from the target" in body["note"]
    assert body["final"]["normalized"] == 0.5   # read back, not assumed
    assert body["final"]["read_back"] is True
    sets = [c["payload"]["normalized_value"] for c in record
            if c["type"] == "set_fx_param"]
    assert sets[-1] == 0.5
    assert resp["result"].get("isError") is not True


def test_tune_param_non_monotone_mid_bracket_aborts_and_restores(root):
    # Bracket [0.5 -> -14, 0.0 -> -20], goal -17; the midpoint reads -25,
    # OUTSIDE the bracket — the true NON_MONOTONE case. Initial restored.
    table = {0.5: -14.0, 0.0: -20.0, 0.25: -25.0}

    record = []
    _auto_bridge(root, lambda n: table[round(n, 4)], record=record)
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": -3.0, "tolerance": 0.5}})
    body = json.loads(result_text(resp))
    assert body["status"] == "NON_MONOTONE"
    assert "refusing to thrash" in body["note"].lower()
    assert body["final"]["normalized"] == 0.5   # initial value restored
    assert resp["result"].get("isError") is not True


def test_tune_param_baseline_already_within_tolerance(root):
    # No sets, no iteration renders: report CONVERGED honestly at 0 cost.
    record = []
    _auto_bridge(root, lambda n: -14.0, record=record)
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": -0.1, "tolerance": 0.5}})
    body = json.loads(result_text(resp))
    assert body["status"] == "CONVERGED"
    assert body["iterations_used"] == 0
    assert "nothing was changed" in body["note"]
    assert all(c["type"] != "set_fx_param" for c in record)


def test_tune_param_boundary_start_flips_direction(root):
    # n0 = 1.0 with a DECREASING metric (-10n): the naive up-boundary equals
    # the start, so the probe must go the other way instead of reporting a
    # reachable target as UNREACHABLE (Codex gate MAJOR, 2026-07-23).
    _auto_bridge(root, lambda n: round(-10.0 * n, 4), n0=1.0)
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": 5.0, "tolerance": 0.5}})
    body = json.loads(result_text(resp))
    assert body["status"] == "CONVERGED"
    assert body["final"]["normalized"] == 0.5
    assert body["iterations_used"] == 2


def test_tune_param_flat_metric_is_unreachable_not_non_monotone(root):
    # A constant metric is monotone but useless — the honest label is
    # UNREACHABLE (Codex gate MINOR, 2026-07-23).
    _auto_bridge(root, lambda n: -14.0)
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": 3.0, "tolerance": 0.5}})
    body = json.loads(result_text(resp))
    assert body["status"] == "UNREACHABLE"
    assert "does not move" in body["note"]


def test_tune_param_aborts_on_fx_chain_edit(root):
    # After resolution, the FX at the pinned index changes GUID (a chain
    # edit): the tuner must abort BEFORE setting a different plugin.
    record = []
    _auto_bridge(root, lambda n: round(-20 + 12 * n, 4),
                 fx_guid_fn=lambda i: "{F}" if i == 0 else "{G}",
                 record=record)
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": -3.0, "tolerance": 0.5}})
    body = json.loads(result_text(resp))
    assert body["status"] == "IDENTITY_CHANGED"
    assert body["iterations_used"] == 0
    assert all(c["type"] != "set_fx_param" for c in record)
    assert body["final"]["read_back"] is False  # different FX at read-back too
    assert resp["result"].get("isError") is not True


def test_tune_param_aborts_on_scope_change(root):
    # Baseline is isolated; iteration captures fall back to full_mix (e.g.
    # the project changed): per-track evidence is gone, stop honestly.
    _auto_bridge(root, lambda n: round(-20 + 12 * n, 4),
                 scope_fn=lambda i: ("isolated_track", True) if i == 0
                 else ("full_mix", False))
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": -3.0, "tolerance": 0.5}})
    body = json.loads(result_text(resp))
    assert body["status"] == "SCOPE_CHANGED"
    assert "full_mix" in body["note"]
    assert body["final"]["read_back"] is True
    assert resp["result"].get("isError") is not True


def test_tune_param_refuses_full_mix_baseline(root):
    _auto_bridge(root, lambda n: -14.0,
                 scope_fn=lambda i: ("full_mix", False))
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": -3.0}})
    assert resp["result"]["isError"] is True
    body = json.loads(result_text(resp))
    assert body["status"] == "REFUSED"
    assert "full_mix" in body["note"]


def test_tune_param_set_failure_reads_back_final_state(root):
    _auto_bridge(root, lambda n: round(-20 + 12 * n, 4), fail_set_calls={1})
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": -3.0, "tolerance": 0.5}})
    body = json.loads(result_text(resp))
    assert body["status"] == "SET_FAILED"
    assert "SET_REJECTED" in body["note"]
    assert body["final"]["normalized"] == 0.5  # read back: knob never moved
    assert body["final"]["read_back"] is True
    # a set may already have applied in the general case -> never isError
    assert resp["result"].get("isError") is not True


def test_verify_change_bridge_rejection_is_not_a_tool_error(root):
    # A rejected handler can still leave a partial mid-edit change; marking
    # it isError invites a retry on top of it (Codex gate BLOCKER).
    def respond(cmd):
        t = cmd["type"]
        if t == "get_capture_preflight":
            return PRE_OK
        if t == "get_context":
            return CTX
        if t == "capture_track_audio":
            p = cmd["payload"]
            write_test_wav(p["output_file"])
            return {"ok": True, "data": {
                "track": {"index": 2, "name": "Bass", "guid": "{B}"},
                "file_path": p["output_file"], "render_loudness_lufs": -14.1,
                "render_stats_raw": "LUFSI:-14.1",
                "capture_scope": "isolated_track", "isolation_verified": True,
                "start_seconds": p.get("start_seconds"),
                "duration_seconds": p.get("duration_seconds")}}
        return {"ok": False, "error": {"code": "NO_FX_PARAM",
                                       "details": "no such parameter"}}
    fake_bridge_script(root, [respond] * 4)
    resp = call("verify_change", {
        "track": "Bass", "command_type": "set_fx_param",
        "payload": {"target_track_name": "Bass", "param_index": 99,
                    "normalized_value": 0.2}})
    body = json.loads(result_text(resp))
    # A post-send rejection is UNVERIFIED (the handler may have made a
    # partial mid-edit change), and it is a real outcome the model must
    # read and relay, never an isError the client would auto-retry.
    assert body["status"] == "UNVERIFIED"
    assert body["exit_code"] == 2
    assert body["mutation"]["rejection_code"] == "NO_FX_PARAM"
    assert resp["result"].get("isError") is not True


def test_tune_param_advertised_schema_expresses_target_shape():
    resp = reaper_mcp.handle_message(rpc("tools/list"))
    tool = next(t for t in resp["result"]["tools"] if t["name"] == "tune_param")
    target_schema = tool["inputSchema"]["properties"]["target"]
    assert target_schema["required"] == ["metric", "delta"]
    assert target_schema["properties"]["metric"]["enum"] == ["lufs_i", "band_db"]


def test_tune_param_refused_on_gated_capture(root):
    fake_bridge_script(root, [PRE_GATED])
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "TestGain", "param_index": 3,
        "target": {"metric": "lufs_i", "delta": -3.0}})
    assert resp["result"]["isError"] is True
    body = json.loads(result_text(resp))
    assert body["status"] == "REFUSED"
    assert "nothing was changed" in body["note"].lower()


@pytest.mark.parametrize("tool", ["verify_change", "tune_param"])
@pytest.mark.parametrize("flag", [True, False])
def test_mutating_tools_reject_dry_run_outright(root, tool, flag):
    # These tools have no dry_run; silently dropping it would hand a caller
    # who asked for a preview a REAL mutation. Even dry_run=false is intent-
    # bearing and must be refused before any bridge call.
    resp = call(tool, {"track": "Bass", "dry_run": flag})
    assert resp["result"]["isError"] is True
    text = result_text(resp)
    assert "dry_run" in text and "raw_command" in text
    assert os.listdir(os.path.join(root, "inbox")) == []


def test_tune_param_rejects_conflicting_fx_selectors(root):
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "ReaEQ", "fx_index": 0,
        "param_index": 3, "target": {"metric": "lufs_i", "delta": -3.0}})
    assert resp["result"]["isError"] is True
    assert "not both" in result_text(resp)
    assert os.listdir(os.path.join(root, "inbox")) == []


def test_tune_param_rejects_conflicting_param_selectors(root):
    resp = call("tune_param", {
        "track": "Bass", "fx_name_contains": "ReaEQ",
        "param_index": 3, "param_name_contains": "Gain",
        "target": {"metric": "lufs_i", "delta": -3.0}})
    assert resp["result"]["isError"] is True
    text = result_text(resp)
    assert "param_index" in text and "not both" in text
    assert os.listdir(os.path.join(root, "inbox")) == []


def test_dry_run_stale_reply_lock_is_structured_not_a_crash(root, monkeypatch):
    # The dry_run branch calls reaperd.send_command directly; StaleReplyError
    # must map to the same STALE_REPLY_LOCKED shape send_type produces, not
    # fall through to the dispatcher's generic "tool error".
    def raise_stale(*a, **k):
        raise reaper_mcp.reaperd.StaleReplyError("outbox reply locked")

    monkeypatch.setattr(reaper_mcp.reaperd, "send_command", raise_stale)
    resp = call("raw_command", {"type": "play", "dry_run": True})
    assert resp["result"]["isError"] is True
    body = json.loads(result_text(resp))
    assert body["error"]["code"] == "STALE_REPLY_LOCKED"


def test_verify_sender_default_timeout_is_the_shared_constant(root, monkeypatch):
    # CLI and MCP senders must not drift (10 s vs 15 s gave two verdicts for
    # one project state on a slow REAPER).
    seen = {}

    def fake(cmd_type, payload, **kw):
        seen.update(kw)
        return {"ok": True}

    monkeypatch.setattr(reaper_mcp.reaperd, "send_type", fake)
    reaper_mcp._verify_sender("get_context", {})
    assert (seen["timeout_ms"]
            == reaper_mcp.verifyloop.DEFAULT_SENDER_TIMEOUT_MS == 10000)


# --- drum workflow (profile_track / riff_grid / insert_groove) ---------------


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_child(monkeypatch, returncode=0, stdout="", stderr="", record=None,
               raises=None):
    """Stand in for the drum engine / drumgen module the tool shells out to.
    Records the argv it was handed so tests can pin the CLI contract."""
    def run(cmd, **kw):
        if record is not None:
            record.append(list(cmd))
        if raises is not None:
            raise raises
        return FakeProc(returncode, stdout, stderr)

    monkeypatch.setattr(reaper_mcp.subprocess, "run", run)
    return record


def drum_skill(root):
    """The skill layout the three tools require under the bridge root."""
    skill = os.path.join(root, "skills", "drum-apparatus")
    os.makedirs(skill, exist_ok=True)
    with open(os.path.join(skill, "groovegen.py"), "w", encoding="utf-8") as f:
        f.write("# fake engine; subprocess is monkeypatched in these tests\n")
    return skill


def saved_project(root, name="song.rpp"):
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("<REAPER_PROJECT 0.1\n>\n")
    return path


def live_heartbeat(root):
    with open(os.path.join(root, "bridge", "heartbeat.json"), "w",
              encoding="utf-8") as f:
        json.dump({"alive_at": "now", "bridge_version": 3,
                   "project_name": "Song"}, f)


def midi_arg(argv):
    return argv[argv.index("--out") + 1]


def test_drum_tools_listed_with_schemas():
    resp = reaper_mcp.handle_message(rpc("tools/list"))
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    for name in ("profile_track", "riff_grid", "insert_groove"):
        assert tools[name]["inputSchema"]["type"] == "object"
        assert tools[name]["description"]
    assert tools["profile_track"]["inputSchema"]["required"] == ["project", "track"]
    assert tools["riff_grid"]["inputSchema"]["required"] == ["project", "track"]
    # insert_groove's DSL source is one-of, so it cannot be a required field;
    # the handler enforces the choice instead.
    assert "required" not in tools["insert_groove"]["inputSchema"]
    for name in ("dsl_text", "dsl_path", "dry_run"):
        assert name in tools["insert_groove"]["inputSchema"]["properties"]
    # The stale-project caveat has to be visible BEFORE the call, not only in
    # the payload: a model choosing the tool must know it reads the disk.
    for name in ("profile_track", "riff_grid"):
        assert "SAVED" in tools[name]["description"]


@pytest.mark.parametrize("tool", ["profile_track", "riff_grid"])
def test_saved_project_tools_require_project_and_track(root, tool):
    assert call(tool, {"track": "GTR"})["result"]["isError"] is True
    assert call(tool, {"project": "x.rpp"})["result"]["isError"] is True


@pytest.mark.parametrize("tool", ["profile_track", "riff_grid"])
def test_saved_project_tools_refuse_missing_rpp(root, tool):
    resp = call(tool, {"project": os.path.join(root, "nope.rpp"), "track": "GTR"})
    assert resp["result"]["isError"] is True
    body = json.loads(result_text(resp))
    assert body["error"]["code"] == "PROJECT_NOT_FOUND"
    assert "save the project" in body["error"]["details"].lower()


def test_profile_track_payload_carries_the_stale_saved_project_caveat(
        root, monkeypatch):
    drum_skill(root)
    project = saved_project(root)
    fake_child(monkeypatch, stdout="tempo 146  sr 44100  track GTR  bars 4",
               stderr="WARNING: 2 items on track; first item only")
    resp = call("profile_track", {"project": project, "track": "GTR"})
    body = json.loads(result_text(resp))
    assert body["ok"] is True
    assert body["source"] == "saved_project_file"
    assert body["reads_live_reaper_state"] is False
    assert "SAVED .rpp" in body["caveat"] and "stale" in body["caveat"]
    assert body["project"] == project and body["project_saved_at"]
    assert "tempo 146" in body["report"]
    # The CLI drops the child's stderr on success; the tool must not — that is
    # where drumgen says it analyzed only the first of several items.
    assert "2 items" in body["warnings"]


def test_riff_grid_payload_carries_the_caveat_and_defaults(root, monkeypatch):
    drum_skill(root)
    project = saved_project(root)
    seen = fake_child(monkeypatch, stdout="bar 1: x... x... x... x...",
                      record=[])
    resp = call("riff_grid", {"project": project, "track": "GTR"})
    body = json.loads(result_text(resp))
    assert body["ok"] is True and body["analysis"] == "riff"
    assert body["reads_live_reaper_state"] is False
    assert "SAVED .rpp" in body["caveat"]
    # reaperd.py riff's defaults: 4 bars from bar 0, positional.
    assert seen[0][1:] == ["-m", "drumgen.riff", project, "GTR", "4", "0"]


def test_profile_window_args_are_positional_in_drumgen_order(root, monkeypatch):
    drum_skill(root)
    project = saved_project(root)
    seen = fake_child(monkeypatch, stdout="ok", record=[])
    call("profile_track", {"project": project, "track": "GTR",
                           "start_bar": 32, "bars": 8, "max_seconds": 10.0})
    assert seen[0][1:] == ["-m", "drumgen.profile", project, "GTR",
                           "8", "32", "10.0"]


def test_profile_zero_max_seconds_reaches_drumgen_to_be_rejected(
        root, monkeypatch):
    # is-not-None, not truthiness: max_seconds 0 must be refused by drumgen,
    # never silently reinterpreted as "profile the whole item".
    drum_skill(root)
    project = saved_project(root)
    seen = fake_child(monkeypatch, stdout="ok", record=[])
    call("profile_track", {"project": project, "track": "GTR", "max_seconds": 0})
    assert seen[0][-3:] == ["0", "0", "0"]  # bars=whole item, start=0, cap=0


def test_profile_subprocess_failure_is_a_structured_error(root, monkeypatch):
    drum_skill(root)
    project = saved_project(root)
    fake_child(monkeypatch, returncode=1,
               stderr="error: max_seconds 0.5 is shorter than one bar")
    resp = call("profile_track", {"project": project, "track": "GTR",
                                  "max_seconds": 0.5})
    assert resp["result"]["isError"] is True
    body = json.loads(result_text(resp))
    assert body["ok"] is False
    assert body["error"]["code"] == "PROFILE_FAILED"
    assert "shorter than one bar" in body["error"]["details"]


def test_child_traceback_is_reduced_to_the_exception_line(root, monkeypatch):
    # drumgen.riff has no top-level error guard, so a bad track name crashes
    # it. The model gets the exception, not a page of Python frames.
    drum_skill(root)
    project = saved_project(root)
    fake_child(monkeypatch, returncode=1, stderr=(
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n'
        "    proj = parse_project(rpp, track)\n"
        "ValueError: track 'Nope' not found in song.rpp\n"))
    resp = call("riff_grid", {"project": project, "track": "Nope"})
    body = json.loads(result_text(resp))
    assert body["error"]["code"] == "RIFF_FAILED"
    assert body["error"]["details"] == "ValueError: track 'Nope' not found in song.rpp"
    assert "Traceback" not in result_text(resp)


def test_drum_tools_report_a_missing_skill_folder(root, monkeypatch):
    project = saved_project(root)  # no skills/drum-apparatus under this root
    resp = call("profile_track", {"project": project, "track": "GTR"})
    body = json.loads(result_text(resp))
    assert body["error"]["code"] == "DRUM_SKILL_MISSING"


def test_profile_timeout_is_structured_not_a_crash(root, monkeypatch):
    drum_skill(root)
    project = saved_project(root)
    fake_child(monkeypatch,
               raises=subprocess.TimeoutExpired(cmd="profile", timeout=600))
    resp = call("profile_track", {"project": project, "track": "GTR"})
    body = json.loads(result_text(resp))
    assert body["error"]["code"] == "PROFILE_TIMEOUT"


@pytest.mark.parametrize("args, needle", [
    ({"dsl_text": "kick x...", "dsl_path": "beat.dsl"}, "not both"),
    ({}, "dsl_text"),
    ({"dsl_text": "   "}, "empty"),
])
def test_insert_groove_refuses_ambiguous_or_missing_dsl(root, args, needle):
    resp = call("insert_groove", dict(args, track="Drums"))
    assert resp["result"]["isError"] is True
    assert needle in result_text(resp)
    assert os.listdir(os.path.join(root, "inbox")) == []


def test_insert_groove_refuses_a_missing_dsl_file(root):
    resp = call("insert_groove", {"dsl_path": os.path.join(root, "no.dsl"),
                                  "track": "Drums"})
    body = json.loads(result_text(resp))
    assert body["error"]["code"] == "DSL_NOT_FOUND"


def test_insert_groove_gates_on_the_heartbeat_before_generating(
        root, monkeypatch):
    drum_skill(root)
    seen = fake_child(monkeypatch, stdout="unused", record=[])
    monkeypatch.setattr(reaper_mcp.reaperd, "reaper_running", lambda: False)
    resp = call("insert_groove", {"dsl_text": "kick x...", "track": "Drums"})
    assert resp["result"]["isError"] is True
    body = json.loads(result_text(resp))
    assert body["error"]["code"] == "REAPER_DOWN"
    assert seen == []  # no MIDI generated for a project nothing can receive
    assert os.listdir(os.path.join(root, "inbox")) == []


def test_insert_groove_renders_and_inserts(root, monkeypatch):
    drum_skill(root)
    live_heartbeat(root)
    seen = fake_child(monkeypatch, stdout="groovekit: 32 notes | 2 bars | "
                                          "map=GM Standard", record=[])
    cmds = []
    fake_bridge(root, {"ok": True, "type": "insert_midi_file",
                       "data": {"track": {"name": "Drums"}}}, record=cmds)
    resp = call("insert_groove", {"dsl_text": "kick x...x...x...x...",
                                  "track": "Drums", "seed": 7, "map": "MyKit"})
    body = json.loads(result_text(resp))
    assert body["ok"] is True
    assert "32 notes" in body["generated"]
    assert body["data"]["track"]["name"] == "Drums"
    payload = cmds[0]["payload"]
    assert payload["target_track_name"] == "Drums"
    assert payload["position"] == {"type": "cursor"}
    assert payload["midi_path"].endswith(".mid")
    argv = seen[0]
    # --spec=<text>, one argv entry: a DSL line starting with '-' must not be
    # read as an option by the engine's argparse.
    assert any(a.startswith("--spec=kick x") for a in argv)
    assert argv[argv.index("--seed") + 1] == "7"
    assert argv[argv.index("--map") + 1] == "MyKit"
    assert not os.path.exists(midi_arg(argv))  # temp MIDI cleaned on success


def test_insert_groove_honors_dry_run(root, monkeypatch):
    drum_skill(root)
    live_heartbeat(root)
    fake_child(monkeypatch, stdout="groovekit: 8 notes | 1 bars")
    cmds = []
    fake_bridge(root, {"ok": True, "type": "insert_midi_file", "dry_run": True,
                       "message": "would insert"}, record=cmds)
    resp = call("insert_groove", {"dsl_text": "kick x...", "track": "Drums",
                                  "dry_run": True})
    body = json.loads(result_text(resp))
    assert body["ok"] is True and body["dry_run"] is True
    assert cmds[0]["dry_run"] is True
    assert cmds[0]["created_by"] == "mcp"


def test_insert_groove_position_seconds_and_selected_track_fallback(
        root, monkeypatch):
    drum_skill(root)
    live_heartbeat(root)
    fake_child(monkeypatch, stdout="groovekit: 8 notes")
    cmds = []
    fake_bridge(root, {"ok": True, "type": "insert_midi_file"}, record=cmds)
    call("insert_groove", {"dsl_text": "kick x...", "position": 12.5})
    payload = cmds[0]["payload"]
    assert payload["position"] == {"type": "time", "seconds": 12.5}
    assert payload["use_selected_track"] is True


def test_insert_groove_surfaces_the_engine_message_and_unlinks_the_midi(
        root, monkeypatch):
    drum_skill(root)
    live_heartbeat(root)
    seen = fake_child(monkeypatch, returncode=2, record=[],
                      stderr="error: unknown role 'kik' on line 3")
    resp = call("insert_groove", {"dsl_text": "kik x...", "track": "Drums"})
    assert resp["result"]["isError"] is True
    body = json.loads(result_text(resp))
    assert body["error"]["code"] == "DSL_ERROR"
    # Verbatim: this message is what the model needs to repair its DSL.
    assert body["error"]["details"] == "error: unknown role 'kik' on line 3"
    assert not os.path.exists(midi_arg(seen[0]))
    assert os.listdir(os.path.join(root, "inbox")) == []


def test_insert_groove_unlinks_the_midi_when_the_bridge_rejects(
        root, monkeypatch):
    drum_skill(root)
    live_heartbeat(root)
    seen = fake_child(monkeypatch, stdout="groovekit: 8 notes", record=[])
    fake_bridge(root, {"ok": False, "type": "insert_midi_file",
                       "error": {"code": "NO_TARGET_TRACK", "details": "gone"}})
    resp = call("insert_groove", {"dsl_text": "kick x...", "track": "Ghost"})
    assert resp["result"]["isError"] is True
    body = json.loads(result_text(resp))
    assert body["error"]["code"] == "NO_TARGET_TRACK"
    assert "8 notes" in body["generated"]  # what was generated, honestly
    assert not os.path.exists(midi_arg(seen[0]))


def test_insert_groove_unlinks_the_midi_when_the_engine_times_out(
        root, monkeypatch):
    drum_skill(root)
    live_heartbeat(root)
    seen = []

    def run(cmd, **kw):
        seen.append(list(cmd))
        raise subprocess.TimeoutExpired(cmd="groovegen", timeout=120)

    monkeypatch.setattr(reaper_mcp.subprocess, "run", run)
    resp = call("insert_groove", {"dsl_text": "kick x...", "track": "Drums"})
    body = json.loads(result_text(resp))
    assert body["error"]["code"] == "GROOVE_TIMEOUT"
    assert not os.path.exists(midi_arg(seen[0]))


def test_insert_groove_falls_back_to_drum_config_track_and_map(
        root, monkeypatch):
    drum_skill(root)
    live_heartbeat(root)
    with open(os.path.join(root, "drum-config.json"), "w", encoding="utf-8") as f:
        json.dump({"track": "Kit", "map": "RS Monarch"}, f)
    seen = fake_child(monkeypatch, stdout="groovekit: 8 notes", record=[])
    cmds = []
    fake_bridge(root, {"ok": True, "type": "insert_midi_file"}, record=cmds)
    call("insert_groove", {"dsl_text": "kick x..."})
    assert cmds[0]["payload"]["target_track_name"] == "Kit"
    assert seen[0][seen[0].index("--map") + 1] == "RS Monarch"


# --- console freeze policy ----------------------------------------------------

def clamp(seconds, default, console_mode=True, maximum=8):
    return reaper_mcp.clamp_capture_seconds(seconds, default,
                                            console_mode=console_mode,
                                            maximum=maximum)


def test_outside_the_console_nothing_is_clamped():
    # From a terminal a 30 s render costs an alt-tab. The policy exists only
    # because the panel lives inside the frozen window.
    assert clamp(30, 30, console_mode=False) == (30, None)
    assert clamp(None, 30, console_mode=False) == (None, None)


def test_a_console_capture_is_clamped_and_says_so():
    seconds, note = clamp(30, 30)
    assert seconds == 8
    # Not decoration. Without it the model reports on 8 seconds while claiming
    # 30, which is the honesty failure AGENTS.md exists to prevent.
    assert "8s" in note and "30s" in note
    assert "not 30s" in note


def test_an_unrequested_length_is_clamped_from_the_tool_default():
    # capture_track_audio defaults to 30, so "just measure it" is a 30 second
    # freeze unless the default is clamped too.
    assert clamp(None, 30)[0] == 8
    assert clamp(None, 30)[1] is not None


def test_a_capture_already_short_enough_is_left_alone():
    assert clamp(5, 30) == (5, None)
    assert clamp(8, 30) == (8, None)


def test_a_junk_length_is_left_for_the_bridge_to_refuse():
    # Sanitizing it here would swallow the bridge's own error message.
    assert clamp("soon", 30) == ("soon", None)


def test_the_clamp_note_travels_inside_the_json_not_in_front_of_it(root):
    # console_sidecar's tool_verdict decides a result is structured by its
    # FIRST CHARACTER, so a prose preamble would turn every measurement back
    # into an opaque string in the panel.
    fake_bridge(root, {"ok": True, "data": {"rms_db": -12.0}})
    resp = reaper_mcp._reply_result({"ok": True, "data": {"rms_db": -12.0}},
                                    note="shortened")
    text = resp["content"][0]["text"]
    assert text.lstrip().startswith("{")
    assert json.loads(text)["console_note"] == "shortened"


def test_capture_track_audio_clamps_in_console_mode(root, monkeypatch):
    cmds = []
    fake_bridge(root, {"ok": True, "type": "capture_track_audio"}, record=cmds)
    monkeypatch.setattr(reaper_mcp, "CONSOLE_MODE", True)
    monkeypatch.setattr(reaper_mcp, "CONSOLE_MAX_CAPTURE_SECONDS", 8.0)
    resp = call("capture_track_audio", {"track": "Geets", "duration_seconds": 30})
    assert cmds[0]["payload"]["duration_seconds"] == 8
    assert "Capture shortened" in json.loads(result_text(resp))["console_note"]


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
