"""Optional local drum transcription jobs. The CLI and MCP share this adapter.

Importing this module needs only the standard library. Model imports and work
happen in a separate, explicitly configured Python environment.
"""
from pathlib import Path
import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
REQUIRED_ROLES = ("KICK_R", "SNARE", "TOM_1", "TOM_2", "TOM_3", "TOM_4",
                  "HH_OPEN_1", "CRASH_L", "CRASH_R")
LIMITATIONS = ["Automatic transcription; musical accuracy needs listening.",
               "Tom pitches, cymbal types and hat openness are estimates.",
               "Analyzes source audio before item fades/gain and track FX."]


def write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temp, path)


def jobs_dir(root):
    return Path(root).resolve() / "state" / "drum-transcription"


def job_dir(root, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"drums-[0-9a-f]{24}", job_id):
        raise ValueError("Invalid transcription job ID")
    return jobs_dir(root) / job_id


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_identity(path):
    p = Path(path)
    before = p.stat()
    h = hashlib.sha256()
    with p.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    after = p.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Source file changed while it was being read")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": h.hexdigest()}


def _bridge(send, kind, payload):
    result = send(kind, payload)
    if not result.get("ok"):
        raise ValueError(f"{kind}: {result.get('error')}")
    return result["data"]


def runtime_python(root):
    configured = jobs_dir(root) / "runtime.json"
    value = os.environ.get("REAPER_TRANSCRIPTION_PYTHON")
    if not value and configured.exists():
        value = _read(configured).get("python")
    if not value:
        value = str(Path(root) / ".venvs" / "drum-transcription" /
                    ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError("Transcription runtime missing. Run setup/install_transcription.py "
                         "with Python 3.11, or transcribe configure --python PATH.")
    return str(path)


def check_runtime(python):
    probe = subprocess.run([str(python), str(ROOT / "transcription_worker.py"), "--check"],
                           capture_output=True, text=True, timeout=60,
                           **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    if probe.returncode:
        raise ValueError("Transcription dependencies unavailable: " + (probe.stderr or probe.stdout)[-2000:])
    return json.loads(probe.stdout)


def configure(root, python):
    path = str(Path(python).expanduser().resolve())
    details = check_runtime(path)
    folder = jobs_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "runtime.json", {"python": path})
    return {"python": path, "dependencies": details}


def validate_source(source):
    for field in ("project_token", "track_guid", "item_guid", "take_guid", "source_file"):
        if not isinstance(source.get(field), str) or not source[field]:
            raise ValueError(f"Source snapshot is missing {field}; reload the updated bridge")
    for field in ("position", "source_offset", "length"):
        value = source.get(field)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid source {field}")
    if not 0 < source["length"] <= 600 or source.get("playrate") != 1 or source.get("pitch") != 0:
        raise ValueError("Use up to 600 seconds of audio at original speed and pitch")
    if not Path(source["source_file"]).is_file():
        raise ValueError("Source audio is not readable from the transcription process")


def start(root, send, *, source_track=None, item_index=None, source_item_guid=None,
          separate=True, device="cpu", threads=4):
    for selector in (source_track, source_item_guid):
        if selector is not None and (not isinstance(selector, str) or not selector.strip()):
            raise ValueError("Source selectors must be nonempty strings")
    if source_track is not None and source_item_guid is not None:
        raise ValueError("Choose source_track or source_item_guid, not both")
    if type(separate) is not bool or device not in ("cpu", "cuda") or type(threads) is not int or not 1 <= threads <= 16:
        raise ValueError("Use separate=true/false, device=cpu/cuda, and 1..16 threads")
    python = runtime_python(root)
    check_runtime(python)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("ffmpeg must be on PATH to decode the source audio")
    payload = {}
    if source_item_guid:
        payload["item_guid"] = source_item_guid
    elif source_track:
        payload["target_track_name"] = source_track
    if item_index is not None:
        if type(item_index) is not int or item_index < 0 or not source_track or source_item_guid:
            raise ValueError("item_index requires source_track and must be a nonnegative integer")
        payload["item_index"] = item_index
    source = _bridge(send, "get_transcription_source", payload)
    validate_source(source)
    identity = file_identity(source["source_file"])
    job_id = "drums-" + secrets.token_hex(12)
    folder = job_dir(root, job_id)
    folder.mkdir(parents=True, exist_ok=False)
    manifest = {"format": "reaper-drum-transcription-v1", "job_id": job_id,
                "source": source, "source_file_identity": identity, "separate": separate,
                "device": device, "threads": threads, "ffmpeg": ffmpeg,
                "created_at": time.time()}
    write_json(folder / "manifest.json", manifest)
    write_json(folder / "status.json", {"job_id": job_id, "state": "queued", "stage": "starting",
                                        "updated_at": time.time(), "limitations": LIMITATIONS})
    try:
        with (folder / "worker.log").open("ab") as log:
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen([python, str(ROOT / "transcription_worker.py"), str(folder)], **kwargs)
        write_json(folder / "process.json", {"pid": proc.pid})
    except OSError as exc:
        write_json(folder / "status.json", {"job_id": job_id, "state": "failed", "error": str(exc), "updated_at": time.time()})
        raise
    return status(root, job_id)


def status(root, job_id):
    folder = job_dir(root, job_id)
    data = _read(folder / "status.json")
    if data["state"] in ("queued", "running") and time.time() - data["updated_at"] > 180:
        data["state"] = "stalled"
        data["error"] = "Worker heartbeat is stale; no completed result is available. See worker.log."
    data["directory"] = str(folder)
    if (folder / "receipt.json").exists():
        data["insertion"] = _read(folder / "receipt.json")
    return data


def _kit_map(root, send, track_guid, map_name):
    sys.path.insert(0, str(ROOT / "skills" / "drum-apparatus"))
    from drumgen.catalog import load_maps
    from drumgen.mapdetect import match_roles
    discovery = _bridge(send, "discover_drum_map", {"target_track_guid": track_guid})
    raw_names = discovery.get("notes") or {}
    if not isinstance(raw_names, dict):
        raise ValueError("Invalid note-name discovery reply")
    names = {int(k): v["name"] if isinstance(v, dict) else v for k, v in raw_names.items()}
    maps = load_maps()
    if map_name:
        if map_name not in maps:
            raise ValueError("Unknown kit map: " + map_name)
        kit = maps[map_name]
    else:
        # Exact named-note matches beat fuzzy classification for known kits:
        # Monarch keeps its hand-built map and the rimshot snare voicing below.
        if all(names.get(p) == name for p, name in {24: "Kick", 28: "Snare Rimshot", 38: "Rack 1", 45: "Small Open", 54: "Right Crash"}.items()):
            map_name, kit = "RS Monarch", maps["RS Monarch"]
        else:
            kit, _ = match_roles(names)
            map_name = "Live note names"
    missing = [role for role in REQUIRED_ROLES if role not in kit]
    if missing:
        raise ValueError("Incomplete drum map; choose --map. Missing: " + ", ".join(missing))
    if any(type(kit[role]) is not int or not 0 <= kit[role] <= 127 for role in REQUIRED_ROLES):
        raise ValueError("Kit map contains an invalid MIDI pitch")
    # Frozen names are rechecked atomically by the bridge when it inserts.
    return dict(kit), map_name, {str(k): v for k, v in names.items()}


def map_events(rows, kit, map_name, duration):
    sys.path.insert(0, str(ROOT / "skills" / "drum-apparatus"))
    from drumgen.goldenrule import enforce, violations
    if not rows or len(rows) > 10000:
        raise ValueError("Transcription must contain 1..10000 detected hits")
    events = []
    for row in rows:
        t = row["time"]
        role = row["role"]
        if isinstance(t, bool) or not isinstance(t, (float, int)) or not math.isfinite(t) or not 0 <= t < duration:
            raise ValueError("Detected hit lies outside the source item")
        if role not in REQUIRED_ROLES or type(row["velocity"]) is not int or not 1 <= row["velocity"] <= 127:
            raise ValueError("Invalid detected drum role or velocity")
        if role == "SNARE" and map_name == "RS Monarch":
            role = "SNARE_RIM"
        events.append({"type": "note", "time": t, "duration": min(.03, duration-t),
                       "channel": 0, "pitch": kit[role], "velocity": row["velocity"]})
    events.sort(key=lambda e: (e["time"], e["pitch"]))
    # Sparse kits can map several tom roles onto one pitch. Merge coincident
    # aliases before enforcing per-drum velocities and writing note events.
    merged = []
    last = {}
    for e in events:
        previous = last.get(e["pitch"])
        if previous is not None and e["time"] - previous["time"] < .01:
            previous["velocity"] = max(previous["velocity"], e["velocity"])
        else:
            merged.append(e)
            last[e["pitch"]] = e
    notes = [{"index": i, "ppq": e["time"] * 960, "pitch": e["pitch"], "velocity": e["velocity"]} for i, e in enumerate(merged)]
    velocities = enforce(notes, {n["index"]: n["velocity"] for n in notes}, min_gap=3)
    if violations(notes, velocities):
        raise ValueError("Velocity verification failed")
    for i, event in enumerate(merged):
        event["velocity"] = velocities[i]
    return merged


def insert(root, send, job_id, *, track, map_name=None, dry_run=False):
    if not isinstance(track, str) or not track.strip():
        raise ValueError("Choose a destination track by name")
    folder = job_dir(root, job_id)
    if status(root, job_id)["state"] != "completed":
        raise ValueError("Transcription is not complete")
    if (folder / "receipt.json").exists() or (folder / "insert-attempt.json").exists():
        raise ValueError("Insertion was already attempted. Inspect the saved receipt and live item before retrying; do not duplicate notes.")
    manifest = _read(folder / "manifest.json")
    if file_identity(manifest["source"]["source_file"]) != manifest["source_file_identity"]:
        raise ValueError("Source audio file changed after transcription")
    context = _bridge(send, "get_context", {})
    matches = [t for t in context["tracks"] if t.get("name", "").casefold() == track.casefold() and not t.get("is_master")]
    if len(matches) != 1:
        raise ValueError("Choose one uniquely named destination track")
    guid = matches[0]["guid"]
    kit, chosen, note_names = _kit_map(root, send, guid, map_name)
    rows = _read(folder / "hits.json")
    events = map_events(rows, kit, chosen, manifest["source"]["length"])
    payload = {"source": manifest["source"], "job_id": job_id, "target_track_guid": guid,
               "events": events, "expected_note_names": note_names, "dry_run": bool(dry_run)}
    # Validate native source/target guards even for a preview.
    validation = _bridge(send, "insert_drum_transcription", {**payload, "validate_only": True})
    if dry_run:
        return {"job_id": job_id, "dry_run": True, "notes": len(events), "kit_map": chosen, "validation": validation}
    lock = folder / "insert-attempt.json"
    with lock.open("x", encoding="utf-8") as stream:
        json.dump({"target_track_guid": guid, "started_at": time.time()}, stream)
    # A timeout may already have inserted notes. Keep the attempt marker even on
    # failure, so another client cannot blindly retry an ambiguous write.
    result = send("insert_drum_transcription", payload)
    receipt = {"job_id": job_id, "kit_map": chosen, "target_track_guid": guid, "reply": result}
    write_json(folder / "receipt.json", receipt)
    if not result.get("ok"):
        raise ValueError(f"Insertion failed or is unverified; inspect receipt.json: {result.get('error')}")
    return {"job_id": job_id, "kit_map": chosen, **result["data"], "limitations": LIMITATIONS}


def add_parser(sub):
    parser = sub.add_parser("transcribe", help="transcribe an audio item into drum MIDI")
    actions = parser.add_subparsers(dest="transcribe_action", required=True)
    p = actions.add_parser("configure", help="use an existing transcription Python environment")
    p.add_argument("--python", required=True)
    p = actions.add_parser("start", help="analyze one audio item in a background worker")
    p.add_argument("--source-track")
    p.add_argument("--source-item-guid")
    p.add_argument("--item-index", type=int)
    p.add_argument("--drums-only", action="store_true", help="skip separation for an isolated drum recording")
    p.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    p.add_argument("--threads", type=int, default=4)
    p = actions.add_parser("status", help="read progress and output paths")
    p.add_argument("job_id")
    p = actions.add_parser("insert", help="insert a completed job on a drum track")
    p.add_argument("job_id")
    p.add_argument("--track", required=True)
    p.add_argument("--map", dest="map_name")
    p.add_argument("--dry-run", action="store_true")
    parser.set_defaults(func=cli)


def cli(args):
    import reaperd
    send = lambda kind, payload: reaperd.send_type(kind, payload, bridge_root=args.bridge_root,
                                                  timeout_ms=60000, verbose=False)
    try:
        action = args.transcribe_action
        if action == "configure":
            data = configure(args.bridge_root, args.python)
        elif action == "start":
            data = start(args.bridge_root, send, source_track=args.source_track, item_index=args.item_index,
                         source_item_guid=args.source_item_guid, separate=not args.drums_only,
                         device=args.device, threads=args.threads)
        elif action == "status":
            data = status(args.bridge_root, args.job_id)
        else:
            data = insert(args.bridge_root, send, args.job_id, track=args.track, map_name=args.map_name, dry_run=args.dry_run)
        print(json.dumps({"ok": True, "data": data}, indent=2))
        return 1 if data.get("state") in ("failed", "stalled") else 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
