"""Tests for verifyloop.verify (SPEC_VERIFY_LOOP Phase 2).

All five verdict outcomes against the scripted fake bridge, the frozen-bounds
guarantee at the verify level, and the delta/verdict formatter on canned
metrics dicts. No live REAPER.
"""

import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reaperd  # noqa: E402
import verifyloop  # noqa: E402
from bridge_fakes import fake_bridge_script  # noqa: E402
from test_verifyloop import (  # noqa: E402
    write_wav, preflight_ok, context_reply, capture_reply, no_postmortem)


# Realistic mutation payload: the real bridge has NO implicit selected-track
# fallback, so a set_fx_param without an explicit track selector would be
# rejected NO_TARGET_TRACK — tests must not normalize an unusable shape.
MUT_PAYLOAD = {"target_track_name": "Bass", "param_index": 1,
               "normalized_value": 0.4}


def set_param_ok():
    return {"ok": True, "type": "set_fx_param",
            "data": {"normalized_value": 0.42, "formatted_value": "-3.00 dB"}}


# --- verdict outcomes -------------------------------------------------------

def test_verified_happy_path(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav1 = write_wav(str(tmp_path / "pre.wav"))
    wav2 = write_wav(str(tmp_path / "post.wav"))
    record = []
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav1, lufs=-14.1),
        set_param_ok(),
        preflight_ok(), capture_reply(wav2, lufs=-14.9),
    ], record=record)
    result = verifyloop.verify("Bass", "set_fx_param",
                               MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "VERIFIED"
    assert result["exit_code"] == 0
    assert result["mutation_applied"] is True
    assert result["deltas"]["lufs_i"] == {"pre": -14.1, "post": -14.9,
                                          "delta": -0.8}
    assert [c["type"] for c in record] == [
        "get_capture_preflight", "capture_track_audio", "set_fx_param",
        "get_capture_preflight", "capture_track_audio"]


def test_pre_measure_blocked_mutates_nothing(root):
    record = []
    fake_bridge_script(root, [
        {"ok": True, "type": "get_capture_preflight", "data": {
            "capture_allowed": False,
            "blockers": [{"code": "capture_gated", "message": "gated"}],
            "risk_gate": {"allow_risk_level_3": False,
                          "requires_restart_to_change": True}}},
    ], record=record)
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "PRE_MEASURE_BLOCKED"
    assert result["exit_code"] == 1
    assert result["mutation_applied"] is False
    time.sleep(0.1)
    assert all(c["type"] == "get_capture_preflight" for c in record)


def test_pre_measure_silent_mutates_nothing(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "pre.wav"))
    record = []
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav, lufs=-80.0)],
                       record=record)
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "PRE_MEASURE_SILENT"
    assert result["exit_code"] == 1
    assert result["mutation_applied"] is False
    assert "cmd" in result["message"]  # points at the unverified alternative
    time.sleep(0.1)
    assert all(c["type"] != "set_fx_param" for c in record)


def test_mutation_failed_no_post_capture(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "pre.wav"))
    record = []
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav),
        {"ok": False, "error": {"code": "NO_FX", "details": "no such fx"}},
    ], record=record)
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "MUTATION_FAILED"
    assert result["exit_code"] == 1
    assert result["mutation_applied"] is False
    assert result["error"]["code"] == "NO_FX"
    time.sleep(0.1)
    assert [c["type"] for c in record].count("capture_track_audio") == 1


def test_post_capture_failure_is_unverified_not_rolled_back(root, tmp_path,
                                                            monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "pre.wav"))
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav),
        set_param_ok(),
        preflight_ok(),
        {"ok": False, "error": {"code": "CAPTURE_FAILED", "details": "boom"}},
    ])
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "UNVERIFIED"
    assert result["exit_code"] == 2
    assert result["mutation_applied"] is True
    assert "NOT rolled back" in result["message"]
    assert "Ctrl/Cmd+Z" in result["message"]


def test_post_capture_silent_is_unverified(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav1 = write_wav(str(tmp_path / "pre.wav"))
    wav2 = write_wav(str(tmp_path / "post.wav"))
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav1, lufs=-14.0),
        set_param_ok(),
        preflight_ok(), capture_reply(wav2, lufs=-90.0),
    ])
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "UNVERIFIED"
    assert result["exit_code"] == 2
    assert result["mutation_applied"] is True


# --- bounds freezing at the verify level ------------------------------------

def test_pre_and_post_bounds_sent_byte_identical(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav1 = write_wav(str(tmp_path / "pre.wav"))
    wav2 = write_wav(str(tmp_path / "post.wav"))
    record = []
    fake_bridge_script(root, [
        preflight_ok(), context_reply(cursor=7.75),
        capture_reply(wav1),
        set_param_ok(),
        preflight_ok(), capture_reply(wav2),
    ], record=record)
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               bridge_root=root)
    assert result["verdict"] == "VERIFIED"
    captures = [c for c in record if c["type"] == "capture_track_audio"]
    assert len(captures) == 2
    pre_p, post_p = captures[0]["payload"], captures[1]["payload"]
    assert (json.dumps({k: pre_p[k] for k in ("start_seconds", "duration_seconds")},
                       sort_keys=True)
            == json.dumps({k: post_p[k] for k in ("start_seconds", "duration_seconds")},
                          sort_keys=True))
    # Bounds resolved exactly once: no second get_context.
    assert [c["type"] for c in record].count("get_context") == 1


# --- scope honesty ----------------------------------------------------------

def test_scope_change_between_measures_warns(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav1 = write_wav(str(tmp_path / "pre.wav"))
    wav2 = write_wav(str(tmp_path / "post.wav"))
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav1, scope="isolated_track", verified=True),
        set_param_ok(),
        preflight_ok(), capture_reply(wav2, scope="full_mix", verified=False),
    ])
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "VERIFIED"
    assert any("CHANGED" in w for w in result["warnings"])


def test_unisolated_but_stable_scope_warns_of_scope(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav1 = write_wav(str(tmp_path / "pre.wav"))
    wav2 = write_wav(str(tmp_path / "post.wav"))
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav1, scope="full_mix", verified=False),
        set_param_ok(),
        preflight_ok(), capture_reply(wav2, scope="full_mix", verified=False),
    ])
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert any("full_mix" in w and "not necessarily this track alone" in w
               for w in result["warnings"])


# --- delta computation and formatter (canned dicts, no bridge) ---------------

def canned(source="postmortem", lufs=-14.0, rms=-18.0, bands=None, stereo=None):
    metrics = {"lufs_i": lufs}
    if source == "postmortem":
        metrics.update({"sample_peak_db": -3.0, "rms_db": rms,
                        "crest_factor_db": 15.0, "silence_fraction": 0.0,
                        "spectrum_third_octave": bands or [],
                        "stereo": stereo})
    return {"ok": True, "metrics_source": source, "metrics": metrics,
            "capture": {"capture_scope": "isolated_track",
                        "isolation_verified": True},
            "bounds": {"start_seconds": 0, "duration_seconds": 10,
                       "source": "explicit"},
            "silent": False, "warnings": []}


def test_delta_metrics_band_matching_and_missing_bands():
    pre = canned(bands=[{"freq_hz": 100, "level_db": -20.0},
                        {"freq_hz": 200, "level_db": -22.0},
                        {"freq_hz": 20000, "level_db": -60.0}])
    post = canned(bands=[{"freq_hz": 100, "level_db": -24.5},
                         {"freq_hz": 200, "level_db": -22.0}])
    deltas = verifyloop._delta_metrics(pre, post)
    bands = {b["freq_hz"]: b for b in deltas["spectrum_third_octave"]}
    assert bands[100]["delta"] == -4.5
    assert bands[200]["delta"] == 0.0
    assert 20000 not in bands  # present on one side only: never guessed


def test_delta_metrics_mixed_sources_compares_only_common():
    pre = canned(source="postmortem")
    post = canned(source="render_stats", lufs=-15.5)
    deltas = verifyloop._delta_metrics(pre, post)
    assert deltas["lufs_i"]["delta"] == -1.5
    assert "rms_db" not in deltas
    assert "spectrum_third_octave" not in deltas


def test_delta_metrics_none_lufs_is_absent_not_zero():
    pre = canned(source="render_stats", lufs=None)
    post = canned(source="render_stats", lufs=-14.0)
    assert "lufs_i" not in verifyloop._delta_metrics(pre, post)


def test_format_verify_states_mutation_and_deltas():
    result = {"ok": True, "verdict": "VERIFIED", "exit_code": 0,
              "mutation_applied": True,
              "deltas": {"lufs_i": {"pre": -14.1, "post": -14.9, "delta": -0.8},
                         "spectrum_third_octave": [
                             {"freq_hz": 315, "pre": -20.0, "post": -23.1,
                              "delta": -3.1},
                             {"freq_hz": 1000, "pre": -30.0, "post": -30.2,
                              "delta": -0.2}]},
              "warnings": ["scope note"]}
    text = verifyloop.format_verify(result)
    assert "VERIFIED" in text and "mutation applied: yes" in text
    assert "-14.1 -> -14.9" in text
    assert "315" in text          # moved >= 1 dB: shown
    assert "1000" not in text     # under the display threshold: not shown
    assert "WARNING: scope note" in text


def test_format_verify_unverified_states_not_rolled_back():
    result = {"ok": False, "verdict": "UNVERIFIED", "exit_code": 2,
              "mutation_applied": True,
              "message": f"unproven. {verifyloop.UNVERIFIED_NOTE}",
              "error": {"code": "CAPTURE_FAILED", "details": "boom"}}
    text = verifyloop.format_verify(result)
    assert "UNVERIFIED" in text
    assert "NOT rolled back" in text
    assert "CAPTURE_FAILED" in text


# --- CLI wiring --------------------------------------------------------------

def test_cmd_verify_argument_errors(root, capsys):
    assert reaperd.main(["--bridge-root", root, "verify", "Bass"]) == 1
    assert "needs a mutation" in capsys.readouterr().err
    assert reaperd.main(["--bridge-root", root, "verify", "Bass",
                         "--", "set_fx_param", "{not json"]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_cmd_verify_exit_code_passthrough(root, tmp_path, monkeypatch, capsys):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "pre.wav"))
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav),
        {"ok": False, "error": {"code": "NO_FX", "details": "nope"}},
    ])
    # Options after the track name must survive the `--` split in main().
    rc = reaperd.main(["--bridge-root", root, "verify", "Bass",
                       "--start", "0", "--json",
                       "--", "set_fx_param", json.dumps(MUT_PAYLOAD)])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "MUTATION_FAILED"


# --- gate findings: mutation-state honesty ----------------------------------

def test_bad_payload_refused_before_any_capture(root):
    record = []
    fake_bridge_script(root, [], record=record)
    result = verifyloop.verify("Bass", "set_fx_param", [1, 2, 3],
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "BAD_PAYLOAD"
    assert result["exit_code"] == 1
    assert result["mutation_applied"] is False
    time.sleep(0.1)
    assert record == []  # not even a preflight was sent


def test_unmeasurable_pre_refuses_verdict(root, tmp_path, monkeypatch):
    # RENDER_STATS gave no LUFS and Post Mortem is absent: silence cannot be
    # assessed, so no verdict may be grounded — and nothing may be mutated.
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "pre.wav"))
    record = []
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav, lufs=None)],
                       record=record)
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "PRE_MEASURE_UNMEASURABLE"
    assert result["exit_code"] == 1
    assert result["mutation_applied"] is False
    time.sleep(0.1)
    assert all(c["type"] != "set_fx_param" for c in record)


def test_unmeasurable_post_is_unverified(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav1 = write_wav(str(tmp_path / "pre.wav"))
    wav2 = write_wav(str(tmp_path / "post.wav"))
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav1, lufs=-14.0),
        set_param_ok(),
        preflight_ok(), capture_reply(wav2, lufs=None),
    ])
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "UNVERIFIED"
    assert result["exit_code"] == 2
    assert result["mutation_applied"] is True
    assert "could not be assessed" in result["message"]


def test_mutation_timeout_is_unknown_not_denied(root, tmp_path, monkeypatch):
    # The bridge moves inbox -> processing before executing, so a timed-out
    # mutation may still run: state is UNKNOWN, never "not applied".
    no_postmortem(monkeypatch)
    monkeypatch.setattr(verifyloop, "MUTATION_TIMEOUT_MS", 300)
    wav = write_wav(str(tmp_path / "pre.wav"))
    fake_bridge_script(root, [preflight_ok(), capture_reply(wav)])
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "MUTATION_UNKNOWN"
    assert result["exit_code"] == 2
    assert result["mutation_applied"] is None
    assert "do NOT blindly resend" in result["message"]
    text = verifyloop.format_verify(result)
    assert "mutation applied: UNKNOWN" in text


def test_batch_failure_is_partial_not_nothing(root, tmp_path, monkeypatch):
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "pre.wav"))
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav),
        {"ok": False, "error": {"code": "NO_FX", "details": "cmd 3 of 5"}},
    ])
    result = verifyloop.verify("Bass", "batch",
                               {"commands": [], "stop_on_error": True},
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "MUTATION_FAILED"
    assert result["exit_code"] == 2  # the project may well be mutated
    assert result["mutation_applied"] is None
    assert "ARE applied" in result["message"]


def test_verify_usage_error_never_exits_2(root):
    with pytest.raises(SystemExit) as e:
        reaperd.main(["--bridge-root", root, "verify", "Bass", "--secondz", "5",
                      "--", "set_fx_param", "{}"])
    assert e.value.code == 64  # EX_USAGE, never the UNVERIFIED verdict


def test_usage_error_remap_covers_all_subcommands(root):
    # exit 2 is meaningful for discover-map too (incomplete map); a usage
    # error must never collide with any semantic exit code.
    with pytest.raises(SystemExit) as e:
        reaperd.main(["--bridge-root", root, "cmd"])  # missing required args
    assert e.value.code == 64


def test_bridge_root_abbreviation_still_verify(root, tmp_path, monkeypatch,
                                               capsys):
    # argparse accepts --bridge/--b as abbreviations of --bridge-root; the
    # verify detection (and so the '--' split) must survive them.
    assert reaperd._subcommand(["--bridge", "/x", "verify", "T"]) == "verify"
    assert reaperd._subcommand(["--bridge-root=/x", "verify", "T"]) == "verify"
    assert reaperd._subcommand(["--b", "/x", "cmd", "t", "{}"]) == "cmd"
    assert reaperd._subcommand(["send", "--", "f.json"]) == "send"
    no_postmortem(monkeypatch)
    wav = write_wav(str(tmp_path / "pre.wav"))
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav),
        {"ok": False, "error": {"code": "NO_FX", "details": "nope"}},
    ])
    rc = reaperd.main(["--bridge", root, "verify", "Bass", "--start", "0",
                       "--json", "--", "set_fx_param", json.dumps(MUT_PAYLOAD)])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "MUTATION_FAILED"


def test_verified_never_carries_empty_deltas(root, tmp_path):
    # Mixed modes with asymmetric LUFS: pre degrades to render_stats (bad WAV,
    # LUFS present -> assessable), post analyzes fine but has no LUFS. No
    # metric spans both sides -> zero evidence -> must NOT be VERIFIED.
    pytest.importorskip("postmortem")
    bad = str(tmp_path / "bad.wav")
    with open(bad, "wb") as f:
        f.write(b"not a wav")
    good = write_wav(str(tmp_path / "good.wav"))
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(bad, lufs=-14.0),
        set_param_ok(),
        preflight_ok(), capture_reply(good, lufs=None),
    ])
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "UNVERIFIED"
    assert result["exit_code"] == 2
    assert result["mutation_applied"] is True
    assert "no metric was comparable across both sides" in result["message"]


def test_dashdash_stays_native_for_other_subcommands(root):
    from bridge_fakes import fake_bridge
    fake_bridge(root, {"ok": True, "type": "get_context", "data": {}})
    # Standard argparse idiom: `--` ends option parsing. Before the fix the
    # global split discarded the tail and cmd exited 2 on missing arguments.
    rc = reaperd.main(["--bridge-root", root, "cmd", "--", "get_context", "{}"])
    assert rc == 0


# --- gate findings: comparability and warning bubbling -----------------------

def test_capture_mismatch_restricts_deltas_to_lufs():
    pre = canned()
    post = canned(lufs=-15.0)
    pre["metrics"].update({"duration_seconds": 10.0, "sample_rate": 48000,
                           "channels": 2})
    post["metrics"].update({"duration_seconds": 4.2, "sample_rate": 48000,
                            "channels": 2})
    mismatch = verifyloop._capture_mismatch(pre, post)
    assert mismatch and "duration_seconds" in mismatch
    deltas = verifyloop._delta_metrics(pre, post, content_comparable=False)
    assert "lufs_i" in deltas
    assert "rms_db" not in deltas and "spectrum_third_octave" not in deltas


def test_degraded_analysis_warnings_bubble_to_verify_report(root, tmp_path):
    pytest.importorskip("postmortem")
    bad1, bad2 = str(tmp_path / "b1.wav"), str(tmp_path / "b2.wav")
    for p in (bad1, bad2):
        with open(p, "wb") as f:
            f.write(b"not a wav")
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(bad1, lufs=-14.0),
        set_param_ok(),
        preflight_ok(), capture_reply(bad2, lufs=-14.6),
    ])
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "VERIFIED"  # LUFS basis still held
    assert any(w.startswith("pre-measure: Post Mortem analysis")
               for w in result["warnings"])
    assert "WARNING" in verifyloop.format_verify(result)


def test_verified_postmortem_end_to_end_spectrum_deltas(root, tmp_path):
    pytest.importorskip("postmortem")
    wav1 = write_wav(str(tmp_path / "pre.wav"), amp=0.5)
    wav2 = write_wav(str(tmp_path / "post.wav"), amp=0.25)
    fake_bridge_script(root, [
        preflight_ok(), capture_reply(wav1, lufs=-14.0),
        set_param_ok(),
        preflight_ok(), capture_reply(wav2, lufs=-20.0),
    ])
    result = verifyloop.verify("Bass", "set_fx_param", MUT_PAYLOAD,
                               start=0.0, bridge_root=root)
    assert result["verdict"] == "VERIFIED"
    assert result["deltas"]["lufs_i"]["delta"] == -6.0
    # Halving amplitude: RMS drops ~6 dB, and the 440 Hz band moves with it.
    assert abs(result["deltas"]["rms_db"]["delta"] + 6.0) < 0.5
    moved = [b for b in result["deltas"]["spectrum_third_octave"]
             if abs(b["delta"]) >= 1.0]
    assert moved
