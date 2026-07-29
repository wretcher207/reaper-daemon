"""Phase 1 tests for verifyloop.measure (docs/SPEC_VERIFY_LOOP.md).

Everything runs without a live REAPER: the scripted fake bridge answers the
real inbox/outbox protocol through reaperd.send_type, so these tests exercise
the exact transport the CLI uses. Post Mortem analysis is injected as a fake
analyzer so both metrics modes are covered deterministically whether or not
the package is installed on the machine running the suite.
"""

import json
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
                       scope="isolated_track", verified=True,
                       backdate_by=None, report_other_file=False,
                       bounds_override=None, guid="{B}"):
    """A scripted reply that mimics the real bridge: writes the WAV the
    payload asked for, echoes the rendered bounds, reports the path back.
    backdate_by backdates the reported WAV's mtime by that many seconds;
    report_other_file reports a DIFFERENT path than requested (the replayed-
    reply shape); bounds_override fakes a different rendered window."""
    def responder(command):
        payload = command["payload"]
        out = payload["output_file"]
        if report_other_file:
            out = out + "-other.wav"
        _write_wav(out)
        if backdate_by is not None:
            old = time.time() - backdate_by
            os.utime(out, (old, old))
        echoed = {"start_seconds": payload.get("start_seconds"),
                  "duration_seconds": payload.get("duration_seconds")}
        if bounds_override:
            echoed.update(bounds_override)
        return {"ok": True, "type": "capture_track_audio", "data": {
            "track": {"index": 2, "name": "Bass", "guid": guid},
            "file_path": out,
            "file_size_bytes": os.path.getsize(out),
            "render_loudness_lufs": lufs,
            "render_stats_raw": raw,
            "capture_scope": scope,
            "isolation_verified": verified,
            **echoed,
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

@pytest.mark.parametrize("stale_by", [3600, 1])
def test_stale_other_file_rejected(root, tmp_path, stale_by):
    # A reply reporting a DIFFERENT path than requested (the replayed-reply
    # shape) gets the strict mtime check: even 1 second pre-send must be
    # rejected — there is NO slack window (Codex gate findings, 2026-07-23;
    # the 1 s case exists so reintroducing the old 2 s slack fails the suite).
    fake_bridge_script(root, [PREFLIGHT_OK, _context(),
                              _capture_responder(report_other_file=True,
                                                 backdate_by=stale_by)])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "STALE_CAPTURE_FILE"


def test_own_unique_path_accepted_despite_coarse_mtime(root, tmp_path):
    # Coarse-timestamp filesystems can round mtime BELOW the send time for a
    # genuinely fresh file. When the reply reports our own unique output path
    # (proven non-existent before the send), the file is fresh by
    # construction and a rounded-down mtime must NOT reject it.
    fake_bridge_script(root, [PREFLIGHT_OK, _context(),
                              _capture_responder(backdate_by=1)])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is True


@pytest.mark.parametrize("override", [
    {"start_seconds": 8.5},            # real mismatch
    {"start_seconds": None},           # missing/null echo — cannot confirm
    {"start_seconds": float("nan")},   # non-finite echo (arrives as NaN/null)
    {"duration_seconds": "bogus"},     # non-numeric echo must not crash
])
def test_unconfirmed_or_mismatched_bounds_rejected(root, tmp_path, override):
    # "Cannot confirm the rendered window" must never read as "verified" —
    # missing, null, non-finite, and non-numeric echoes all fail closed.
    fake_bridge_script(root, [PREFLIGHT_OK, _context(cursor=3.5),
                              _capture_responder(bounds_override=override)])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "BOUNDS_MISMATCH"


def test_capture_failure_discloses_possible_partial_wav(root, tmp_path):
    fake_bridge_script(root, [
        PREFLIGHT_OK, _context(),
        {"ok": False, "error": {"code": "RENDER_PREFERENCES_RESTORE_FAILED",
                                "details": "restore failed after render"}},
    ])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["output_file"].endswith(".wav")


def test_nan_metrics_are_sanitized_and_fail_closed_silent(root, tmp_path):
    # NaN poisons threshold comparisons (all False) and breaks strict JSON —
    # must sanitize to None and treat the capture as unusable (silent).
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), _capture_responder()])
    res = verifyloop.measure(
        _sender(root), "Bass", output_dir=str(tmp_path / "wavs"),
        _analyzer_loader=lambda: lambda p: _fake_stats(
            rms_db=float("nan"), sample_peak_db=float("inf")))
    assert res["ok"] is True
    assert res["rms_db"] is None
    assert res["sample_peak_db"] is None
    assert res["silent"] is True
    assert "not a finite number" in res["silence_reason"]
    json.dumps(res, allow_nan=False)  # must not raise


def test_analyzer_bad_shape_degrades_and_still_cleans_up(root, tmp_path):
    # An incompatible Post Mortem returning a wrong-shaped object must
    # degrade to render_stats — not crash measure and leak the WAV.
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), _capture_responder()])
    bad = types.SimpleNamespace(sample_peak_db=-3.0)  # missing every other field
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=lambda: lambda p: bad)
    assert res["ok"] is True
    assert res["metrics_source"] == "render_stats"
    assert "AttributeError" in res["analysis_error"]
    assert res["wav_kept"] is False
    assert not os.path.exists(res["file_path"])


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


# --- verify: measure -> mutate -> measure -> verdict ------------------------

MUT_OK = {"ok": True, "type": "set_fx_param", "data": {"applied": True}}
MUT_FAIL = {"ok": False, "error": {"code": "NO_FX_PARAM",
                                   "details": "no matching parameter"}}
SET_PARAM = {"target_track_name": "Bass", "fx_name_contains": "EQ",
             "param_index": 3, "value": 0.4}


def _mutator(root):
    def mutate(cmd_type, payload):
        return reaperd.send_type(cmd_type, payload, bridge_root=root,
                                 resolve=True, repair=True)
    return mutate


def _run_verify(root, tmp_path, replies, record=None, analyzer=_no_analyzer,
                cmd_type="set_fx_param", payload=None, **kwargs):
    fake_bridge_script(root, replies, record=record)
    return verifyloop.verify(
        _sender(root), _mutator(root), "Bass", cmd_type,
        dict(SET_PARAM) if payload is None else payload,
        output_dir=str(tmp_path / "wavs"), _analyzer_loader=analyzer, **kwargs)


def test_verify_verified_happy_path(root, tmp_path):
    record = []
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(cursor=3.5),
        _capture_responder(lufs=-14.1, raw="LUFSI:-14.10;TRUEPEAK:-3.20"),
        MUT_OK,
        PREFLIGHT_OK,
        _capture_responder(lufs=-14.9, raw="LUFSI:-14.90;TRUEPEAK:-3.90"),
    ], record=record)
    assert res["status"] == "VERIFIED"
    assert res["exit_code"] == 0
    assert res["deltas"]["lufs_i_delta"] == -0.8
    assert res["deltas"]["true_peak_db_delta"] == -0.7
    assert res["deltas"]["masking"] == "not applicable: single-track verify"
    assert res["scope_warning"] is None
    # mutation went through the cmd path: value -> normalized_value repair
    mut_cmd = [c for c in record if c["type"] == "set_fx_param"]
    assert len(mut_cmd) == 1
    assert mut_cmd[0]["payload"]["normalized_value"] == 0.4
    assert "value" not in mut_cmd[0]["payload"]


def test_verify_pre_post_bounds_byte_identical(root, tmp_path):
    record = []
    _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(ts={"active": True, "start": 12.25, "end": 15.75}),
        _capture_responder(), MUT_OK, PREFLIGHT_OK, _capture_responder(),
    ], record=record)
    captures = [c for c in record if c["type"] == "capture_track_audio"]
    assert len(captures) == 2
    for key in ("start_seconds", "duration_seconds"):
        a, b = captures[0]["payload"][key], captures[1]["payload"][key]
        assert json.dumps(a) == json.dumps(b)  # byte-identical on the wire
    assert captures[0]["payload"]["start_seconds"] == 12.25
    assert captures[0]["payload"]["duration_seconds"] == 3.5
    # post-measure resolves NO context (bounds frozen from pre)
    assert [c["type"] for c in record].count("get_context") == 1


@pytest.mark.parametrize("code", ["NO_TARGET_TRACK",     # resolution-stage
                                  "SET_PARAM_FAILED"])   # mid-handler
def test_verify_bridge_rejection_is_unverified_exit_2(root, tmp_path, code):
    # A readable rejection code is NOT proof of a clean project: a handler
    # that failed mid-edit leaves its partial change in one closed undo
    # block, and that cannot be told apart from a clean resolution
    # rejection from this side (nor across bridge versions). Exit 1's
    # "nothing was changed" promise is reserved for pre-send refusals;
    # every post-send rejection fails toward exit 2.
    record = []
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(),
        {"ok": False, "error": {"code": code, "details": "rejected"}},
    ], record=record)
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert res["post"] is None
    assert res["mutation"]["rejection_code"] == code
    assert "rejected" in res["note"]
    assert "Do not retry blindly" in res["note"]
    assert "Ctrl/Cmd+Z" in res["note"]
    assert [c["type"] for c in record].count("capture_track_audio") == 1


def test_verify_failed_batch_is_unverified_not_mutation_failed(root, tmp_path):
    # The bridge KEEPS sub-commands that ran before a batch failure (one
    # closed undo block). Exit 1 would promise "nothing changed" — a lie an
    # agent would compound by retrying. Must be exit 2 (Codex gate BLOCKER,
    # 2026-07-23).
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(),
        {"ok": False, "error": {"code": "BATCH_FAILED",
                                "details": "sub-command 2 failed"}},
    ], cmd_type="batch",
        payload={"commands": [{"type": "set_fx_param", "payload": {}},
                              {"type": "set_fx_param", "payload": {}}]})
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert "REMAIN APPLIED" in res["note"]
    assert "Do not retry blindly" in res["note"]


@pytest.mark.parametrize("code", ["TIMEOUT", "NO_REPLY", "BAD_REPLY",
                                  "STALE_REPLY_LOCKED"])
def test_verify_uncertain_mutation_outcome_is_unverified(root, tmp_path, code):
    # A timed-out or unreadable mutation may have executed (or may execute
    # later) — exit 1's "nothing was changed" promise cannot be made, so
    # these are exit 2 with a do-not-retry note (Codex gate BLOCKER).
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(),
        {"ok": False, "error": {"code": code, "details": "transport hiccup"}},
    ])
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert "UNKNOWN" in res["note"]
    assert "Do not retry blindly" in res["note"]


def test_verify_unverified_when_post_capture_fails(root, tmp_path):
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(), MUT_OK,
        PREFLIGHT_OK,
        {"ok": False, "error": {"code": "CAPTURE_FAILED",
                                "details": "render died"}},
    ])
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert "NOT rolled back" in res["note"]
    assert "Ctrl/Cmd+Z" in res["note"]


def test_verify_unverified_when_post_capture_is_silent(root, tmp_path):
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(), MUT_OK,
        PREFLIGHT_OK, _capture_responder(lufs=-75.0, raw="LUFSI:-75.0"),
    ])
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert "SILENT" in res["note"]
    assert "Ctrl/Cmd+Z" in res["note"]


def test_verify_refused_when_pre_capture_blocked(root, tmp_path):
    record = []
    res = _run_verify(root, tmp_path, [PREFLIGHT_GATED], record=record)
    assert res["status"] == "REFUSED"
    assert res["exit_code"] == 1
    assert "nothing was mutated" in res["note"]
    # the mutation was never sent
    assert all(c["type"] != "set_fx_param" for c in record)


def test_verify_refused_when_pre_capture_is_silent(root, tmp_path):
    record = []
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(),
        _capture_responder(lufs=-75.0, raw="LUFSI:-75.0"),
    ], record=record)
    assert res["status"] == "REFUSED"
    assert res["exit_code"] == 1
    assert "`cmd`" in res["note"]
    assert all(c["type"] != "set_fx_param" for c in record)


def test_verify_scope_change_is_unverified_not_verified(root, tmp_path):
    # Pre isolated, post full-mix (e.g. the mutation inserted media items and
    # changed the render fallback): unlike evidence — subtracting their LUFS
    # is not a measurement of the change. Exit 0 would tell a branching agent
    # both captures were clean (Codex gate MAJOR, 2026-07-23).
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(),
        MUT_OK, PREFLIGHT_OK,
        _capture_responder(scope="full_mix", verified=False),
    ])
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert "scope changed" in res["note"]


def test_verify_post_targets_pre_guid_and_guid_change_is_unverified(root, tmp_path):
    # Post-capture must pin the exact track the pre-capture resolved (GUID),
    # and a GUID mismatch (rename/reorder swapped names mid-verify) gets no
    # verdict (Codex gate MAJOR, 2026-07-23).
    record = []
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(guid="{A}"),
        MUT_OK, PREFLIGHT_OK, _capture_responder(guid="{Z}"),
    ], record=record)
    post_preflight, post_capture = record[-2], record[-1]
    assert post_preflight["payload"] == {"target_track_guid": "{A}"}
    assert post_capture["payload"]["target_track_guid"] == "{A}"
    assert "target_track_name" not in post_capture["payload"]
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert "DIFFERENT tracks" in res["note"]


def test_verify_lufs_delta_always_reported_null_when_unavailable(root, tmp_path):
    # REAPER supplied no LUFS but Post Mortem RMS keeps both captures
    # measurable: lufs_i_delta must still be REPORTED (null + reason), and
    # the verdict rests on the RMS delta (Codex gate MAJOR, 2026-07-23).
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(lufs=None, raw=None),
        MUT_OK, PREFLIGHT_OK, _capture_responder(lufs=None, raw=None),
    ], analyzer=lambda: lambda p: _fake_stats())
    assert res["status"] == "VERIFIED"
    assert res["deltas"]["lufs_i_delta"] is None
    assert "unavailable" in res["deltas"]["lufs_i_note"]
    assert res["deltas"]["rms_db_delta"] == 0.0


def test_verify_partial_batch_with_stop_on_error_false_is_unverified(root, tmp_path):
    # stop_on_error:false lets the bridge return top-level ok:true while
    # sub-commands failed — exit 0 would claim the whole batch succeeded
    # (Codex gate round-2 BLOCKER, 2026-07-23). Deltas are still measured
    # but the verdict must be UNVERIFIED.
    batch_reply = {"ok": True, "type": "batch", "data": {"results": [
        {"index": 1, "type": "set_fx_param", "ok": True, "data": {}},
        {"index": 2, "type": "set_fx_param", "ok": False,
         "error": "NO_FX_PARAM: nope"},
    ]}}
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(), batch_reply,
        PREFLIGHT_OK, _capture_responder(lufs=-14.9, raw="LUFSI:-14.90"),
    ], cmd_type="batch",
        payload={"stop_on_error": False,
                 "commands": [{"type": "set_fx_param", "payload": {}},
                              {"type": "set_fx_param", "payload": {}}]})
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert "PARTIALLY" in res["note"]
    assert res["deltas"]["lufs_i_delta"] == -0.8  # still measured, still honest


@pytest.mark.parametrize("batch_reply", [
    {"ok": True, "data": {"results": [None]}},        # null result entry
    {"ok": True, "data": "oops"},                     # data not an object
    {"ok": True, "data": {"results": 1}},             # results not a list
    {"ok": True, "data": {"results": []}},            # fewer results than commands
    {"ok": True},                                     # no data at all
])
def test_verify_malformed_batch_results_are_outcome_unknown(root, tmp_path,
                                                            batch_reply):
    # A top-level ok:true batch whose per-command results are malformed or
    # incomplete cannot PROVE the requested batch completed — never exit 0,
    # never a crash (Codex gate round-3 BLOCKER, 2026-07-23).
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(), batch_reply,
    ], cmd_type="batch",
        payload={"commands": [{"type": "set_fx_param", "payload": {}}]})
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert "UNKNOWN" in res["note"]


def test_verify_partial_batch_disclosed_even_when_post_capture_fails(root, tmp_path):
    batch_reply = {"ok": True, "type": "batch", "data": {"results": [
        {"index": 1, "type": "set_fx_param", "ok": True, "data": {}},
        {"index": 2, "type": "set_fx_param", "ok": False,
         "error": "NO_FX_PARAM: nope"},
    ]}}
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(), batch_reply,
        PREFLIGHT_OK,
        {"ok": False, "error": {"code": "CAPTURE_FAILED",
                                "details": "render died"}},
    ], cmd_type="batch",
        payload={"stop_on_error": False,
                 "commands": [{"type": "set_fx_param", "payload": {}},
                              {"type": "set_fx_param", "payload": {}}]})
    assert res["status"] == "UNVERIFIED"
    assert "PARTIALLY" in res["note"]  # disclosed on the failure path too
    text = verifyloop.format_verify(res)
    assert "PARTIAL" in text
    assert "mutation batch: ok" not in text


def test_verify_fully_ok_batch_still_verifies(root, tmp_path):
    batch_reply = {"ok": True, "type": "batch", "data": {"results": [
        {"index": 1, "type": "set_fx_param", "ok": True, "data": {}},
    ]}}
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(), batch_reply,
        PREFLIGHT_OK, _capture_responder(lufs=-14.9, raw="LUFSI:-14.90"),
    ], cmd_type="batch",
        payload={"commands": [{"type": "set_fx_param", "payload": {}}]})
    assert res["status"] == "VERIFIED"
    assert res["exit_code"] == 0


@pytest.mark.parametrize("reply", [
    None,                                    # raw null through a bad transport
    {"ok": False, "error": "oops"},          # error is a string, not an object
    {"ok": False},                           # no error field at all
    {"ok": False, "error": {"code": ""}},    # empty code proves nothing
    {"ok": False, "error": {"code": []}},    # unhashable code must not crash
    {"ok": False, "error": {"code": 7}},     # non-string code
])
def test_verify_malformed_mutation_reply_is_outcome_unknown(root, tmp_path, reply):
    # Wrong-shaped replies after the command was queued are version skew or
    # corruption — the mutation may have executed. Exit 1 would promise
    # "nothing changed"; must be exit 2, and must not crash (Codex gate
    # round-2 BLOCKER, 2026-07-23).
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), _capture_responder()])
    res = verifyloop.verify(
        _sender(root), lambda t, p: reply, "Bass", "set_fx_param",
        dict(SET_PARAM), output_dir=str(tmp_path / "wavs"),
        _analyzer_loader=_no_analyzer)
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert "UNKNOWN" in res["note"]


def test_verify_mutator_exception_is_outcome_unknown(root, tmp_path):
    def exploding_mutator(t, p):
        raise RuntimeError("transport blew up mid-send")
    fake_bridge_script(root, [PREFLIGHT_OK, _context(), _capture_responder()])
    res = verifyloop.verify(
        _sender(root), exploding_mutator, "Bass", "set_fx_param",
        dict(SET_PARAM), output_dir=str(tmp_path / "wavs"),
        _analyzer_loader=_no_analyzer)
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2


def test_verify_missing_pre_guid_refuses_before_mutation(root, tmp_path):
    # No GUID in the pre-capture -> the post-capture cannot be pinned; must
    # refuse BEFORE mutating, not fail open (Codex gate round-2 MAJOR).
    record = []
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(guid=None),
    ], record=record)
    assert res["status"] == "REFUSED"
    assert res["exit_code"] == 1
    assert "GUID" in res["note"]
    assert all(c["type"] != "set_fx_param" for c in record)


def test_verify_missing_post_guid_is_unverified(root, tmp_path):
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(), _capture_responder(guid="{A}"),
        MUT_OK, PREFLIGHT_OK, _capture_responder(guid=None),
    ])
    assert res["status"] == "UNVERIFIED"
    assert res["exit_code"] == 2
    assert "identity" in res["note"]


def test_verify_scope_warning_on_unverified_isolation(root, tmp_path):
    res = _run_verify(root, tmp_path, [
        PREFLIGHT_OK, _context(),
        _capture_responder(scope="full_mix", verified=False),
        MUT_OK, PREFLIGHT_OK,
        _capture_responder(scope="full_mix", verified=False),
    ])
    assert res["status"] == "VERIFIED"
    assert "not necessarily this track alone" in res["scope_warning"]


# --- compute_deltas / format_verify unit tests -------------------------------

def test_compute_deltas_with_canned_metrics():
    pre = {"lufs_i": -14.1, "rms_db": -18.0, "true_peak_db": -3.2,
           "silence_fraction": 0.02,
           "stereo": {"correlation": 0.9, "side_rms_db": -30.0},
           "spectrum_third_octave": [{"freq_hz": 100, "level_db": -20.0},
                                     {"freq_hz": 315, "level_db": -18.0}]}
    post = {"lufs_i": -14.9, "rms_db": -18.8, "true_peak_db": -3.9,
            "silence_fraction": 0.05,
            "stereo": {"correlation": 0.85, "side_rms_db": -31.0},
            "spectrum_third_octave": [{"freq_hz": 100, "level_db": -20.5},
                                      {"freq_hz": 315, "level_db": -21.1}]}
    d = verifyloop.compute_deltas(pre, post)
    assert d["lufs_i_delta"] == -0.8
    assert d["rms_db_delta"] == -0.8
    assert d["true_peak_db_delta"] == -0.7
    assert d["silence_fraction_delta"] == 0.03
    assert d["stereo"]["correlation_delta"] == -0.05
    assert d["stereo"]["side_rms_db_delta"] == -1.0
    bands = {b["freq_hz"]: b["delta_db"] for b in d["spectrum_band_deltas"]}
    assert bands == {100: -0.5, 315: -3.1}


def test_compute_deltas_stereo_width():
    pre = {"stereo": {"mid_rms_db": -20.0, "side_rms_db": -30.0}}   # width -10
    post = {"stereo": {"mid_rms_db": -20.0, "side_rms_db": -27.5}}  # width -7.5
    d = verifyloop.compute_deltas(pre, post)
    assert d["stereo"]["width_db_delta"] == 2.5  # positive = wider


def test_compute_deltas_never_fabricates_from_missing_values():
    d = verifyloop.compute_deltas({"lufs_i": None, "rms_db": -18.0},
                                  {"lufs_i": -14.0, "rms_db": None})
    assert "lufs_i_delta" not in d
    assert "rms_db_delta" not in d


def test_compute_deltas_rejects_nonfinite_and_bad_band_keys():
    # inf/NaN inputs must not become deltas, and a malformed band entry with
    # a None freq must not crash the sort (Codex gate MINOR, 2026-07-23).
    pre = {"lufs_i": float("-inf"), "rms_db": float("nan"),
           "spectrum_third_octave": [{"freq_hz": None, "level_db": -20.0},
                                     {"freq_hz": 100, "level_db": -20.0}]}
    post = {"lufs_i": -14.0, "rms_db": -18.0,
            "spectrum_third_octave": [{"freq_hz": 100, "level_db": -21.0}]}
    d = verifyloop.compute_deltas(pre, post)
    assert "lufs_i_delta" not in d
    assert "rms_db_delta" not in d
    assert [b["freq_hz"] for b in d["spectrum_band_deltas"]] == [100]


def test_format_verify_verified_and_unverified():
    verified = {"status": "VERIFIED", "exit_code": 0,
                "pre": {"ok": True, "lufs_i": -14.1,
                        "capture_scope": "isolated_track",
                        "isolation_verified": True},
                "post": {"ok": True, "lufs_i": -14.9,
                         "capture_scope": "isolated_track",
                         "isolation_verified": True},
                "mutation": {"type": "set_fx_param",
                             "reply": {"ok": True}},
                "deltas": {"lufs_i_delta": -0.8,
                           "spectrum_band_deltas": [
                               {"freq_hz": 315, "pre_db": -18.0,
                                "post_db": -21.1, "delta_db": -3.1}]},
                "scope_warning": None, "note": None}
    text = verifyloop.format_verify(verified)
    assert "VERDICT: VERIFIED" in text
    assert "dLUFS-I -0.80" in text
    assert "315 Hz -3.1 dB" in text

    unverified = {"status": "UNVERIFIED", "exit_code": 2,
                  "pre": {"ok": True, "lufs_i": -14.1,
                          "capture_scope": "isolated_track",
                          "isolation_verified": True},
                  "post": {"ok": False,
                           "error": {"code": "CAPTURE_FAILED",
                                     "details": "render died"}},
                  "mutation": {"type": "set_fx_param",
                               "reply": {"ok": True}},
                  "deltas": None, "scope_warning": None,
                  "note": "mutation applied but the post-capture failed. "
                          + verifyloop.NOT_ROLLED_BACK}
    text = verifyloop.format_verify(unverified)
    assert "VERDICT: UNVERIFIED" in text
    assert "one Ctrl/Cmd+Z away" in text


def test_format_verify_reports_null_lufs_with_reason():
    res = {"status": "VERIFIED", "exit_code": 0,
           "pre": {"ok": True, "lufs_i": None, "rms_db": -18.0,
                   "capture_scope": "isolated_track",
                   "isolation_verified": True},
           "post": {"ok": True, "lufs_i": None, "rms_db": -18.8,
                    "capture_scope": "isolated_track",
                    "isolation_verified": True},
           "mutation": {"type": "set_fx_param", "reply": {"ok": True}},
           "deltas": {"lufs_i_delta": None,
                      "lufs_i_note": "LUFS-I unavailable in pre and/or post "
                                     "capture (REAPER did not report it)",
                      "rms_db_delta": -0.8},
           "scope_warning": None, "note": None}
    text = verifyloop.format_verify(res)
    assert "dLUFS-I n/a" in text
    assert "LUFS-I unavailable" in text
    assert "dRMS -0.80 dB" in text


# --- band_energy_db unit tests ----------------------------------------------

def test_band_energy_db_power_sums_bands_in_range():
    spectrum = [{"freq_hz": 100, "level_db": -10.0},
                {"freq_hz": 125, "level_db": -10.0},
                {"freq_hz": 8000, "level_db": 0.0}]
    # two equal bands power-sum to +3.01 dB over one
    assert verifyloop.band_energy_db(spectrum, [90, 130]) == -6.99


def test_band_energy_db_empty_range_is_none():
    assert verifyloop.band_energy_db(
        [{"freq_hz": 8000, "level_db": -10.0}], [20, 200]) is None
    assert verifyloop.band_energy_db([], [20, 200]) is None


def test_band_energy_db_underflow_and_malformed_entries_do_not_crash():
    # -4000 dB underflows every power term to exactly 0.0 -> floor, not a
    # log10(0) crash; non-dict entries are skipped (Codex gate MINOR).
    spectrum = ["junk", None, {"freq_hz": 100, "level_db": -4000.0}]
    assert verifyloop.band_energy_db(spectrum, [90, 130]) == \
        verifyloop.BAND_ENERGY_FLOOR_DB


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


# --- malformed reply data shapes (fail closed, never crash) ------------------

def test_default_sender_timeout_constant_is_the_shared_contract():
    # reaperd._bridge_sender and reaper_mcp._verify_sender default to this.
    assert verifyloop.DEFAULT_SENDER_TIMEOUT_MS == 10000


@pytest.mark.parametrize("bad_data", ["oops", [1, 2, 3]])
def test_malformed_capture_data_is_error_not_crash(root, tmp_path, bad_data):
    # A wrong-shaped capture `data` passes send_type (only the TOP-LEVEL
    # reply must be a dict) — measure must return a structured error, never
    # crash into a traceback (adversarial review P2, 2026-07-29).
    fake_bridge_script(root, [PREFLIGHT_OK, _context(),
                              {"ok": True, "type": "capture_track_audio",
                               "data": bad_data}])
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "CAPTURE_FILE_MISSING"


def test_malformed_preflight_data_is_error_not_crash(root):
    fake_bridge_script(root, [{"ok": True, "type": "get_capture_preflight",
                               "data": [1, 2]}])
    res = verifyloop.measure(_sender(root), "Bass",
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "CAPTURE_BLOCKED"


@pytest.mark.parametrize("ts", [
    {"active": True, "start": "x", "end": 14.0},
    {"active": True, "start": 10.0, "end": "x"},
])
def test_non_numeric_time_selection_is_bounds_unresolved(root, ts):
    fake_bridge_script(root, [PREFLIGHT_OK, _context(ts=ts)])
    res = verifyloop.measure(_sender(root), "Bass",
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "BOUNDS_UNRESOLVED"
    assert "non-numeric" in res["error"]["details"]


def test_non_numeric_cursor_is_bounds_unresolved(root):
    fake_bridge_script(root, [PREFLIGHT_OK, _context(cursor="soon")])
    res = verifyloop.measure(_sender(root), "Bass",
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "BOUNDS_UNRESOLVED"


def test_sub_second_time_selection_refused(root):
    # The 1 s floor measure enforces on explicit seconds must not be
    # silently bypassed by a stray tiny time selection (review P2).
    fake_bridge_script(root, [
        PREFLIGHT_OK,
        _context(ts={"active": True, "start": 10.0, "end": 10.05}),
    ])
    res = verifyloop.measure(_sender(root), "Bass",
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "BOUNDS_UNRESOLVED"
    assert "shorter than the 1 s minimum" in res["error"]["details"]
    assert "0.05" in res["error"]["details"]


def test_vanished_capture_file_is_error_not_crash(root, tmp_path, monkeypatch):
    # The reported file can vanish between the isfile check and the mtime
    # stat (getmtime raises OSError) — must be the structured staleness
    # refusal, not a traceback (review P2).
    fake_bridge_script(root, [PREFLIGHT_OK, _context(),
                              _capture_responder(report_other_file=True)])

    def raising_getmtime(path):
        raise OSError("vanished between isfile and stat")
    monkeypatch.setattr(verifyloop.os.path, "getmtime", raising_getmtime)
    res = verifyloop.measure(_sender(root), "Bass",
                             output_dir=str(tmp_path / "wavs"),
                             _analyzer_loader=_no_analyzer)
    assert res["ok"] is False
    assert res["error"]["code"] == "STALE_CAPTURE_FILE"


# --- bool-as-number rejection -------------------------------------------------

def test_band_energy_db_rejects_bool_freq_and_level():
    # bool is an int subclass: True must not read as a 1 Hz band center or
    # a 1 dB level (review P3; matches compute_deltas' fin() convention).
    assert verifyloop.band_energy_db(
        [{"freq_hz": True, "level_db": -10.0}], [0, 2]) is None
    assert verifyloop.band_energy_db(
        [{"freq_hz": 100, "level_db": True}], [90, 130]) is None


def test_tune_metric_rejects_bool_lufs():
    assert verifyloop._tune_metric({"lufs_i": True},
                                   {"metric": "lufs_i"}) is None


# --- tune_param: scripted-sender unit tests -----------------------------------

def _scan_data(fx_guid="{FX}"):
    return {"track": {"guid": "{B}", "name": "Bass"},
            "fx": {"guid": fx_guid, "index": 0, "scope": "track",
                   "name": "EQ"},
            "parameters": [{"index": 3, "name": "Gain",
                            "normalized_value": 0.5,
                            "formatted_value": "0.0 dB"}]}


def _static_scanner(fx_guid="{FX}"):
    def scanner(base):
        return _scan_data(fx_guid), None
    return scanner


def _tune_sender(steps):
    """A plain scripted sender: pops one step per call. A step is a reply
    dict or a callable(cmd_type, payload) -> reply. Records every call on
    .calls."""
    calls = []

    def sender(cmd_type, payload, timeout_ms=10000):
        calls.append({"type": cmd_type, "payload": payload})
        step = steps.pop(0)
        if callable(step):
            return step(cmd_type, payload)
        return step
    sender.calls = calls
    return sender


def _cap(lufs, track=None):
    """Capture responder for the scripted sender; `track` overrides the
    reply's track object (e.g. with a malformed non-dict shape)."""
    resp = _capture_responder(lufs=lufs, raw=f"LUFSI:{lufs:.2f}")

    def step(cmd_type, payload):
        reply = resp({"payload": payload})
        if track is not None:
            reply["data"]["track"] = track
        return reply
    return step


TUNE_SET_OK = {"ok": True, "type": "set_fx_param", "data": {"applied": True}}


def _run_tune(sender, scanner, tmp_path, delta=-3.0):
    return verifyloop.tune_param(
        sender, scanner, "Bass", {"fx_name_contains": "EQ"},
        {"param_index": 3}, {"metric": "lufs_i", "delta": delta},
        output_dir=str(tmp_path / "wavs"), _analyzer_loader=_no_analyzer)


def test_tune_malformed_capture_data_is_measure_failed_not_crash(tmp_path):
    # A wrong-shaped iteration capture `data` AFTER a successful set must
    # end as an honest MEASURE_FAILED with the read-back `final`, never a
    # crash that hides the applied mutation (review P1/P2, 2026-07-29).
    sender = _tune_sender([
        PREFLIGHT_OK, _context(), _cap(-14.0),           # baseline
        TUNE_SET_OK, PREFLIGHT_OK, {"ok": True, "data": "oops"},
    ])
    res = _run_tune(sender, _static_scanner(), tmp_path)
    assert res["status"] == "MEASURE_FAILED"
    assert res["final"]["read_back"] is True
    assert res["iterations"][-1] == {"normalized": 0.0, "metric": None}


def test_tune_malformed_capture_reply_runs_finish_and_discloses(tmp_path):
    # A capture reply whose track field is a bare string crashes past
    # measure's own guards mid-iteration; the tune-wide guard must route it
    # through finish() — restore policy evaluated, `final` read back, and
    # the applied mutation DISCLOSED — instead of the P1 crash escape that
    # produced no status, no restore, and no read-back (review, 2026-07-29).
    sender = _tune_sender([
        PREFLIGHT_OK, _context(), _cap(-14.0),           # baseline
        TUNE_SET_OK, PREFLIGHT_OK,
        _cap(-20.0, track="not-an-object"),              # malformed mid-run
    ])
    res = _run_tune(sender, _static_scanner(), tmp_path)
    assert res["status"] == "INTERNAL_ERROR"
    assert "AttributeError" in res["note"]
    assert "NOT rolled back" in res["note"]
    assert "0.0000" in res["note"]        # the applied value is disclosed
    assert res["iterations_used"] == 1
    assert res["final"]["read_back"] is True


def test_tune_finish_restore_skipped_when_identity_changed(tmp_path):
    # An FX-chain edit between the last iteration and finish(): the restore
    # set must be SKIPPED with the IDENTITY_CHANGED note appended, not
    # written to the retargeted FX (review P2).
    scan_calls = {"n": 0}

    def scanner(base):
        scan_calls["n"] += 1
        # calls 1-3: resolve + the two pre-set identity checks pass;
        # afterwards (pre-restore check, read-back) the FX was swapped.
        return _scan_data("{FX}" if scan_calls["n"] <= 3 else "{OTHER}"), None
    sender = _tune_sender([
        PREFLIGHT_OK, _context(), _cap(-14.0),           # baseline
        TUNE_SET_OK, PREFLIGHT_OK, _cap(-20.0),          # probe brackets goal
        TUNE_SET_OK, PREFLIGHT_OK, _cap(-10.0),          # midpoint escapes
    ])
    res = _run_tune(sender, scanner, tmp_path)
    assert res["status"] == "NON_MONOTONE"
    sets = [c for c in sender.calls if c["type"] == "set_fx_param"]
    assert len(sets) == 2                 # no restore write went out
    assert "SKIPPED (IDENTITY_CHANGED)" in res["note"]
    assert "different plugin" in res["note"]
    assert res["final"]["read_back"] is False


def test_tune_unreachable_flat_note_reports_both_probes(tmp_path):
    # First probe moves the metric AWAY, second boundary is flat: the note
    # must report both probes, not claim the parameter "does not move the
    # metric anywhere" while the iterations list shows a 5 dB move
    # (review P3, 2026-07-29).
    sender = _tune_sender([
        PREFLIGHT_OK, _context(), _cap(-14.0),           # baseline
        TUNE_SET_OK, PREFLIGHT_OK, _cap(-19.0),          # up probe: away
        TUNE_SET_OK, PREFLIGHT_OK, _cap(-14.0),          # down probe: flat
        TUNE_SET_OK,                                     # best-observed restore
    ])
    res = _run_tune(sender, _static_scanner(), tmp_path, delta=3.0)
    assert res["status"] == "UNREACHABLE"
    assert "up: -5.00" in res["note"]
    assert "down: +0.00" in res["note"]
    assert "anywhere in its range" not in res["note"]
