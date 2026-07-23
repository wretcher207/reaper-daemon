#!/usr/bin/env python3
"""verifyloop — closed-loop measurement for the Reaper Daemon bridge.

measure(): preflight -> freeze capture bounds -> capture -> metrics dict.
This is the first half of the capture-mutate-capture-diff loop
(docs/SPEC_VERIFY_LOOP.md); verify runs it on both sides of a mutation and
reports measured deltas. All bridge traffic goes through reaperd.send_type.

Stdlib only, like the rest of the repo. Post Mortem's analysis module (needs
numpy) is an optional import that upgrades metrics from "LUFS-I out of
RENDER_STATS" to full spectrum/RMS/stereo; every result labels which mode
produced it in metrics_source, and the two modes never mix silently.
"""

import datetime
import os
import re
import secrets
import tempfile
import time

import reaperd

try:
    from postmortem.analysis import analyze_wav
except Exception:
    analyze_wav = None

DEFAULT_SECONDS = 10.0
# Verify captures are short evidence windows, not bounces. The bridge itself
# allows up to 600 s; refusing above 60 here keeps a verify round-trip (two
# renders) tolerable and is a deliberate policy, not a bridge limit.
MAX_SECONDS = 60.0
CAPTURE_TIMEOUT_MS = 180000  # matches reaper_mcp.CAPTURE_TIMEOUT_MS

# Same thresholds as reaper_mcp._run_postmortem: a capture at/below these is
# dead air, and no verdict may rest on it.
SILENT_RMS_DB = -60.0
SILENT_FRACTION = 0.85

CAPTURE_DIR_NAME = "reaper-verify"
# Filesystem mtime granularity can be coarse (2 s on FAT/exFAT) and the
# comparison is same-machine clock vs same-machine mtime, so a small slack
# avoids rejecting a genuinely fresh render.
MTIME_SLACK_SECONDS = 2.0


def _err(code, details, **extra):
    out = {"ok": False, "error": {"code": code, "details": details}}
    out.update(extra)
    return out


def metrics_source_available():
    """Which metrics mode measure() will run in right now."""
    return "postmortem" if analyze_wav is not None else "render_stats"


def _capture_output_path(track):
    """A unique timestamped WAV path under the OS temp dir (never the repo —
    this workspace can live under a syncing folder like OneDrive)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", track).strip("_")[:40] or "track"
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    directory = os.path.join(tempfile.gettempdir(), CAPTURE_DIR_NAME)
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{safe}-{stamp}-{secrets.token_hex(3)}.wav")


def _preflight(track, bridge_root):
    """Run get_capture_preflight; return (None, error_result) on refusal."""
    res = reaperd.send_type("get_capture_preflight",
                            {"target_track_name": track},
                            bridge_root=bridge_root, resolve=False, repair=False)
    if not res.get("ok"):
        err = res.get("error") or {}
        return None, _err(err.get("code", "PREFLIGHT_FAILED"),
                          f"capture preflight failed: {err.get('details')}")
    data = res.get("data", {})
    if data.get("capture_allowed"):
        return data, None
    blockers = data.get("blockers") or []
    codes = ", ".join(b.get("code", "?") for b in blockers) or "unknown"
    lines = [f"capture blocked for {track!r}: {codes}"]
    for b in blockers:
        if b.get("message"):
            lines.append(f"  - [{b.get('code')}] {b['message']}")
    risk = data.get("risk_gate") or {}
    if not risk.get("allow_risk_level_3"):
        # Users always trip on this: the flag is read once at REAPER startup.
        restart = risk.get("requires_restart_to_change")
        lines.append(
            "  - allow_risk_level_3 is false in bridge/bridge_config.json"
            + (f" (requires_restart_to_change: {restart} — set it true, then "
               f"restart REAPER)" if restart is not None else ""))
    return None, _err("CAPTURE_BLOCKED", "\n".join(lines))


def _check_window_args(seconds, start):
    """Validate the requested window without any bridge traffic. Returns an
    error result or None. Split out so measure() can refuse bad arguments
    before sending a single command."""
    duration = DEFAULT_SECONDS if seconds is None else float(seconds)
    if not (0 < duration <= MAX_SECONDS):
        return _err("BAD_SECONDS",
                    f"seconds must be in (0, {MAX_SECONDS:g}] for verify "
                    f"captures, got {duration:g}")
    if start is not None and float(start) < 0:
        return _err("BAD_START", f"start must be >= 0, got {float(start):g}")
    return None


def resolve_bounds(seconds=None, start=None, bridge_root=None):
    """Freeze the capture window ONCE, client-side, mirroring the bridge's own
    resolution (explicit start > active time selection > edit cursor). The
    frozen start/duration are then passed explicitly on every capture, so the
    user moving the cursor or time selection between two measures of the same
    spot cannot shift the window (verified: an explicit start_seconds bypasses
    the bridge's time-selection branch entirely).

    Returns (bounds_dict, None) or (None, error_result).
    """
    bad = _check_window_args(seconds, start)
    if bad:
        return None, bad
    duration = DEFAULT_SECONDS if seconds is None else float(seconds)
    if start is not None:
        return {"start_seconds": float(start), "duration_seconds": duration,
                "source": "explicit"}, None
    ctx = reaperd.send_type("get_context", {"include_fx": False},
                            bridge_root=bridge_root, resolve=False, repair=False)
    if not ctx.get("ok"):
        err = ctx.get("error") or {}
        return None, _err(err.get("code", "CONTEXT_FAILED"),
                          f"could not resolve capture bounds: {err.get('details')}")
    data = ctx.get("data", {})
    ts = data.get("time_selection") or {}
    if ts.get("active"):
        ts_start = float(ts.get("start") or 0.0)
        ts_len = float(ts.get("end") or 0.0) - ts_start
        if ts_len > 0:
            return {"start_seconds": ts_start,
                    "duration_seconds": min(duration, ts_len),
                    "source": "time_selection"}, None
    cursor = float((data.get("cursor") or {}).get("seconds") or 0.0)
    return {"start_seconds": cursor, "duration_seconds": duration,
            "source": "edit_cursor"}, None


def _judge_silence(metrics, source):
    """(silent, basis). basis None = could not assess (caller must warn)."""
    if source == "postmortem":
        rms = metrics.get("rms_db")
        frac = metrics.get("silence_fraction") or 0
        silent = ((rms is not None and rms <= SILENT_RMS_DB)
                  or frac >= SILENT_FRACTION)
        return silent, "postmortem"
    lufs = metrics.get("lufs_i")
    if lufs is not None:
        return lufs <= SILENT_RMS_DB, "lufs_i"
    return False, None


def measure(track, seconds=None, start=None, bounds=None, bridge_root=None,
            keep_wav=False):
    """One capture, one metrics dict. Never raises on bridge/audio problems —
    returns {"ok": False, "error": {...}} so callers (and verify) can branch.

    bounds: a dict from resolve_bounds() to reuse a frozen window (verify's
    post-measure passes the pre-measure's bounds so both captures are
    byte-identical in start/duration). When given, seconds/start are ignored.
    """
    if bounds is None:
        bad = _check_window_args(seconds, start)
        if bad:
            return bad
    data, refusal = _preflight(track, bridge_root)
    if refusal:
        return refusal
    if bounds is None:
        bounds, err = resolve_bounds(seconds=seconds, start=start,
                                     bridge_root=bridge_root)
        if err:
            return err

    output_file = _capture_output_path(track)
    sent_at = time.time()
    res = reaperd.send_type(
        "capture_track_audio",
        {"target_track_name": track,
         "start_seconds": bounds["start_seconds"],
         "duration_seconds": bounds["duration_seconds"],
         "output_file": output_file},
        bridge_root=bridge_root, timeout_ms=CAPTURE_TIMEOUT_MS,
        resolve=False, repair=False)
    if not res.get("ok"):
        err = res.get("error") or {}
        return _err(err.get("code", "CAPTURE_FAILED"),
                    f"capture failed: {err.get('details')}", bounds=bounds)
    cap = res.get("data", {})
    file_path = cap.get("file_path") or output_file

    # The schema demands the client verify freshness: a pre-existing file at
    # the render target (stale WAV, failed render leaving the old one) must
    # never be measured as if it were this capture.
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return _err("CAPTURE_FILE_MISSING",
                    f"bridge reported success but no file at {file_path}",
                    bounds=bounds)
    if mtime < sent_at - MTIME_SLACK_SECONDS:
        return _err("STALE_CAPTURE_FILE",
                    f"{file_path} was last modified before this capture was "
                    f"sent; refusing to measure a stale file", bounds=bounds)

    warnings = []
    source = metrics_source_available()
    metrics = {"lufs_i": cap.get("render_loudness_lufs")}
    if source == "postmortem":
        try:
            stats = analyze_wav(file_path)
            metrics.update({
                "sample_peak_db": stats.sample_peak_db,
                "rms_db": stats.rms_db,
                "crest_factor_db": stats.crest_factor_db,
                "silence_fraction": stats.silence_fraction,
                "spectrum_third_octave": stats.spectrum_third_octave,
                "stereo": stats.stereo,
                "duration_seconds": stats.duration_seconds,
                "sample_rate": stats.sample_rate,
                "channels": stats.channels,
            })
        except Exception as e:
            source = "render_stats"
            warnings.append(f"Post Mortem analysis failed ({e}); metrics "
                            f"degraded to render_stats (LUFS-I only)")

    silent, basis = _judge_silence(metrics, source)
    if basis is None:
        warnings.append("silence could not be assessed (no LUFS-I in "
                        "RENDER_STATS and Post Mortem is not installed); "
                        "treat level metrics with caution")
    if silent:
        warnings.append("capture is effectively SILENT — no verdict may rest "
                        "on these numbers; park the capture window where the "
                        "track is playing and re-measure")

    scope = cap.get("capture_scope")
    isolated = cap.get("isolation_verified") is True
    if not (scope == "isolated_track" and isolated):
        warnings.append(
            f"capture_scope is {scope or 'unknown'} (isolation_verified: "
            f"{cap.get('isolation_verified')}): these numbers describe that "
            f"capture scope, NOT necessarily the track alone")

    file_kept = True
    if not keep_wav and not silent:
        try:
            os.remove(file_path)
            file_kept = False
        except OSError:
            pass  # keeping the WAV is the safe failure mode

    return {
        "ok": True,
        "track": cap.get("track"),
        "bounds": bounds,
        "capture": {
            "file_path": file_path,
            "file_kept": file_kept,
            "file_size_bytes": cap.get("file_size_bytes"),
            "sample_rate": cap.get("sample_rate"),
            "capture_scope": scope,
            "isolation_verified": cap.get("isolation_verified"),
        },
        "metrics_source": source,
        "metrics": metrics,
        "silent": silent,
        "silence_basis": basis,
        "warnings": warnings,
    }


def _fmt_db(value, unit="dB"):
    return "n/a" if value is None else f"{value:.1f} {unit}"


def format_measure(result):
    """Short human-readable report for one measure() result."""
    if not result.get("ok"):
        err = result.get("error", {})
        return f"[measure] FAILED [{err.get('code')}]\n{err.get('details')}"
    lines = []
    track = result.get("track") or {}
    cap = result.get("capture", {})
    b = result.get("bounds", {})
    m = result.get("metrics", {})
    lines.append(f"[measure] track: {track.get('name', '?')}  "
                 f"scope: {cap.get('capture_scope')}"
                 + ("  (isolation verified)" if cap.get("isolation_verified")
                    else ""))
    lines.append(f"[measure] window: start {b.get('start_seconds'):.2f}s  "
                 f"duration {b.get('duration_seconds'):.2f}s  "
                 f"(from {b.get('source')})")
    lines.append(f"[measure] LUFS-I: {_fmt_db(m.get('lufs_i'), 'LUFS')}   "
                 f"metrics source: {result.get('metrics_source')}")
    if result.get("metrics_source") == "postmortem":
        lines.append(f"[measure] sample peak: {_fmt_db(m.get('sample_peak_db'), 'dBFS')}"
                     f"  RMS: {_fmt_db(m.get('rms_db'), 'dBFS')}"
                     f"  crest: {_fmt_db(m.get('crest_factor_db'))}")
        lines.append(f"[measure] silence fraction: {m.get('silence_fraction')}")
        stereo = m.get("stereo")
        if stereo:
            lines.append(f"[measure] stereo correlation: {stereo.get('correlation')}"
                         f"  balance: {_fmt_db(stereo.get('balance_db'))}")
    lines.append(f"[measure] silent: {'YES' if result.get('silent') else 'no'}")
    if cap.get("file_kept"):
        lines.append(f"[measure] WAV kept: {cap.get('file_path')}")
    for w in result.get("warnings", []):
        lines.append(f"[measure] WARNING: {w}")
    return "\n".join(lines)
