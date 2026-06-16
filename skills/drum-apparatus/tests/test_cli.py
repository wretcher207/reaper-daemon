import json, subprocess, sys
from pathlib import Path
from drumgen.smf import parse_smf

ROOT = Path(__file__).resolve().parent.parent


def test_cli_single_groove(tmp_path):
    out = tmp_path / "g.mid"
    r = subprocess.run([sys.executable, str(ROOT / "generate.py"),
                        "--groove", "Tech Death Pulse", "--bars", "4",
                        "--out", str(out), "--seed", "3"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    assert out.exists()
    assert "notes" in r.stdout
    notes = parse_smf(out.read_bytes())["notes"]
    assert len(notes) > 0


def test_cli_arrangement_spec(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"sections": [
        {"groove": "Hammer Blast", "bars": 2, "fill": True},
        {"groove": "The Pit Opener", "bars": 2}]}))
    out = tmp_path / "arr.mid"
    r = subprocess.run([sys.executable, str(ROOT / "generate.py"),
                        "--spec", str(spec), "--out", str(out), "--seed", "3"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    assert out.exists()
