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
import math
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
        lines.append(
            "  - allow_risk_level_3 is false in bridge/bridge_config.json"
            + (" (requires_restart_to_change: the flag is read once at REAPER "
               "startup — set it true, then restart REAPER)"
               if risk.get("requires_restart_to_change") else ""))
    # Machine callers (the Phase-3 MCP tools) get the structured lists, not
    # just the prose.
    return None, _err("CAPTURE_BLOCKED", "\n".join(lines),
                      blockers=blockers, risk_gate=risk)


def _check_window_args(seconds, start):
    """Validate the requested window without any bridge traffic. Returns an
    error result or None. Split out so measure() can refuse bad arguments
    before sending a single command. Non-finite values are refused explicitly:
    json.dumps would emit a bare NaN/Infinity the bridge's JSON parser cannot
    read, costing the caller a full capture timeout."""
    duration = DEFAULT_SECONDS if seconds is None else float(seconds)
    if not (math.isfinite(duration) and 0 < duration <= MAX_SECONDS):
        return _err("BAD_SECONDS",
                    f"seconds must be in (0, {MAX_SECONDS:g}] for verify "
                    f"captures, got {duration:g}")
    if start is not None and not (math.isfinite(float(start))
                                  and float(start) >= 0):
        return _err("BAD_START",
                    f"start must be finite and >= 0, got {float(start):g}")
    return None


def _check_bounds_dict(bounds):
    """Validate a caller-supplied frozen-bounds dict (the verify reuse path)
    so measure() keeps its never-raises contract on malformed input."""
    if not isinstance(bounds, dict):
        return _err("BAD_BOUNDS",
                    f"bounds must be a dict from resolve_bounds(), got "
                    f"{type(bounds).__name__}")
    start = bounds.get("start_seconds")
    duration = bounds.get("duration_seconds")
    if (isinstance(start, bool) or not isinstance(start, (int, float))
            or not math.isfinite(start) or start < 0):
        return _err("BAD_BOUNDS", f"bounds.start_seconds invalid: {start!r}")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or not 0 < duration <= MAX_SECONDS):
        return _err("BAD_BOUNDS",
                    f"bounds.duration_seconds invalid: {duration!r}")
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
    """(silent, basis). basis None = could not assess (caller must warn).
    NaN samples in the WAV make rms_db NaN and every comparison False — that
    is an unassessable capture, not a clean one."""
    if source == "postmortem":
        rms = metrics.get("rms_db")
        if rms is not None and rms != rms:
            return False, None
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
    bad = (_check_window_args(seconds, start) if bounds is None
           else _check_bounds_dict(bounds))
    if bad:
        return bad
    preflight, refusal = _preflight(track, bridge_root)
    if refusal:
        return refusal
    warnings = [f"preflight: [{w.get('code')}] {w.get('message')}"
                for w in (preflight.get("warnings") or [])]
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
                    f"capture failed: {err.get('details')}", bounds=bounds,
                    warnings=warnings)
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
                    bounds=bounds, warnings=warnings)
    if mtime < sent_at - MTIME_SLACK_SECONDS:
        return _err("STALE_CAPTURE_FILE",
                    f"{file_path} was last modified before this capture was "
                    f"sent; refusing to measure a stale file", bounds=bounds,
                    warnings=warnings)

    source = metrics_source_available()
    lufs = cap.get("render_loudness_lufs")
    if isinstance(lufs, float) and lufs != lufs:  # NaN survives json.loads;
        lufs = None                               # treat as "not measured"
    metrics = {"lufs_i": lufs}
    degraded = False
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
            want = bounds["duration_seconds"]
            if abs(stats.duration_seconds - want) > max(0.25, 0.05 * want):
                warnings.append(
                    f"capture WAV is {stats.duration_seconds:g}s but the "
                    f"requested window was {want:g}s — the render may have "
                    f"been truncated; the metrics describe the file, not the "
                    f"full window")
        except Exception as e:
            source = "render_stats"
            degraded = True
            warnings.append(f"Post Mortem analysis of the capture failed "
                            f"({e}); metrics degraded to render_stats "
                            f"(LUFS-I only) — the WAV is kept for debugging")

    silent, basis = _judge_silence(metrics, source)
    if basis is None:
        warnings.append("silence could not be assessed (no LUFS-I in "
                        "RENDER_STATS and no usable Post Mortem analysis); "
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

    # The WAV is evidence. It survives silence (debug why it's silent) and
    # analysis failure (debug why Post Mortem choked) — deletion happens only
    # for a clean, fully-analyzed success the caller didn't ask to keep.
    file_kept = True
    if not keep_wav and not silent and not degraded:
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


# verify() exit codes — agents branch on these, so they are part of the
# contract: 0 = both captures clean and deltas reported; 1 = nothing was
# mutated (pre-measure refused, or the mutation itself failed); 2 = the
# mutation IS applied but the post-measure could not prove what it did.
EXIT_VERIFIED = 0
EXIT_MUTATION_FAILED = 1
EXIT_UNVERIFIED = 2

# Said verbatim whenever verify leaves a mutation applied but unproven. The
# asymmetry is deliberate: never destroy a user-visible change because
# measurement hiccupped.
UNVERIFIED_NOTE = ("The mutation is NOT rolled back — it is one Ctrl/Cmd+Z "
                   "away if you don't want it.")
MUTATION_TIMEOUT_MS = 30000


def _capture_mismatch(pre, post):
    """A reason the two captures' CONTENT metrics cannot be compared (format
    drift, truncated render), or None. LUFS-I stays comparable — it describes
    whatever actually rendered — but spectrum/RMS/stereo/silence compared
    across different formats or lengths would be a quiet lie."""
    pm, qm = pre.get("metrics", {}), post.get("metrics", {})
    drifts = []
    for key, tol in (("sample_rate", 0), ("channels", 0),
                     ("duration_seconds", 0.1)):
        a, b = pm.get(key), qm.get(key)
        if a is not None and b is not None and abs(a - b) > tol:
            drifts.append(f"{key} {a} -> {b}")
    if drifts:
        return ("pre and post captures are not directly comparable ("
                + ", ".join(drifts) + "); content deltas (spectrum/RMS/stereo/"
                "silence) are omitted, only LUFS-I is compared")
    return None


def _delta_metrics(pre, post, content_comparable=True):
    """Measured deltas between two measure() results. Only fields that are
    numeric on BOTH sides are compared — a metric that exists on one side only
    (e.g. Post Mortem analysis degraded once) is silently absent, never
    guessed. content_comparable=False (format/length drift between the WAVs)
    restricts the comparison to LUFS-I."""
    pm, qm = pre.get("metrics", {}), post.get("metrics", {})
    deltas = {}
    content_keys = (("sample_peak_db", "rms_db", "crest_factor_db",
                     "silence_fraction") if content_comparable else ())
    for key in ("lufs_i",) + content_keys:
        a, b = pm.get(key), qm.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            deltas[key] = {"pre": a, "post": b, "delta": round(b - a, 2)}
    if not content_comparable:
        return deltas
    pa, qa = pm.get("stereo") or {}, qm.get("stereo") or {}
    stereo = {}
    for key in ("correlation", "side_rms_db", "mid_rms_db", "balance_db"):
        a, b = pa.get(key), qa.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            stereo[key] = {"pre": a, "post": b, "delta": round(b - a, 3)}
    if stereo:
        deltas["stereo"] = stereo
    ps = {b["freq_hz"]: b["level_db"] for b in pm.get("spectrum_third_octave") or []}
    qs = {b["freq_hz"]: b["level_db"] for b in qm.get("spectrum_third_octave") or []}
    bands = [{"freq_hz": f, "pre": ps[f], "post": qs[f],
              "delta": round(qs[f] - ps[f], 1)}
             for f in ps if f in qs]
    if bands:
        deltas["spectrum_third_octave"] = bands
    return deltas


def _scope_warnings(pre, post):
    warnings = []
    pre_cap, post_cap = pre.get("capture", {}), post.get("capture", {})
    if pre_cap.get("capture_scope") != post_cap.get("capture_scope"):
        warnings.append(
            f"capture scope CHANGED between measures "
            f"({pre_cap.get('capture_scope')} -> {post_cap.get('capture_scope')}"
            f"): the deltas compare two different signals and are not "
            f"per-track evidence")
    elif not (pre_cap.get("capture_scope") == "isolated_track"
              and pre_cap.get("isolation_verified") is True
              and post_cap.get("isolation_verified") is True):
        warnings.append(
            f"captures are {pre_cap.get('capture_scope') or 'unknown'} (not a "
            f"verified isolated track): the deltas describe that capture "
            f"scope, not necessarily this track alone")
    if pre.get("metrics_source") != post.get("metrics_source"):
        warnings.append(
            f"metrics source differs between measures "
            f"({pre.get('metrics_source')} pre, {post.get('metrics_source')} "
            f"post); only metrics present on both sides are compared")
    return warnings


# A mutation "failure" with one of these codes means NO REPLY was read — the
# command was withdrawn from the inbox, but the bridge may have grabbed it
# first (it moves inbox -> processing before executing) or replied just after
# the deadline. The mutation state is UNKNOWN, never "not applied".
UNKNOWN_MUTATION_CODES = ("TIMEOUT", "NO_REPLY", "BAD_REPLY")


def verify(track, cmd_type, payload, seconds=None, start=None,
           bridge_root=None, keep_wav=False):
    """measure -> mutate -> measure with frozen bounds -> measured deltas.

    Returns a result whose exit_code field implements the contract above.
    mutation_applied is three-valued: True, False, or None (unknown — e.g. a
    mutation timeout, or a batch that failed partway). The mutation goes
    through the same send path as `reaperd.py cmd` (add_fx name resolution and
    set_fx_param alias repair included).
    """
    if not isinstance(payload, dict):
        return {"ok": False, "verdict": "BAD_PAYLOAD",
                "exit_code": EXIT_MUTATION_FAILED, "mutation_applied": False,
                "error": {"code": "BAD_PAYLOAD",
                          "details": f"mutation payload must be a JSON object, "
                                     f"got {type(payload).__name__}"},
                "message": "mutation payload must be a JSON object; nothing "
                           "was captured or mutated."}

    pre = measure(track, seconds=seconds, start=start, bridge_root=bridge_root,
                  keep_wav=keep_wav)
    if not pre.get("ok"):
        return {"ok": False, "verdict": "PRE_MEASURE_BLOCKED",
                "exit_code": EXIT_MUTATION_FAILED, "mutation_applied": False,
                "error": pre.get("error"), "pre": pre,
                "message": "pre-measure refused; nothing was mutated. Fix the "
                           "capture (or use `cmd` if you don't need proof)."}
    if pre.get("silent"):
        return {"ok": False, "verdict": "PRE_MEASURE_SILENT",
                "exit_code": EXIT_MUTATION_FAILED, "mutation_applied": False,
                "pre": pre,
                "message": "pre-capture is silent, so no delta could prove "
                           "anything; nothing was mutated. Park the capture "
                           "window where the track is playing, or use `cmd` "
                           "for an unverified change."}
    if pre.get("silence_basis") is None:
        return {"ok": False, "verdict": "PRE_MEASURE_UNMEASURABLE",
                "exit_code": EXIT_MUTATION_FAILED, "mutation_applied": False,
                "pre": pre,
                "message": "pre-capture produced no assessable level metrics "
                           "(no LUFS-I from RENDER_STATS and no usable Post "
                           "Mortem analysis), so no delta could prove "
                           "anything; nothing was mutated. Fix the capture, "
                           "or use `cmd` for an unverified change."}

    mut = reaperd.send_type(cmd_type, dict(payload), bridge_root=bridge_root,
                            timeout_ms=MUTATION_TIMEOUT_MS)
    if not mut.get("ok"):
        code = (mut.get("error") or {}).get("code")
        if code in UNKNOWN_MUTATION_CODES:
            return {"ok": False, "verdict": "MUTATION_UNKNOWN",
                    "exit_code": EXIT_UNVERIFIED, "mutation_applied": None,
                    "error": mut.get("error"), "pre": pre,
                    "message": f"no reply for mutation {cmd_type} ({code}). "
                               f"The command was withdrawn, but the bridge may "
                               f"have grabbed it first and may still execute "
                               f"it — the project state is UNKNOWN. Re-scan "
                               f"(get_fx_parameters / get_context) before "
                               f"deciding anything; do NOT blindly resend."}
        if cmd_type == "batch":
            return {"ok": False, "verdict": "MUTATION_FAILED",
                    "exit_code": EXIT_UNVERIFIED, "mutation_applied": None,
                    "error": mut.get("error"), "pre": pre,
                    "message": "batch failed partway: any sub-commands that "
                               "ran before the failure ARE applied (they share "
                               "one undo point — Ctrl/Cmd+Z reverts them). No "
                               "post-capture attempted."}
        return {"ok": False, "verdict": "MUTATION_FAILED",
                "exit_code": EXIT_MUTATION_FAILED, "mutation_applied": False,
                "error": mut.get("error"), "pre": pre,
                "message": f"mutation {cmd_type} failed; no post-capture "
                           f"attempted, nothing to roll back."}

    try:
        post = measure(track, bounds=pre["bounds"], bridge_root=bridge_root,
                       keep_wav=keep_wav)
    except Exception as e:  # measure() shouldn't raise; the mutation IS
        post = _err("POST_MEASURE_CRASH",     # applied, so never exit 1 here.
                    f"unexpected error during post-measure: {e!r}")
    if not post.get("ok") or post.get("silent") \
            or post.get("silence_basis") is None:
        why = ("post-capture failed" if not post.get("ok")
               else "post-capture is silent" if post.get("silent")
               else "post-capture level could not be assessed")
        return {"ok": False, "verdict": "UNVERIFIED",
                "exit_code": EXIT_UNVERIFIED, "mutation_applied": True,
                "error": post.get("error"), "pre": pre, "post": post,
                "message": f"mutation {cmd_type} IS applied, but {why}, so "
                           f"its effect is unproven. {UNVERIFIED_NOTE}"}

    warnings = _scope_warnings(pre, post)
    mismatch = _capture_mismatch(pre, post)
    if mismatch:
        warnings.append(mismatch)
    # Per-measure warnings (e.g. Post Mortem analysis degrading) must reach
    # the report a reader actually sees, not just the nested pre/post dicts.
    # Scope warnings are excluded: _scope_warnings already covers scope at
    # the verify level.
    for label, m in (("pre", pre), ("post", post)):
        for w in m.get("warnings", []):
            if not w.startswith("capture_scope is"):
                warnings.append(f"{label}-measure: {w}")
    deltas = _delta_metrics(pre, post, content_comparable=not mismatch)
    if not deltas:
        # Both captures individually assessable, yet no metric is numeric on
        # BOTH sides (e.g. one side's analysis degraded while the other's
        # LUFS went missing). VERIFIED means evidence; zero deltas is zero
        # evidence.
        return {"ok": False, "verdict": "UNVERIFIED",
                "exit_code": EXIT_UNVERIFIED, "mutation_applied": True,
                "pre": pre, "post": post, "warnings": warnings,
                "message": f"mutation {cmd_type} IS applied and both captures "
                           f"completed, but no metric was comparable across "
                           f"both sides, so there is no evidence of its "
                           f"effect. {UNVERIFIED_NOTE}"}
    return {"ok": True, "verdict": "VERIFIED", "exit_code": EXIT_VERIFIED,
            "mutation_applied": True, "mutation": {"type": cmd_type,
                                                   "result": mut.get("data")},
            "pre": pre, "post": post, "bounds": pre["bounds"],
            "deltas": deltas, "warnings": warnings}


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
    lines.append("[measure] silent: "
                 + ("unassessed" if result.get("silence_basis") is None
                    else "YES" if result.get("silent") else "no"))
    if cap.get("file_kept"):
        lines.append(f"[measure] WAV kept: {cap.get('file_path')}")
    for w in result.get("warnings", []):
        lines.append(f"[measure] WARNING: {w}")
    return "\n".join(lines)


def format_verify(result):
    """Short human-readable report for one verify() result. The verdict line
    always states whether the project was mutated — that is the one thing a
    reader must never be wrong about."""
    verdict = result.get("verdict")
    applied = result.get("mutation_applied")
    applied_text = ("yes" if applied is True
                    else "NO" if applied is False
                    else "UNKNOWN")
    lines = [f"[verify] VERDICT: {verdict}  (mutation applied: {applied_text})"]
    if result.get("message"):
        lines.append(f"[verify] {result['message']}")
    if result.get("error"):
        err = result["error"]
        lines.append(f"[verify] error [{err.get('code')}]: {err.get('details')}")
    deltas = result.get("deltas") or {}
    label = {"lufs_i": ("LUFS-I", "LUFS"), "sample_peak_db": ("sample peak", "dB"),
             "rms_db": ("RMS", "dB"), "crest_factor_db": ("crest", "dB"),
             "silence_fraction": ("silence fraction", "")}
    for key, (name, unit) in label.items():
        d = deltas.get(key)
        if d:
            lines.append(f"[verify] {name}: {d['pre']:g} -> {d['post']:g}  "
                         f"(delta {d['delta'] or 0:+g}"
                         f"{' ' + unit if unit else ''})")
    stereo = deltas.get("stereo") or {}
    for key, d in stereo.items():
        lines.append(f"[verify] stereo {key}: {d['pre']:g} -> {d['post']:g}  "
                     f"(delta {d['delta'] or 0:+g})")
    bands = deltas.get("spectrum_third_octave") or []
    moved = [b for b in bands if abs(b["delta"]) >= 1.0]
    if bands:
        if moved:
            lines.append("[verify] spectrum bands that moved >= 1 dB:")
            for b in moved:
                lines.append(f"[verify]   {b['freq_hz']:>6g} Hz: "
                             f"{b['pre']:g} -> {b['post']:g} dB "
                             f"(delta {b['delta']:+g})")
        else:
            lines.append("[verify] spectrum: no 1/3-octave band moved >= 1 dB")
    for w in result.get("warnings", []):
        lines.append(f"[verify] WARNING: {w}")
    return "\n".join(lines)
