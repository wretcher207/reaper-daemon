"""Phase 1 tests for verifyloop.measure (docs/SPEC_VERIFY_LOOP.md).

Everything runs without a live REAPER: the scripted fake bridge answers the
real inbox/outbox protocol through reaperd.send_type, so these tests exercise
the exact transport the CLI uses. Post Mortem analysis is injected as a fake
analyzer so both metrics modes are covered deterministically whether or not
the package is installed on the machine running the suite.
"""

import math
import os
import struct
import sys
import time
import types
import wave

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reaperd  # noqa: E402
import verifyloop  # noqa: E402
from bridge_fakes import fake_bridge_script  # noqa: E402


def _sender(root):
    def sender(cmd_type, payload, timeout_ms=10000):
        return reaperd.send_type(cmd_type, payload, bridge_root=root,
                                 timeout_ms=timeout_ms, resolve=False,
                                 repair=False)
    return sender


def _no_analyzer():
    return None


def _fake_stats(**overrides):
    base = dict(sample_peak_db=-3.0, rms_db=-18.0, crest_factor_db=15.0,
                silence_fraction=0.02,
                spectrum_third_octave=[{"freq_hz": 100, "level_db": -20.0}],
                stereo=None)
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _write_wav(path, seconds=0.05, rate=8000, amplitude=0.5):
    n = int(rate * seconds)
    frames = b"".join(
        struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(n))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


PREFLIGHT_OK = {"ok": True, "type": "get_capture_preflight", "data": {
    "capture_allowed": True, "blockers": [], "warnings": [],
    "risk_gate": {"allow_risk_level_3": True, "requires_restart_to_change": True},
    "sws_installed": True, "render_autoclose": True}}

PREFLIGHT_GATED = {"ok": True, "type": "get_capture_preflight", "data": {
    "capture_allowed": False,
    "blockers": [{"code": "capture_gated",
                  "message": "allow_risk_level_3 is false"}],
    "warnings": [],
    "risk_gate": {"allow_risk_level_3": False, "requires_restart_to_change": True},
    "sws_installed": True, "render_autoclose": True}}


def _context(cursor=3.5, ts=None):
    ts = ts or {"active": False, "start": 0, "end": 0}
    return {"ok": True, "type": "get_context",
            "data": {"cursor": {"seconds": cursor, "bar": 1},
                     "time_selection": ts}}


def _capture_responder(lufs=-14.1, raw="LUFSI:-14.10;TRUEPEAK:-3.20;LRA:4.50",
                       scope="isolated_track", verified=True, stale=False):
    """A scripted reply that mimics the real bridge: writes the WAV the
    payload asked for, then reports its path back."""
    def responder(command):
        out = command["payload"]["output_file"]
        _write_wav(out)
        if stale:
            old = time.time() - 3600
            os.utime(out, (old, old))
        return {"ok": True, "type": "capture_track_audio", "data": {
            "track": {"index": 2, "name": "Bass", "guid": "{B}"},
            "file_path": out,
            "file_size_bytes": os.path.getsize(out),
            "render_loudness_lufs": lufs,
            "render_stats_raw": raw,
            "capture_scope": scope,
            "isolation_verified": verified,
        }}
    return responder


# --- refusals -------------------------------------------------------------

def test_preflight_blocked_refuses_with_blocker_codes_and_restart_note(root):
    fake_bridge_script(root, [PREFLIGHT_GATED])
    res = verifyloop.measure(_sender(root), "Bass",
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "CAPTURE_BLOCKED"
    assert [b["code"] for b in res["blockers"]] == ["capture_gated"]
    assert res["risk_gate"]["requires_restart_to_change"] is True
    assert "requires_restart_to_change" in res["error"]["details"]
    assert "RESTART REAPER" in res["error"]["details"]


def test_preflight_transport_failure_is_reported(root):
    # No fake bridge at all: send_type times out -> PREFLIGHT_FAILED.
    def sender(cmd_type, payload, timeout_ms=10000):
        return {"ok": False, "error": {"code": "TIMEOUT", "details": "no bridge"}}
    res = verifyloop.measure(sender, "Bass", _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "PREFLIGHT_FAILED"


@pytest.mark.parametrize("seconds", [0, 0.5, 61, "ten"])
def test_bad_seconds_refused_before_any_bridge_traffic(seconds):
    def sender(*a, **k):
        raise AssertionError("sender must not be called")
    res = verifyloop.measure(sender, "Bass", seconds=seconds,
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "BAD_SECONDS"


def test_capture_failure_passes_error_through(root):
    fake_bridge_script(root, [
        PREFLIGHT_OK, _context(),
        {"ok": False, "error": {"code": "CAPTURE_FAILED",
                                "details": "render produced no file"}},
    ])
    res = verifyloop.measure(_sender(root), "Bass",
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "CAPTURE_FAILED"


# --- happy path & bounds ---------------------------------------------------

def test_happy_path_returns_bounds_lufs_and_render_stats(root, tmp_path):
    record = []
    fake_bridge_script(root, [PREFLIGHT_OK, _context(cursor=3.5),
                              _capture_responder()], record=record)
    out_dir = str(tmp_path / "wavs")
    res = verifyloop.measure(_sender(root), "Bass", output_dir=out_dir,
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is True
    assert res["lufs_i"] == -14.1
    assert res["true_peak_db"] == -3.2
    assert res["loudness_range_lu"] == 4.5
    assert res["metrics_source"] == "render_stats"
    assert res["bounds"] == {"start_seconds": 3.5, "duration_seconds": 10.0}
    assert res["bounds_source"] == "edit_cursor"
    assert res["capture_scope"] == "isolated_track"
    assert res["isolation_verified"] is True
    assert res["silent"] is False
    capture_cmd = record[-1]
    assert capture_cmd["type"] == "capture_track_audio"
    assert capture_cmd["payload"]["start_seconds"] == 3.5
    assert capture_cmd["payload"]["duration_seconds"] == 10.0
    assert capture_cmd["payload"]["output_file"].endswith(".wav")
    # WAV cleaned up on success by default
    assert res["wav_kept"] is False
    assert not os.path.exists(res["file_path"])


def test_time_selection_bounds_start_and_clamp(root, tmp_path):
    record = []
    fake_bridge_script(root, [
        PREFLIGHT_OK,
        _context(ts={"active": True, "start": 10.0, "end": 14.0}),
        _capture_responder(),
    ], record=record)
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is True
    assert res["bounds"] == {"start_seconds": 10.0, "duration_seconds": 4.0}
    assert res["bounds_source"] == "time_selection"
    assert record[-1]["payload"]["start_seconds"] == 10.0
    assert record[-1]["payload"]["duration_seconds"] == 4.0


def test_explicit_start_skips_context_resolution(root, tmp_path):
    record = []
    fake_bridge_script(root, [PREFLIGHT_OK, _capture_responder()],
                       record=record)
    res = verifyloop.measure(_sender(root), "Bass", start_seconds=5.25,
                             seconds=8,
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is True
    assert res["bounds"] == {"start_seconds": 5.25, "duration_seconds": 8.0}
    assert res["bounds_source"] == "explicit_start"
    # only preflight + capture — no get_context round-trip
    assert [c["type"] for c in record] == ["get_capture_preflight",
                                          "capture_track_audio"]


def test_keep_wav_preserves_the_file(root, tmp_path):
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), _capture_responder()])
    res = verifyloop.measure(_sender(root), "Bass", keep_wav=True,
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is True
    assert res["wav_kept"] is True
    assert os.path.isfile(res["file_path"])


# --- metrics modes ---------------------------------------------------------

def test_postmortem_mode_adds_analysis_fields(root, tmp_path):
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), _capture_responder()])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=lambda: lambda p: _fake_stats())
    assert res["ok"] is True
    assert res["metrics_source"] == "postmortem"
    assert res["rms_db"] == -18.0
    assert res["sample_peak_db"] == -3.0
    assert res["silence_fraction"] == 0.02
    assert res["spectrum_third_octave"]
    assert res["silent"] is False


def test_missing_postmortem_degrades_to_render_stats(root, tmp_path):
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), _capture_responder()])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["metrics_source"] == "render_stats"
    assert "sample_peak_db" not in res
    assert "rms_db" not in res


def test_analysis_crash_degrades_and_reports(root, tmp_path):
    def broken(path):
        raise ValueError("not a RIFF/WAVE file")
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), _capture_responder()])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=lambda: broken)
    assert res["ok"] is True
    assert res["metrics_source"] == "render_stats"
    assert "not a RIFF/WAVE" in res["analysis_error"]


def test_real_postmortem_analysis_when_installed(root, tmp_path):
    pytest.importorskip("postmortem.analysis")
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), _capture_responder()])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"))
    assert res["ok"] is True
    assert res["metrics_source"] == "postmortem"
    assert res["rms_db"] is not None
    assert res["spectrum_third_octave"]


# --- silence guard ---------------------------------------------------------

@pytest.mark.parametrize("stats_kw, reason_bit", [
    (dict(rms_db=-80.0), "essentially silent"),
    (dict(silence_fraction=0.9), "silence"),
])
def test_silent_capture_flagged_postmortem_mode(root, tmp_path, stats_kw, reason_bit):
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), _capture_responder()])
    res = verifyloop.measure(
        _sender(root), "Bass", output_dir=str(tmp_path / "wavs"),
        _analyzer_loader=lambda: lambda p: _fake_stats(**stats_kw))
    assert res["ok"] is True
    assert res["silent"] is True
    assert reason_bit in res["silence_reason"]


def test_render_stats_mode_low_lufs_is_silent(root, tmp_path):
    fake_bridge_script(root, [PREFLIGHT_OK, _context(),
                              _capture_responder(lufs=-72.0, raw="LUFSI:-72.0")])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["silent"] is True


def test_render_stats_mode_null_lufs_is_silent_not_a_pass(root, tmp_path):
    # Digital silence renders LUFS -inf, which the bridge maps to null. With
    # no Post Mortem there is no other level evidence — must NOT pass as ok.
    fake_bridge_script(root, [PREFLIGHT_OK, _context(),
                              _capture_responder(lufs=None, raw=None)])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is True
    assert res["silent"] is True
    assert "Post Mortem" in res["silence_reason"]


# --- freshness / staleness -------------------------------------------------

def test_stale_capture_file_rejected(root, tmp_path):
    fake_bridge_script(root, [PREFLIGHT_OK, _context(),
                              _capture_responder(stale=True)])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "STALE_CAPTURE_FILE"


def test_missing_capture_file_rejected(root, tmp_path):
    def responder(command):
        # Reply claims a path nothing ever wrote.
        return {"ok": True, "type": "capture_track_audio", "data": {
            "file_path": os.path.join(str(tmp_path), "ghost.wav"),
            "capture_scope": "isolated_track", "isolation_verified": True,
            "render_loudness_lufs": -12.0}}
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), responder])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "CAPTURE_FILE_MISSING"


# --- scope honesty in the human output --------------------------------------

def test_format_notes_non_isolated_scope(root, tmp_path):
    fake_bridge_script(root, [PREFLIGHT_OK, _context(),
                              _capture_responder(scope="full_mix",
                                                 verified=False)])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is True
    assert res["isolation_verified"] is False
    text = verifyloop.format_measure(res)
    assert "not necessarily this track alone" in text


# --- parse_render_stats unit tests ------------------------------------------

def test_parse_render_stats_maps_known_keys():
    out = verifyloop.parse_render_stats(
        "LUFSI:-14.1;TRUEPEAK:-1.20;LRA:6.3;LUFSM:-10.0;FILE:C\\out.wav")
    assert out == {"true_peak_db": -1.2, "loudness_range_lu": 6.3,
                   "lufs_momentary_max": -10.0}


def test_parse_render_stats_first_spelling_wins_and_skips_junk():
    out = verifyloop.parse_render_stats("TPK:-2.0;TRUEPEAK:-1.0;BOGUS;X:notanum")
    assert out["true_peak_db"] == -1.0  # TRUEPEAK listed before TPK


@pytest.mark.parametrize("raw", [None, "", "FILE:C:\\x.wav"])
def test_parse_render_stats_empty(raw):
    assert verifyloop.parse_render_stats(raw) == {}
