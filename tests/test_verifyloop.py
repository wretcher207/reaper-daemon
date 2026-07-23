"""Tests for verifyloop.measure (SPEC_VERIFY_LOOP Phase 1).

Everything runs against the scripted fake bridge — no live REAPER. The
postmortem-mode tests generate real WAV files and run the real analyze_wav
(skipped when Post Mortem isn't installed); the render_stats-mode tests
monkeypatch analyze_wav away so both degradation paths are covered on any
machine.
"""

import math
import os
import struct
import sys
import time
import wave

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verifyloop  # noqa: E402
from bridge_fakes import fake_bridge_script  # noqa: E402


def write_wav(path, seconds=0.5, rate=8000, amp=0.5, freq=440.0):
    """A small 16-bit mono WAV; amp=0 writes digital silence."""
    frames = int(rate * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        data = b"".join(
            struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(frames))
        w.writeframes(data)
    return path


def preflight_ok(**overrides):
    data = {"capture_allowed": True, "blockers": [], "warnings": [],
            "risk_gate": {"allow_risk_level_3": True,
                          "requires_restart_to_change": True},
            "sws_installed": True, "render_autoclose": True}
    data.update(overrides)
    return {"ok": True, "type": "get_capture_preflight", "data": data}


def context_reply(cursor=0.0, ts_start=0.0, ts_end=0.0):
    return {"ok": True, "type": "get_context",
            "data": {"cursor": {"seconds": cursor},
                     "time_selection": {"start": ts_start, "end": ts_end,
                                        "active": ts_end > ts_start}}}


def capture_reply(wav_path, lufs=-14.2, scope="isolated_track", verified=True,
                  track_name="Bass", touch=True):
    """Callable reply: echoes the requested bounds like the real bridge.
    touch=False keeps the WAV's existing mtime (the stale-file tests set it
    deliberately old)."""
    def build(cmd):
        p = cmd["payload"]
        if touch:
            # The real bridge writes the WAV during the render, after the
            # command was sent. Refresh the pre-made file's mtime to match —
            # and to keep the freshness gate from flaking when a slow test
            # run reaches the second measure >2s after the WAV was created.
            # Never let this kill the fake's thread (an unanswered command
            # costs the test the full 180s capture timeout).
            try:
                os.utime(wav_path, None)
            except OSError:
                pass
        return {"ok": True, "type": "capture_track_audio", "data": {
            "track": {"index": 1, "name": track_name, "guid": "{fake}"},
            "file_path": wav_path,
            "file_size_bytes": os.path.getsize(wav_path),
            "duration_seconds": p["duration_seconds"],
            "start_seconds": p["start_seconds"],
            "sample_rate": 48000,
            "render_loudness_lufs": lufs,
            "capture_scope": scope,
            "isolation_verified": verified,
        }}
    return build


def no_postmortem(monkeypatch):
    monkeypatch.setattr(verifyloop, "analyze_wav", None)


# --- refusals before any capture -------------------------------------------

def test_preflight_blocked_refuses_with_blocker_codes(root):
    record = []
    fake_bridge_script(root, [
        {"ok": True, "type": "get_capture_preflight", "data": {
            "capture_allowed": False,
            "blockers": [{"code": "capture_gated",
                          "message": "set allow_risk_level_3 true"}],
            "warnings": [],
            "risk_gate": {"allow_risk_level_3": False,
                          "requires_restart_to_change": True},
        }},
    ], record=record)
    result = verifyloop.measure("Bass", bridge_root=root)
    assert result["ok"] is False
    assert result["error"]["code"] == "CAPTURE_BLOCKED"
    assert "capture_gated" in result["error"]["details"]
    # The risk-gate restart trap must be surfaced — users always hit it.
    assert "requires_restart_to_change" in result["error"]["details"]
    assert "restart" in result["error"]["details"].lower()
    # Preflight only; no capture was attempted.
    assert [c["type"] for c in record] == ["get_capture_preflight"]


def test_preflight_error_passthrough(root):
    fake_bridge_script(root, [
        {"ok": False, "error": {"code": "NO_TARGET_TRACK", "details": "nope"}},
    ])
    result = verifyloop.measure("Ghost", bridge_root=root)
    assert result["ok"] is False
    assert result["error"]["code"] == "NO_TARGET_TRACK"


def test_bad_seconds_refused_before_any_command(root):
    record = []
    fake_bridge_script(root, [], record=record)
    for bad in (0, -1, 61):
        result = verifyloop.measure("Bass", seconds=bad, bridge_root=root)
        assert result["ok"] is False
        assert result["error"]["code"] == "BAD_SECONDS"
    time.sleep(0.1)
    # Window args are validated before ANY bridge traffic: not even a
    # preflight goes out on bad seconds (record stays empty).
    assert record == []


# --- bounds resolution and freezing ----------------------------------------

def test_explicit_start_skips_context_and_freezes_bounds(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "c.wav"))
    record = []
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav)], record=record)
    result = verifyloop.measure("Bass", seconds=8, start=5.0, bridge_root=root)
    assert result["ok"] is True
    assert [c["type"] for c in record] == ["get_capture_preflight",
                                           "capture_track_audio"]
    payload = record[1]["payload"]
    assert payload["start_seconds"] == 5.0
    assert payload["duration_seconds"] == 8.0
    assert result["bounds"] == {"start_seconds": 5.0, "duration_seconds": 8.0,
                                "source": "explicit"}


def test_time_selection_bounds_and_duration_clamp(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "c.wav"))
    record = []
    fake_bridge_script(root, [preflight_ok(),
                              context_reply(cursor=99.0, ts_start=3.0, ts_end=7.0),
                              capture_reply(wav)], record=record)
    result = verifyloop.measure("Bass", seconds=10, bridge_root=root)
    assert result["ok"] is True
    payload = record[2]["payload"]
    assert payload["start_seconds"] == 3.0
    assert payload["duration_seconds"] == 4.0  # clamped to the selection
    assert result["bounds"]["source"] == "time_selection"


def test_cursor_fallback_bounds(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "c.wav"))
    record = []
    fake_bridge_script(root, [preflight_ok(), context_reply(cursor=12.25),
                              capture_reply(wav)], record=record)
    result = verifyloop.measure("Bass", bridge_root=root)
    assert result["ok"] is True
    payload = record[2]["payload"]
    assert payload["start_seconds"] == 12.25
    assert payload["duration_seconds"] == verifyloop.DEFAULT_SECONDS
    assert result["bounds"]["source"] == "edit_cursor"


def test_reused_bounds_sent_identically(root, tmp_path, monkeypatch):
    """verify's core guarantee: a second measure with the first's bounds dict
    sends byte-identical start/duration to the bridge."""
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "c.wav"))
    record = []
    fake_bridge_script(root, [preflight_ok(), context_reply(cursor=12.25),
                              capture_reply(wav),
                              preflight_ok(), capture_reply(wav)],
                       record=record)
    first = verifyloop.measure("Bass", bridge_root=root, keep_wav=True)
    second = verifyloop.measure("Bass", bounds=first["bounds"],
                                bridge_root=root, keep_wav=True)
    assert second["ok"] is True
    captures = [c for c in record if c["type"] == "capture_track_audio"]
    assert len(captures) == 2
    assert (captures[0]["payload"]["start_seconds"]
            == captures[1]["payload"]["start_seconds"])
    assert (captures[0]["payload"]["duration_seconds"]
            == captures[1]["payload"]["duration_seconds"])
    # And no second get_context happened: the window was frozen, not re-resolved.
    assert [c["type"] for c in record].count("get_context") == 1


# --- metrics modes ----------------------------------------------------------

def test_render_stats_mode_reports_lufs_and_source(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "c.wav"))
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav, lufs=-14.2)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["ok"] is True
    assert result["metrics_source"] == "render_stats"
    assert result["metrics"]["lufs_i"] == -14.2
    assert result["silent"] is False
    assert result["silence_basis"] == "lufs_i"


def test_render_stats_mode_missing_lufs_warns_unassessed(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "c.wav"))
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav, lufs=None)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["ok"] is True
    assert result["silent"] is False
    assert result["silence_basis"] is None
    assert any("silence could not be assessed" in w for w in result["warnings"])


def test_postmortem_mode_full_metrics(root, tmp_path):
    pytest.importorskip("postmortem")
    wav = write_wav(str(tmp_path / "c.wav"), amp=0.5)
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["ok"] is True
    assert result["metrics_source"] == "postmortem"
    m = result["metrics"]
    assert m["lufs_i"] == -14.2  # still from RENDER_STATS, not recomputed
    assert m["rms_db"] < 0
    assert m["spectrum_third_octave"]
    assert result["silent"] is False


def test_silent_capture_flagged_postmortem(root, tmp_path):
    pytest.importorskip("postmortem")
    wav = write_wav(str(tmp_path / "silent.wav"), amp=0.0)
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav, lufs=None)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["ok"] is True
    assert result["silent"] is True
    assert result["silence_basis"] == "postmortem"
    assert any("SILENT" in w for w in result["warnings"])
    # A silent capture's WAV is evidence for debugging — it must be kept.
    assert result["capture"]["file_kept"] is True
    assert os.path.exists(wav)


def test_silent_by_lufs_in_render_stats_mode(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "c.wav"))
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav, lufs=-72.5)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["silent"] is True
    assert result["silence_basis"] == "lufs_i"


def test_analysis_failure_degrades_to_render_stats(root, tmp_path, monkeypatch):
    pytest.importorskip("postmortem")
    bad = str(tmp_path / "bad.wav")
    with open(bad, "wb") as f:
        f.write(b"not a wav at all")
    fake_bridge_script(root, [preflight_ok(), capture_reply(bad, lufs=-14.2)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["ok"] is True
    assert result["metrics_source"] == "render_stats"
    assert result["metrics"]["lufs_i"] == -14.2
    assert any("degraded" in w for w in result["warnings"])


# --- capture failure paths --------------------------------------------------

def test_capture_error_passthrough(root):
    fake_bridge_script(root, [preflight_ok(),
                              {"ok": False, "error": {"code": "CAPTURE_FAILED",
                                                      "details": "empty file"}}])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["ok"] is False
    assert result["error"]["code"] == "CAPTURE_FAILED"
    assert result["bounds"]["start_seconds"] == 0.0


def test_stale_capture_file_rejected(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "old.wav"))
    hour_ago = time.time() - 3600
    os.utime(wav, (hour_ago, hour_ago))
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav, touch=False)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["ok"] is False
    assert result["error"]["code"] == "STALE_CAPTURE_FILE"
    assert os.path.exists(wav)  # kept for debugging


def test_missing_capture_file_rejected(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    ghost = str(tmp_path / "never-written.wav")

    def build(cmd):
        return {"ok": True, "type": "capture_track_audio", "data": {
            "file_path": ghost, "render_loudness_lufs": -14.0,
            "capture_scope": "isolated_track", "isolation_verified": True}}
    fake_bridge_script(root, [preflight_ok(), build])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["ok"] is False
    assert result["error"]["code"] == "CAPTURE_FILE_MISSING"


# --- scope honesty and file lifecycle ---------------------------------------

def test_unisolated_scope_warns(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "c.wav"))
    fake_bridge_script(root, [preflight_ok(),
                              capture_reply(wav, scope="full_mix", verified=False)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["ok"] is True
    assert any("full_mix" in w and "NOT necessarily the track alone" in w
               for w in result["warnings"])


def test_wav_deleted_on_success_kept_on_request(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav1 = write_wav(str(tmp_path / "a.wav"))
    wav2 = write_wav(str(tmp_path / "b.wav"))
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav1),
                              preflight_ok(), capture_reply(wav2)])
    gone = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    kept = verifyloop.measure("Bass", start=0.0, bridge_root=root, keep_wav=True)
    assert gone["capture"]["file_kept"] is False and not os.path.exists(wav1)
    assert kept["capture"]["file_kept"] is True and os.path.exists(wav2)


# --- gate findings: validation, evidence, transport hygiene ------------------

def test_nonfinite_start_refused_before_any_command(root):
    record = []
    fake_bridge_script(root, [], record=record)
    for bad in (float("nan"), float("inf"), float("-inf")):
        result = verifyloop.measure("Bass", start=bad, bridge_root=root)
        assert result["ok"] is False
        assert result["error"]["code"] == "BAD_START"
    time.sleep(0.1)
    assert record == []


def test_malformed_bounds_dict_errors_instead_of_raising(root):
    fake_bridge_script(root, [preflight_ok()])
    for bad in ({"start_seconds": "x", "duration_seconds": 10},
                {"duration_seconds": 10}, {"start_seconds": 1}, "nonsense",
                {"start_seconds": 1, "duration_seconds": 600}):
        result = verifyloop.measure("Bass", bounds=bad, bridge_root=root)
        assert result["ok"] is False
        assert result["error"]["code"] == "BAD_BOUNDS"


def test_degraded_analysis_keeps_the_wav(root, tmp_path):
    # The WAV is the evidence of WHY analysis failed; deleting it on the
    # degrade path would destroy the only debuggable artifact.
    pytest.importorskip("postmortem")
    bad = str(tmp_path / "bad.wav")
    with open(bad, "wb") as f:
        f.write(b"not a wav at all")
    fake_bridge_script(root, [preflight_ok(), capture_reply(bad, lufs=-14.2)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert result["ok"] is True
    assert result["metrics_source"] == "render_stats"
    assert result["capture"]["file_kept"] is True
    assert os.path.exists(bad)


def test_truncated_capture_warns(root, tmp_path):
    pytest.importorskip("postmortem")
    wav1 = write_wav(str(tmp_path / "short1.wav"), seconds=0.5)
    wav2 = write_wav(str(tmp_path / "short2.wav"), seconds=0.5)
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav1),
                              preflight_ok(), capture_reply(wav2)])
    short = verifyloop.measure("Bass", start=0.0, seconds=10,
                               bridge_root=root, keep_wav=True)
    exact = verifyloop.measure("Bass", start=0.0, seconds=0.5,
                               bridge_root=root, keep_wav=True)
    assert any("truncated" in w for w in short["warnings"])
    assert not any("truncated" in w for w in exact["warnings"])


def test_capture_blocked_carries_structured_blockers(root):
    fake_bridge_script(root, [
        {"ok": True, "type": "get_capture_preflight", "data": {
            "capture_allowed": False,
            "blockers": [{"code": "capture_gated", "message": "gated"}],
            "risk_gate": {"allow_risk_level_3": False,
                          "requires_restart_to_change": True}}},
    ])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    # Machine callers (MCP in Phase 3) must not have to parse prose.
    assert result["blockers"][0]["code"] == "capture_gated"
    assert result["risk_gate"]["allow_risk_level_3"] is False
    assert "True" not in result["error"]["details"]  # no Python-repr leak


def test_preflight_warnings_bubble_into_result(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "c.wav"))
    fake_bridge_script(root, [
        preflight_ok(warnings=[{"code": "render_hang_risk",
                                "message": "auto-close can't be forced"}]),
        capture_reply(wav)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    assert any(w.startswith("preflight: [render_hang_risk]")
               for w in result["warnings"])


def test_interrupted_wait_withdraws_inbox_command(root, monkeypatch):
    # Ctrl+C during the (up to 3 min) capture wait must not leave a command
    # in inbox/ — the bridge has no age gate there and would execute it later
    # against whatever project is open then.
    def boom(_seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(time, "sleep", boom)
    with pytest.raises(KeyboardInterrupt):
        reaperd_send_interrupted(root)
    assert os.listdir(os.path.join(root, "inbox")) == []


def reaperd_send_interrupted(root):
    import reaperd
    reaperd.send_command({"type": "ping", "payload": {}}, wait=True,
                         timeout_ms=5000, bridge_root=root)


# --- human formatter ---------------------------------------------------------

def test_format_measure_failure_and_success(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    text = verifyloop.format_measure(
        {"ok": False, "error": {"code": "CAPTURE_BLOCKED", "details": "gated"}})
    assert "FAILED" in text and "CAPTURE_BLOCKED" in text
    wav = write_wav(str(tmp_path / "c.wav"))
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav)])
    result = verifyloop.measure("Bass", start=0.0, bridge_root=root)
    text = verifyloop.format_measure(result)
    assert "LUFS" in text and "render_stats" in text
