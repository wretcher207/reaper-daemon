import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import drum_transcription as dt
import reaperd
import reaper_mcp as mcp

JOB = "drums-" + "a" * 24
GM = dict(zip(dt.REQUIRED_ROLES, [36, 38, 48, 47, 45, 43, 46, 49, 57]))


def source(tmp_path):
    audio = tmp_path / "reference.wav"
    audio.write_bytes(b"audio contents")
    return dict(project_token="project-1", project_path="", track_guid="source", item_guid="item",
                take_guid="take", source_file=str(audio), source_type="WAVE", position=12.5,
                source_offset=.8, length=2, playrate=1, pitch=0)


def ready(tmp_path):
    folder = dt.job_dir(tmp_path, JOB)
    folder.mkdir(parents=True)
    src = source(tmp_path)
    dt.write_json(folder / "manifest.json", {"source": src, "source_file_identity": dt.file_identity(src["source_file"])})
    dt.write_json(folder / "status.json", {"state": "completed", "job_id": JOB, "updated_at": time.time()})
    dt.write_json(folder / "hits.json", [{"time": .1, "role": "KICK_R", "velocity": 100},
                                        {"time": .2, "role": "KICK_R", "velocity": 100}])
    return folder, src


def fake_send(calls, refusal=None):
    def send(kind, payload):
        calls.append((kind, payload))
        if kind == "get_context":
            return {"ok": True, "data": {"tracks": [{"name": "Drums", "guid": "target"}]}}
        if refusal:
            return {"ok": False, "error": {"code": refusal}}
        return {"ok": True, "data": {"verified": True, "notes": len(payload["events"])}}
    return send


@pytest.mark.parametrize("name", ["../oops", "drums-", "x" * 100, None, "drums-" + "z" * 24])
def test_job_path_cannot_escape(tmp_path, name):
    with pytest.raises(ValueError):
        dt.job_dir(tmp_path, name)


@pytest.mark.parametrize("field,value", [("position", float("nan")), ("length", 601), ("length", 0),
                                       ("source_offset", -1), ("playrate", 2), ("pitch", 1), ("item_guid", "")])
def test_source_validation(tmp_path, field, value):
    src = source(tmp_path)
    src[field] = value
    with pytest.raises(ValueError):
        dt.validate_source(src)


def test_source_keeps_trim_and_session_position(tmp_path):
    src = source(tmp_path)
    dt.validate_source(src)
    assert src["position"] == 12.5 and src["source_offset"] == .8


@pytest.mark.parametrize("kwargs", [{"source_track": ""}, {"source_item_guid": False},
                                    {"source_track": "A", "source_item_guid": "B"}])
def test_ambiguous_source_selectors_fail_before_launch(tmp_path, kwargs):
    with pytest.raises(ValueError):
        dt.start(tmp_path, lambda *a: pytest.fail("No bridge call expected"), **kwargs)


def test_velocity_rule_spans_other_drums_and_preserves_timing():
    rows = [{"time": .123, "role": "KICK_R", "velocity": 100},
            {"time": .15, "role": "SNARE", "velocity": 98},
            {"time": .234, "role": "KICK_R", "velocity": 100}]
    events = dt.map_events(rows, GM, "GM", 2)
    assert [e["time"] for e in events] == [.123, .15, .234]
    assert events[0]["velocity"] != events[2]["velocity"]


def test_sparse_map_merges_coincident_tom_aliases():
    kit = {**GM, "TOM_2": GM["TOM_1"]}
    events = dt.map_events([{"time": .1, "role": "TOM_1", "velocity": 90},
                            {"time": .1, "role": "TOM_2", "velocity": 100}], kit, "test", 2)
    assert len(events) == 1 and events[0]["velocity"] == 100


def test_monarch_uses_rimshot_not_side_stick():
    kit = {**GM, "SNARE_RIM": 28}
    events = dt.map_events([{"time": .1, "role": "SNARE", "velocity": 100}], kit, "RS Monarch", 2)
    assert events[0]["pitch"] == 28


@pytest.mark.parametrize("row", [{"time": float("nan"), "role": "KICK_R", "velocity": 100},
                                {"time": 2, "role": "KICK_R", "velocity": 100},
                                {"time": .1, "role": "UNKNOWN", "velocity": 100},
                                {"time": .1, "role": "SNARE", "velocity": 128}])
def test_bad_hits_never_reach_bridge(row):
    with pytest.raises(ValueError):
        dt.map_events([row], GM, "GM", 2)


def test_silent_transcription_is_not_success():
    with pytest.raises(ValueError):
        dt.map_events([], GM, "GM", 2)


def test_insert_dry_run_calls_native_guards_without_attempt_marker(tmp_path, monkeypatch):
    folder, src = ready(tmp_path)
    monkeypatch.setattr(dt, "_kit_map", lambda *a: (GM, "GM", {}))
    calls = []
    result = dt.insert(tmp_path, fake_send(calls), JOB, track="Drums", dry_run=True)
    assert result["dry_run"]
    assert calls[-1][1]["validate_only"]
    assert calls[-1][1]["source"] == src
    assert not (folder / "insert-attempt.json").exists()


def test_source_file_changes_refused_before_bridge(tmp_path):
    _, src = ready(tmp_path)
    Path(src["source_file"]).write_bytes(b"different audio")
    calls = []
    with pytest.raises(ValueError, match="changed"):
        dt.insert(tmp_path, fake_send(calls), JOB, track="Drums")
    assert not calls


def test_insertion_preflight_failure_allows_target_correction(tmp_path, monkeypatch):
    folder, _ = ready(tmp_path)
    monkeypatch.setattr(dt, "_kit_map", lambda *a: (GM, "GM", {}))
    with pytest.raises(ValueError, match="RANGE_OCCUPIED"):
        dt.insert(tmp_path, fake_send([], "RANGE_OCCUPIED"), JOB, track="Drums")
    assert not (folder / "insert-attempt.json").exists()


def test_success_records_receipt_and_refuses_duplicate(tmp_path, monkeypatch):
    folder, _ = ready(tmp_path)
    monkeypatch.setattr(dt, "_kit_map", lambda *a: (GM, "GM", {}))
    calls = []
    assert dt.insert(tmp_path, fake_send(calls), JOB, track="Drums")["verified"]
    assert calls[-1][1].get("validate_only") is None
    assert (folder / "receipt.json").exists()
    with pytest.raises(ValueError, match="already attempted"):
        dt.insert(tmp_path, fake_send(calls), JOB, track="Drums")


def test_lost_write_reply_cannot_be_blindly_retried(tmp_path, monkeypatch):
    folder, _ = ready(tmp_path)
    monkeypatch.setattr(dt, "_kit_map", lambda *a: (GM, "GM", {}))
    normal = fake_send([])
    def send(kind, payload):
        if kind == "insert_drum_transcription" and not payload.get("validate_only"):
            raise TimeoutError("possibly inserted")
        return normal(kind, payload)
    with pytest.raises(TimeoutError):
        dt.insert(tmp_path, send, JOB, track="Drums")
    assert (folder / "insert-attempt.json").exists()
    with pytest.raises(ValueError, match="already attempted"):
        dt.insert(tmp_path, send, JOB, track="Drums")


def test_stale_worker_is_visible(tmp_path):
    folder, _ = ready(tmp_path)
    dt.write_json(folder / "status.json", {"state": "running", "updated_at": time.time()-200})
    assert dt.status(tmp_path, JOB)["state"] == "stalled"


def test_start_launches_worker_without_shell_and_preserves_snapshot(tmp_path, monkeypatch):
    src = source(tmp_path)
    monkeypatch.setattr(dt, "runtime_python", lambda _: "/runtime/python")
    monkeypatch.setattr(dt, "check_runtime", lambda _: {})
    monkeypatch.setattr(dt.shutil, "which", lambda _: "/bin/ffmpeg")
    launches = []
    monkeypatch.setattr(dt.subprocess, "Popen", lambda cmd, **kw: launches.append((cmd, kw)) or SimpleNamespace(pid=123))
    calls = []
    def send(kind, payload):
        calls.append((kind, payload))
        return {"ok": True, "data": src}
    data = dt.start(tmp_path, send, source_track="Reference", item_index=1)
    assert calls == [("get_transcription_source", {"target_track_name": "Reference", "item_index": 1})]
    assert data["state"] == "queued"
    assert launches[0][0][0] == "/runtime/python"
    assert not launches[0][1].get("shell")
    manifest = json.loads((Path(data["directory"]) / "manifest.json").read_text())
    assert manifest["source"] == src and manifest["separate"] is True


def test_cli_and_mcp_share_same_start(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(dt, "start", lambda root, send, **kwargs: calls.append(kwargs) or {"state": "queued"})
    args = reaperd.build_parser().parse_args(["--bridge-root", str(tmp_path), "transcribe", "start", "--source-track", "Song"])
    assert args.func(args) == 0
    reply = mcp.tool_transcribe_drums({"source_track": "Song"})
    assert "isError" not in reply
    assert calls[0] == calls[1]


def test_mcp_status_and_insert_share_implementation(monkeypatch):
    calls = []
    monkeypatch.setattr(dt, "status", lambda root, job: {"state": "completed", "job_id": job})
    monkeypatch.setattr(dt, "insert", lambda root, send, job, **kw: calls.append((job, kw)) or {"verified": True})
    assert "completed" in mcp.tool_get_drum_transcription({"job_id": JOB})["content"][0]["text"]
    mcp.tool_insert_drum_transcription({"job_id": JOB, "track": "Monarch", "dry_run": True})
    assert calls == [(JOB, {"track": "Monarch", "map_name": None, "dry_run": True})]


def test_skill_path_no_longer_references_parser(tmp_path):
    assert reaperd._skill_path(str(tmp_path)) == str(tmp_path / "skills" / "drum-apparatus")


def test_explicit_map_accepts_lua_empty_note_array(tmp_path):
    kit, name, names = dt._kit_map(tmp_path, lambda *a: {"ok": True, "data": {"notes": []}}, "track", "RS Monarch")
    assert name == "RS Monarch" and kit["KICK_R"] == 24 and names == {}
