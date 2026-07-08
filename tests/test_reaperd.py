"""Honesty-cluster tests for reaperd.py (2026-07-02 review, Phase 1).

Everything here runs without a live REAPER: a tiny fake bridge thread answers
where a reply is needed, and send_type is monkeypatched for the eq/setparam
failure paths. Run: python -m pytest tests -q (from repo root).
"""

import argparse
import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reaperd  # noqa: E402
from bridge_fakes import fake_bridge  # noqa: E402  (root fixture: conftest.py)


# --- fix 2: send_command hygiene ------------------------------------------

def test_auto_id_has_real_entropy(root):
    cid, _ = reaperd.send_command({"type": "ping", "payload": {}}, bridge_root=root)
    assert len(cid.rsplit("-", 1)[-1]) == 16  # token_hex(8)


def test_stale_reply_is_not_answered_as_new_result(root):
    # Old bug: an unread reply with the same id was returned instantly as the
    # NEW command's result. It must be cleared before send, so with no bridge
    # running the call times out instead of "succeeding".
    stale = os.path.join(root, "outbox", "fixed-1.json")
    with open(stale, "w", encoding="utf-8") as f:
        f.write(json.dumps({"id": "fixed-1", "ok": True, "data": {"stale": True}}))
    with pytest.raises(TimeoutError):
        reaperd.send_command({"id": "fixed-1", "type": "ping", "payload": {}},
                             wait=True, timeout_ms=300, bridge_root=root)
    assert not os.path.exists(stale)


def test_fresh_reply_still_returned_and_consumed(root):
    fake_bridge(root, {"ok": True, "type": "ping", "data": {}})
    cid, reply = reaperd.send_command({"type": "ping", "payload": {}},
                                      wait=True, timeout_ms=5000, bridge_root=root)
    assert json.loads(reply)["ok"] is True
    assert not os.path.exists(os.path.join(root, "outbox", cid + ".json"))


# --- fix 1 (CLI side): timeout withdraws the queued command ----------------

def test_timeout_withdraws_inbox_file(root):
    with pytest.raises(TimeoutError):
        reaperd.send_command({"id": "will-timeout", "type": "ping", "payload": {}},
                             wait=True, timeout_ms=200, bridge_root=root)
    assert not os.path.exists(os.path.join(root, "inbox", "will-timeout.json"))


# --- fix 5: send --wait exit code tracks the reply -------------------------

def _send_args(root, tmp_path, wait=True):
    cmd_file = tmp_path / "cmd.json"
    cmd_file.write_text(json.dumps({"type": "ping", "payload": {}}))
    return argparse.Namespace(file=str(cmd_file), wait=wait, timeout=5000,
                              bridge_root=root)


def test_send_wait_exits_1_on_ok_false(root, tmp_path):
    fake_bridge(root, {"ok": False, "error": {"code": "NO_SUCH_TRACK"}})
    assert reaperd.cmd_send(_send_args(root, tmp_path)) == 1


def test_send_wait_exits_0_on_ok_true(root, tmp_path):
    fake_bridge(root, {"ok": True, "data": {}})
    assert reaperd.cmd_send(_send_args(root, tmp_path)) == 0


# --- fix 4: setparam honest verdicts ---------------------------------------

@pytest.mark.parametrize("target,after,code", [
    ("80 Hz", "80.0 Hz", 0),        # on target
    ("80 Hz", "86 Hz", 0),          # within 10%: CLOSE, still ok
    ("80 Hz", "8000 Hz", 1),        # 100x off: MISSED, no more exit 0
    ("100", "111", 1),              # just past 10%
    ("-3 dB", "-3.1 dB", 0),        # signed, abs floor
    ("Bell", "Bell", 0),            # enum target: text match (old code: TypeError)
    ("Bell", "Low Shelf", 1),       # enum target mismatch
    ("80 Hz", None, 1),             # display unreadable: unverified is a failure
])
def test_judge_landed(target, after, code):
    assert reaperd._judge_landed("Param", target, after) == code


def _scripted_send_type(script):
    """send_type stand-in: pops one canned reply per call, asserts the type."""
    calls = []

    def fake(cmd_type, payload, **kw):
        calls.append(cmd_type)
        want_type, reply = script.pop(0)
        assert cmd_type == want_type, f"call {len(calls)}: {cmd_type} != {want_type}"
        return reply
    return fake


def test_setparam_verify_scan_failure_exits_1(monkeypatch, root):
    params = {"ok": True, "data": {"parameters": [
        {"index": 0, "name": "Frequency", "formatted_value": "100 Hz"}]}}
    monkeypatch.setattr(reaperd, "send_type", _scripted_send_type([
        ("get_fx_parameters", params),
        ("set_fx_param", {"ok": True}),
        ("get_fx_parameters", {"ok": False, "error": {"code": "TIMEOUT"}}),
    ]))
    args = argparse.Namespace(track="master", fx="ReaEQ", param="#0",
                              value="80 Hz", bridge_root=root)
    assert reaperd.cmd_setparam(args) == 1


# --- fix 3: eq only claims LIVE with evidence -------------------------------

EQ_PARAMS = {"ok": True, "data": {"parameters": [
    {"index": 0, "name": "Band 1 Used", "formatted_value": "On"},
    {"index": 1, "name": "Band 1 Frequency", "formatted_value": "100 Hz"},
    {"index": 2, "name": "Band 1 Gain", "formatted_value": "0.0 dB"},
]}}


def _eq_args(root):
    return argparse.Namespace(track="Kick", fx="Pro-Q", band=1, freq="80",
                              gain="-3", q=None, bridge_root=root)


def test_eq_failed_set_exits_1_and_never_says_live(monkeypatch, root, capsys):
    monkeypatch.setattr(reaperd, "send_type", _scripted_send_type([
        ("get_fx_parameters", EQ_PARAMS),
        ("set_fx_param", {"ok": True}),                      # enable
        ("set_fx_param", {"ok": False, "error": {"code": "TIMEOUT"}}),  # freq
        ("set_fx_param", {"ok": True}),                      # gain
    ]))
    assert reaperd.cmd_eq(_eq_args(root)) == 1
    assert "LIVE" not in capsys.readouterr().out


def test_eq_verify_scan_failure_exits_1_and_never_says_live(monkeypatch, root, capsys):
    monkeypatch.setattr(reaperd, "send_type", _scripted_send_type([
        ("get_fx_parameters", EQ_PARAMS),
        ("set_fx_param", {"ok": True}),
        ("set_fx_param", {"ok": True}),
        ("set_fx_param", {"ok": True}),
        ("get_fx_parameters", {"ok": False, "error": {"code": "TIMEOUT"}}),
    ]))
    assert reaperd.cmd_eq(_eq_args(root)) == 1
    assert "LIVE" not in capsys.readouterr().out


def test_eq_happy_path_still_reports_live(monkeypatch, root, capsys):
    landed = {"ok": True, "data": {"parameters": [
        {"index": 0, "name": "Band 1 Used", "formatted_value": "On"},
        {"index": 1, "name": "Band 1 Frequency", "formatted_value": "80.0 Hz"},
        {"index": 2, "name": "Band 1 Gain", "formatted_value": "-3.0 dB"},
    ]}}
    monkeypatch.setattr(reaperd, "send_type", _scripted_send_type([
        ("get_fx_parameters", EQ_PARAMS),
        ("set_fx_param", {"ok": True}),
        ("set_fx_param", {"ok": True}),
        ("set_fx_param", {"ok": True}),
        ("get_fx_parameters", landed),
    ]))
    assert reaperd.cmd_eq(_eq_args(root)) == 0
    assert "BAND IS LIVE" in capsys.readouterr().out


# --- fix 9 (phase 2): paginated FX param scan with the field the bridge reads

def test_scan_fx_parameters_paginates_past_bridge_cap(monkeypatch):
    page1 = {"ok": True, "data": {"parameters": [{"index": i} for i in range(1000)],
                                  "has_more": True}}
    page2 = {"ok": True, "data": {"parameters": [{"index": 1000 + i} for i in range(200)],
                                  "has_more": False}}
    seen = []

    def fake(cmd_type, payload, **kw):
        assert cmd_type == "get_fx_parameters"
        seen.append(payload)
        return page1 if len(seen) == 1 else page2
    monkeypatch.setattr(reaperd, "send_type", fake)
    params, err = reaperd.scan_fx_parameters({"target_track_name": "master"}, None)
    assert err is None
    assert len(params) == 1200                    # Kontakt-scale plugin fully scanned
    assert seen[0]["limit"] == 1000               # the field the bridge reads...
    assert "max_params" not in seen[0]            # ...not the one it ignores
    assert seen[0]["offset"] == 0 and seen[1]["offset"] == 1000


def test_scan_fx_parameters_error_passthrough(monkeypatch):
    monkeypatch.setattr(reaperd, "send_type",
                        lambda *a, **k: {"ok": False, "error": {"code": "NO_FX"}})
    params, err = reaperd.scan_fx_parameters({}, None)
    assert params is None
    assert err == {"code": "NO_FX"}


# --- fixes 10-11 (phase 3): platform correctness ----------------------------

class _Ret:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = ""


def test_reaper_running_linux_miss_is_unknown_not_dead(monkeypatch):
    monkeypatch.setattr(reaperd.platform, "system", lambda: "Linux")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return _Ret(1)
    monkeypatch.setattr(reaperd.subprocess, "run", fake_run)
    assert reaperd.reaper_running() is None       # heartbeat decides, not pgrep
    assert seen["argv"][-1] == "REAPER|reaper"    # lowercase binary matched too


def test_reaper_running_linux_hit_is_true(monkeypatch):
    monkeypatch.setattr(reaperd.platform, "system", lambda: "Linux")
    monkeypatch.setattr(reaperd.subprocess, "run", lambda *a, **k: _Ret(0))
    assert reaperd.reaper_running() is True


def test_reaper_running_macos_miss_is_still_false(monkeypatch):
    monkeypatch.setattr(reaperd.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(reaperd.subprocess, "run", lambda *a, **k: _Ret(1))
    assert reaperd.reaper_running() is False


def test_status_falls_back_to_fresh_heartbeat_when_process_unknown(root, monkeypatch):
    monkeypatch.setattr(reaperd, "reaper_running", lambda: None)
    hb = os.path.join(root, "bridge", "heartbeat.json")
    with open(hb, "w", encoding="utf-8") as f:
        f.write(json.dumps({"project_name": "x", "alive_at": "t", "busy": "none"}))
    assert reaperd.status_ok(root, quiet=True) is True


def test_status_dead_when_process_definitely_gone(root, monkeypatch):
    monkeypatch.setattr(reaperd, "reaper_running", lambda: False)
    assert reaperd.status_ok(root, quiet=True) is False


# --- phase 5 minors ----------------------------------------------------------

def test_groove_gates_on_dead_bridge_instantly(root, monkeypatch, tmp_path):
    # A dead REAPER used to cost groove the full 20s insert timeout.
    monkeypatch.setattr(reaperd, "status_ok", lambda *a, **k: False)
    monkeypatch.setattr(reaperd.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError(
                            "groove must not render when the bridge is dead")))
    dsl = tmp_path / "beat.dsl"
    dsl.write_text("@tempo 144\n[v] bars=1 feel=mf\ngrid 16\nkick | x... |\n")
    args = argparse.Namespace(bridge_root=root, dsl=str(dsl), track=None,
                              position=None, tempo=None, seed=None, map=None)
    assert reaperd.cmd_groove(args) == 1


def test_status_busy_flag_expires(root, monkeypatch):
    # A heartbeat frozen at busy=render used to read BUSY forever, even days
    # after a death mid-render. Past the cap it must read dead.
    monkeypatch.setattr(reaperd, "reaper_running", lambda: True)
    hb = os.path.join(root, "bridge", "heartbeat.json")
    with open(hb, "w", encoding="utf-8") as f:
        f.write(json.dumps({"project_name": "x", "alive_at": "t", "busy": "render"}))
    fresh_busy = time.time() - 60
    os.utime(hb, (fresh_busy, fresh_busy))
    assert reaperd.status_ok(root, quiet=True) is True     # 1 min: still BUSY
    ancient = time.time() - 20 * 60
    os.utime(hb, (ancient, ancient))
    assert reaperd.status_ok(root, quiet=True) is False    # 20 min: dead


def test_strip_block_aborts_on_begin_without_end():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rd_install", os.path.join(REPO, "setup", "install.py"))
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)
    text = "user code\n" + install.BEGIN + "\nmanaged line\nmore user code\n"
    with pytest.raises(RuntimeError):
        install.strip_block(text)
    intact = f"a\n{install.BEGIN}\nx\n{install.END}\nb\n"
    assert install.strip_block(intact) == "a\nb\n"
