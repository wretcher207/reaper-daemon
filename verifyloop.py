"""verifyloop — closed-loop measurement primitives for the agent bridge.

Phase 1 of docs/SPEC_VERIFY_LOOP.md: `measure` captures one track once and
returns a metrics dict an agent can compare against a later capture of the
same spot. The loop lives here in Python as a sequencer of existing bridge
commands (capture_track_audio, get_capture_preflight, get_context) — the
bridge itself needs no changes.

Stdlib only, like the rest of this repo. Post Mortem's analysis module
(numpy) is imported ONLY when installed; without it a capture still yields
LUFS-I whenever REAPER reports one (digital silence reads -inf, which the
bridge maps to null — flagged silent here) and the result is labeled
`metrics_source: "render_stats"` instead of `"postmortem"`. Deliberately
does NOT import `postmortem.diagnose` (it pulls in the anthropic SDK at
module top); the small RENDER_STATS string parser lives here instead.

No direct inbox/outbox writes: callers inject a `sender(cmd_type, payload,
timeout_ms=...) -> reply-dict` built on reaperd.send_type, which keeps
transport, auth, and repair logic in exactly one place and makes this module
testable without a live REAPER.
"""

import math
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone

DEFAULT_SECONDS = 10          # Post Mortem's own single-track default
MAX_SECONDS = 60              # verify captures are short; 600s renders are not verify material
CAPTURE_TIMEOUT_MS = 180000   # a capture blocks the bridge for the render duration

# Silence guard thresholds, copied from reaper_mcp._run_postmortem /
# postmortem.cli — a verdict on dead air would be accurate and useless.
NEAR_SILENT_RMS_DB = -60.0
SILENCE_GATE_FRACTION = 0.85

# Bridge-echoed bounds may differ from the requested ones only by float
# noise; anything larger means the audio does not describe the window we asked
# for and the measurement must be refused.
BOUNDS_ECHO_TOLERANCE = 1e-6


# ---------------------------------------------------------------------------
# RENDER_STATS parsing (string parsing, not DSP)
# ---------------------------------------------------------------------------

# REAPER's RENDER_STATS keys -> metrics field names. Semicolon-separated
# KEY:value pairs; key spellings vary between REAPER builds, so each field
# lists every spelling observed or plausible; first match wins. Same mapping
# Post Mortem uses (postmortem/diagnose.py), duplicated here because that
# module hard-imports the anthropic SDK. LUFS-I is absent on purpose: the
# bridge already parses it into `render_loudness_lufs`.
_RENDER_STATS_FIELDS = [
    ("true_peak_db", ("TRUEPEAK", "TPEAK", "TPK")),
    ("loudness_range_lu", ("LRA",)),
    ("lufs_momentary_max", ("LUFSMMAX", "MAXLUFSM", "LUFSM")),
    ("lufs_short_term_max", ("LUFSSMAX", "MAXLUFSS", "LUFSS")),
]


def parse_render_stats(raw):
    """Extract true peak, LRA, and LUFS maxima from a raw RENDER_STATS string.
    Returns {} when raw is missing; absent keys are omitted, never null-filled."""
    if not raw:
        return {}
    values = {}
    for pair in str(raw).split(";"):
        key, sep, value = pair.partition(":")
        if not sep:
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        if math.isfinite(number):
            values.setdefault(key.strip().upper(), number)
    out = {}
    for field, keys in _RENDER_STATS_FIELDS:
        for key in keys:
            if key in values:
                out[field] = values[key]
                break
    return out


# ---------------------------------------------------------------------------
# Optional Post Mortem analysis
# ---------------------------------------------------------------------------

def _load_analyzer():
    """Post Mortem's analyze_wav when the package is importable, else None.
    Split out so tests can force either mode without touching sys.modules."""
    try:
        from postmortem.analysis import analyze_wav
        return analyze_wav
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Errors / bounds
# ---------------------------------------------------------------------------

def _error(code, details, **extra):
    return {"ok": False, "error": {"code": code, "details": details}, **extra}


def _sanitize_nonfinite(value):
    """Replace every non-finite float (NaN, +/-inf) with None, recursively.
    NaN poisons threshold comparisons (every one is False, so a NaN RMS would
    sail past the silence guard as 'not silent') and json.dumps emits bare
    NaN, which strict JSON consumers reject. None is honest: not measured."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _sanitize_nonfinite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_nonfinite(v) for v in value]
    return value


def resolve_bounds(sender, seconds, start_seconds=None):
    """Resolve capture bounds ONCE, mirroring the bridge's own resolution
    (explicit start > active time selection > edit cursor), so a later capture
    of the same spot uses byte-identical bounds. Returns
    {"ok": True, "start_seconds": s, "duration_seconds": d, "bounds_source": ...}
    or an error dict."""
    if start_seconds is not None:
        return {"ok": True, "start_seconds": float(start_seconds),
                "duration_seconds": float(seconds), "bounds_source": "explicit_start"}
    ctx = sender("get_context", {"include_fx": False})
    if not ctx.get("ok"):
        return _error("BOUNDS_UNRESOLVED",
                      "get_context failed while resolving capture bounds: "
                      f"{ctx.get('error')}")
    data = ctx.get("data") or {}
    ts = data.get("time_selection") or {}
    if ts.get("active"):
        length = float(ts.get("end", 0)) - float(ts.get("start", 0))
        if length <= 0:
            return _error("BOUNDS_UNRESOLVED",
                          "active time selection has non-positive length")
        return {"ok": True, "start_seconds": float(ts.get("start", 0)),
                "duration_seconds": min(float(seconds), length),
                "bounds_source": "time_selection"}
    cursor = (data.get("cursor") or {}).get("seconds")
    if cursor is None:
        return _error("BOUNDS_UNRESOLVED", "get_context reply has no cursor position")
    return {"ok": True, "start_seconds": float(cursor),
            "duration_seconds": float(seconds), "bounds_source": "edit_cursor"}


def _capture_output_path(output_dir=None):
    """Unique timestamped WAV path under the OS temp dir (never the repo —
    this workspace is OneDrive-synced). Same convention as reaper_mcp's
    capture tool, separate folder."""
    out_dir = output_dir or os.path.join(tempfile.gettempdir(), "reaper-verify")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    return os.path.join(out_dir, f"measure-{stamp}-{secrets.token_hex(4)}.wav")


# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------

def measure(sender, track, seconds=None, start_seconds=None, output_dir=None,
            keep_wav=False, track_guid=None, _analyzer_loader=_load_analyzer,
            _bounds=None):
    """One capture, one metrics dict.

    sender: callable(cmd_type, payload, timeout_ms=...) -> parsed reply dict.
    track: exact track name (case-insensitive) or "master".
    track_guid: when given, targets by GUID instead of name (verify pins the
    post-capture to the exact track the pre-capture resolved).
    Returns a dict with "ok": True and metrics, or "ok": False with an error.
    A silent capture is still ok: True but carries silent: True — callers must
    refuse verdicts on it, not pretend it failed.

    _bounds: internal — already-resolved {"start_seconds", "duration_seconds"}
    from a previous measure (verify's frozen post-capture bounds). Skips both
    user-input validation and bounds resolution; the values were validated
    when they were first resolved.
    """
    if _bounds is None:
        seconds = DEFAULT_SECONDS if seconds is None else seconds
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return _error("BAD_SECONDS", f"seconds must be a number, got {seconds!r}")
        if not 1 <= seconds <= MAX_SECONDS:
            return _error("BAD_SECONDS",
                          f"seconds must be between 1 and {MAX_SECONDS} for measure "
                          f"(got {seconds:g}); long renders are not verify material")

    selector = ({"target_track_guid": track_guid} if track_guid
                else {"target_track_name": track})

    # 1. Preflight: everything that would block or degrade the capture,
    # without rendering.
    pre = sender("get_capture_preflight", dict(selector))
    if not pre.get("ok"):
        return _error("PREFLIGHT_FAILED",
                      f"get_capture_preflight failed: {pre.get('error')}")
    pdata = pre.get("data") or {}
    if not pdata.get("capture_allowed"):
        blockers = pdata.get("blockers") or []
        risk_gate = pdata.get("risk_gate") or {}
        details = "capture blocked: " + (
            "; ".join(f"{b.get('code')}: {b.get('message')}" for b in blockers)
            or "no blocker detail from preflight")
        if any(b.get("code") == "capture_gated" for b in blockers):
            # Users always trip on this: the flag is read once at startup.
            details += (
                " | risk_gate.requires_restart_to_change="
                f"{risk_gate.get('requires_restart_to_change')}: set "
                "allow_risk_level_3 true in bridge/bridge_config.json, then "
                "RESTART REAPER — the flag is read once at bridge startup.")
        return _error("CAPTURE_BLOCKED", details,
                      blockers=blockers, warnings=pdata.get("warnings") or [],
                      risk_gate=risk_gate)

    # 2. Bounds, resolved once and passed explicitly (start_seconds fully
    # neutralizes the time selection in the bridge — verified in the Lua).
    if _bounds is not None:
        bounds = {"ok": True,
                  "start_seconds": float(_bounds["start_seconds"]),
                  "duration_seconds": float(_bounds["duration_seconds"]),
                  "bounds_source": _bounds.get("bounds_source", "frozen")}
    else:
        bounds = resolve_bounds(sender, seconds, start_seconds)
        if not bounds.get("ok"):
            return bounds

    # 3. Capture. The output path is unique per invocation; prove it does not
    # exist BEFORE sending — then a file appearing at that exact path can only
    # have been created after the command, which is the freshness guarantee
    # (independent of filesystem mtime granularity).
    output_file = _capture_output_path(output_dir)
    if os.path.exists(output_file):
        return _error("OUTPUT_PATH_COLLISION",
                      f"fresh capture path already exists: {output_file}")
    sent_at = time.time()
    res = sender("capture_track_audio", {
        **selector,
        "start_seconds": bounds["start_seconds"],
        "duration_seconds": bounds["duration_seconds"],
        "output_file": output_file,
    }, timeout_ms=CAPTURE_TIMEOUT_MS)
    if not res.get("ok"):
        err = (res.get("error") or {})
        # The render may have written a partial WAV before the bridge errored
        # (e.g. a restore failure AFTER a successful render). Disclose the
        # path so a kept file is findable, never silently orphaned.
        return _error(err.get("code") or "CAPTURE_FAILED",
                      f"capture_track_audio failed: {err.get('details') or err}",
                      output_file=output_file,
                      note="a partial capture WAV may exist at output_file; "
                           "it is kept for debugging")
    data = res.get("data") or {}
    file_path = data.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        return _error("CAPTURE_FILE_MISSING",
                      f"capture reported ok but no file at {file_path!r}")
    # The schema demands the client verify freshness: a stale or replayed
    # reply pointing at an old WAV must never become evidence. Two cases:
    # - reply reports OUR unique output path: fresh by construction (it did
    #   not exist before the send — proven above), no mtime guessing needed,
    #   so coarse-timestamp filesystems cannot false-reject a good capture;
    # - reply reports any OTHER path (REAPER renamed, or a replayed reply):
    #   strict mtime — the file must not predate the send at all.
    same_file = (os.path.normcase(os.path.abspath(file_path))
                 == os.path.normcase(os.path.abspath(output_file)))
    if not same_file and os.path.getmtime(file_path) < sent_at:
        return _error("STALE_CAPTURE_FILE",
                      f"{file_path} predates the capture command "
                      "(mtime older than send time); refusing to trust it",
                      file_path=file_path)
    # Bounds honesty: the bridge echoes the window it actually rendered. A
    # missing, null, or non-numeric echo is treated exactly like a mismatch —
    # fail closed; "cannot confirm the window" must never read as "verified".
    for key, requested in (("start_seconds", bounds["start_seconds"]),
                           ("duration_seconds", bounds["duration_seconds"])):
        echoed = data.get(key)
        try:
            echoed_f = float(echoed)
        except (TypeError, ValueError):
            echoed_f = None
        if (echoed_f is None or not math.isfinite(echoed_f)
                or abs(echoed_f - requested) > BOUNDS_ECHO_TOLERANCE):
            return _error("BOUNDS_MISMATCH",
                          f"bridge echoed {key}={echoed!r} but {requested} was "
                          "requested; cannot confirm the audio describes the "
                          "requested window", file_path=file_path)

    # 4. Metrics. LUFS-I and provenance always; Post Mortem analysis on top
    # when importable.
    metrics = {
        "ok": True,
        "track": data.get("track"),
        "file_path": file_path,
        "bounds": {"start_seconds": bounds["start_seconds"],
                   "duration_seconds": bounds["duration_seconds"]},
        "bounds_source": bounds["bounds_source"],
        "capture_scope": data.get("capture_scope"),
        "isolation_verified": data.get("isolation_verified") is True,
        "lufs_i": data.get("render_loudness_lufs"),
        "metrics_source": "render_stats",
    }
    metrics.update(parse_render_stats(data.get("render_stats_raw")))

    analyzer = _analyzer_loader()
    if analyzer is not None:
        # The attribute reads live INSIDE the try: an incompatible Post
        # Mortem returning a wrong-shaped object must degrade to
        # render_stats, not crash measure and leak the WAV.
        try:
            stats = analyzer(file_path)
            extra = {
                "sample_peak_db": stats.sample_peak_db,
                "rms_db": stats.rms_db,
                "crest_factor_db": stats.crest_factor_db,
                "silence_fraction": stats.silence_fraction,
                "spectrum_third_octave": stats.spectrum_third_octave,
                "stereo": stats.stereo,
            }
        except Exception as e:
            metrics["analysis_error"] = f"{type(e).__name__}: {e}"
        else:
            metrics["metrics_source"] = "postmortem"
            metrics.update(extra)

    # 5. Silence guard — callers must refuse verdicts on a silent capture.
    # Sanitize first: NaN/inf would make every threshold comparison False and
    # emit invalid JSON downstream.
    metrics = _sanitize_nonfinite(metrics)
    silent, reason = _judge_silence(metrics)
    metrics["silent"] = silent
    if reason:
        metrics["silence_reason"] = reason

    if not keep_wav:
        try:
            os.unlink(file_path)
            metrics["wav_kept"] = False
        except OSError:
            metrics["wav_kept"] = True
    else:
        metrics["wav_kept"] = True
    return metrics


def _judge_silence(metrics):
    """(silent, reason) for a metrics dict. With Post Mortem stats the RMS /
    silence-fraction thresholds apply; in render_stats mode LUFS-I is the only
    level evidence, so the same -60 floor applies to it."""
    if metrics.get("metrics_source") == "postmortem":
        rms = metrics.get("rms_db")
        frac = metrics.get("silence_fraction")
        if rms is None:
            # A sanitized (non-finite) RMS means the level could not be
            # measured at all — fail closed, never "not silent" by default.
            return True, ("RMS is not a finite number; the capture is "
                          "unusable as level evidence")
        if rms <= NEAR_SILENT_RMS_DB:
            return True, (f"capture is essentially silent (RMS {rms:.1f} dBFS "
                          f"<= {NEAR_SILENT_RMS_DB:.0f})")
        if frac is not None and frac >= SILENCE_GATE_FRACTION:
            return True, (f"{frac:.0%} of the capture is silence "
                          f"(>= {SILENCE_GATE_FRACTION:.0%})")
        return False, None
    lufs = metrics.get("lufs_i")
    if lufs is None:
        # REAPER reports -inf LUFS for digital silence and the bridge maps
        # non-finite to null — with no Post Mortem there is no other level
        # evidence, so treat "no LUFS at all" as silence, not as a pass.
        return True, ("no LUFS-I in RENDER_STATS (digital silence reads -inf) "
                      "and Post Mortem is not installed to measure the file")
    if lufs <= NEAR_SILENT_RMS_DB:
        return True, f"LUFS-I {lufs:.1f} <= {NEAR_SILENT_RMS_DB:.0f}"
    return False, None


# ---------------------------------------------------------------------------
# verify: measure -> mutate -> measure -> verdict
# ---------------------------------------------------------------------------

# Exit codes are the contract agents branch on (spec Phase 2):
# 0 VERIFIED, 1 mutation not applied (failed OR refused before mutating),
# 2 UNVERIFIED (mutation applied, post-measure failed/silent — NOT rolled back).
EXIT_VERIFIED = 0
EXIT_MUTATION_FAILED = 1
EXIT_UNVERIFIED = 2

NOT_ROLLED_BACK = ("The mutation is NOT rolled back (it's one Ctrl/Cmd+Z "
                   "away). This is deliberate: a user-visible change is never "
                   "destroyed because measurement hiccupped.")


def compute_deltas(pre, post):
    """Measured differences between two measure dicts (post minus pre).
    Only fields finite in BOTH captures produce a delta — a None on either
    side yields no delta, never a fabricated zero."""
    deltas = {}

    def fin(v):
        """The value if it is a finite real number, else None. Enforced here
        as well as in measure's sanitizer: a delta must never be built from
        NaN/inf even if a caller feeds compute_deltas unsanitized dicts."""
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return v if math.isfinite(v) else None

    def num(m, key):
        return fin(m.get(key))

    rounding = {"silence_fraction": 3}
    for key in ("lufs_i", "true_peak_db", "loudness_range_lu",
                "sample_peak_db", "rms_db", "crest_factor_db",
                "silence_fraction"):
        a, b = num(pre, key), num(post, key)
        if a is not None and b is not None:
            deltas[key + "_delta"] = round(b - a, rounding.get(key, 2))

    stereo = {}
    pre_st, post_st = pre.get("stereo") or {}, post.get("stereo") or {}
    for key in ("correlation", "mid_rms_db", "side_rms_db", "balance_db"):
        a, b = fin(pre_st.get(key)), fin(post_st.get(key))
        if a is not None and b is not None:
            stereo[key + "_delta"] = round(b - a, 3 if key == "correlation" else 2)
    # Stereo width: side-to-mid ratio in dB (side_rms_db - mid_rms_db) —
    # arithmetic over Post Mortem's existing numbers, no new DSP. Positive
    # width delta = the image got wider.
    widths = []
    for st in (pre_st, post_st):
        side, mid = fin(st.get("side_rms_db")), fin(st.get("mid_rms_db"))
        widths.append(side - mid if side is not None and mid is not None else None)
    if widths[0] is not None and widths[1] is not None:
        stereo["width_db_delta"] = round(widths[1] - widths[0], 2)
    if stereo:
        deltas["stereo"] = stereo

    pre_bands = {b.get("freq_hz"): b.get("level_db")
                 for b in pre.get("spectrum_third_octave") or []
                 if isinstance(b, dict) and fin(b.get("freq_hz")) is not None}
    post_bands = {b.get("freq_hz"): b.get("level_db")
                  for b in post.get("spectrum_third_octave") or []
                  if isinstance(b, dict) and fin(b.get("freq_hz")) is not None}
    band_deltas = []
    for freq in sorted(f for f in pre_bands if f in post_bands):
        a, b = fin(pre_bands[freq]), fin(post_bands[freq])
        if a is not None and b is not None:
            band_deltas.append({"freq_hz": freq, "pre_db": a, "post_db": b,
                                "delta_db": round(b - a, 1)})
    if band_deltas:
        deltas["spectrum_band_deltas"] = band_deltas
    deltas["masking"] = "not applicable: single-track verify"
    return deltas


def _scope_warning(pre, post):
    """Honesty caveat when the deltas do not describe the isolated track."""
    pre_scope, post_scope = pre.get("capture_scope"), post.get("capture_scope")
    if pre_scope != post_scope:
        return (f"pre and post captures have DIFFERENT scopes ({pre_scope} vs "
                f"{post_scope}); the deltas compare unlike evidence and must "
                "not be attributed to this track alone.")
    isolated = (pre_scope == "isolated_track"
                and pre.get("isolation_verified") and post.get("isolation_verified"))
    if not isolated:
        return (f"captures have scope {pre_scope!r} (isolation not verified); "
                "the deltas describe that capture scope, not necessarily this "
                "track alone.")
    return None


# Mutation replies with these transport error codes leave the outcome UNKNOWN:
# the command may have executed without a readable reply (locked file, late
# reply) or may still execute later (a timed-out inbox withdraw can miss an
# in-flight command). Exit 1's "nothing was changed" promise cannot be made.
UNCERTAIN_MUTATION_CODES = {"TIMEOUT", "NO_REPLY", "BAD_REPLY",
                            "STALE_REPLY_LOCKED"}


def verify(sender, mutator, track, cmd_type, payload, seconds=None,
           start_seconds=None, output_dir=None, keep_wav=False,
           progress=None, _analyzer_loader=_load_analyzer):
    """measure -> mutate -> measure with frozen bounds -> measured verdict.

    sender: read/capture transport (no name resolution).
    mutator: callable(cmd_type, payload) -> reply, routed like `cmd` (add_fx
    name resolution and set_fx_param alias repair apply).
    progress: optional callable(str) invoked at step boundaries, so a killed
    process still leaves a trace of whether the mutation had been applied.
    Returns {"status", "exit_code", "pre", "mutation", "post", "deltas",
    "scope_warning", ...}. The mutation is never rolled back by this function.

    Exit-code contract (agents branch on it):
    0 VERIFIED — clean, comparable pre/post captures, deltas reported.
    1 mutation NOT applied — pre-measure refused, or the bridge rejected the
      command. (A rejected single command that failed mid-edit can leave a
      partial change inside one closed undo block; the note says so.)
    2 UNVERIFIED — the project MAY have changed (mutation applied, partially
      applied, or outcome unknown) but the change could not be measured.
      Nothing is rolled back. Do not retry blindly on exit 2.
    """
    def report(msg):
        if progress is not None:
            progress(msg)

    result = {"status": None, "exit_code": None, "track": track,
              "mutation": {"type": cmd_type, "payload": payload, "reply": None},
              "pre": None, "post": None, "deltas": None, "scope_warning": None}

    # 1. Pre-measure. Refuse BEFORE mutating on anything unmeasurable —
    # a mutation you can't measure is just `cmd`; say so.
    report("pre-capture...")
    pre = measure(sender, track, seconds=seconds, start_seconds=start_seconds,
                  output_dir=output_dir, keep_wav=keep_wav,
                  _analyzer_loader=_analyzer_loader)
    result["pre"] = pre
    if not pre.get("ok"):
        result["status"] = "REFUSED"
        result["exit_code"] = EXIT_MUTATION_FAILED
        result["note"] = ("pre-measure failed; nothing was mutated. If you "
                          "only want the change without measurement, use "
                          "`reaperd.py cmd` instead.")
        return result
    if pre.get("silent"):
        result["status"] = "REFUSED"
        result["exit_code"] = EXIT_MUTATION_FAILED
        result["note"] = ("pre-capture is SILENT "
                          f"({pre.get('silence_reason')}); nothing was "
                          "mutated. A mutation you can't measure is just "
                          "`cmd` — use that instead, or move the cursor/"
                          "time selection to where the track is playing.")
        return result
    pre_guid = (pre.get("track") or {}).get("guid")

    # 2. Mutate, through the same path `cmd` uses.
    report("mutating (post-capture pending — if this process dies now, the "
           "mutation stays applied; one Ctrl/Cmd+Z reverts it)...")
    reply = mutator(cmd_type, payload)
    result["mutation"]["reply"] = reply
    mut_ok = isinstance(reply, dict) and reply.get("ok")
    if not mut_ok:
        code = None
        if isinstance(reply, dict):
            code = (reply.get("error") or {}).get("code")
        if cmd_type == "batch":
            # The bridge keeps sub-commands that ran BEFORE the failure
            # applied, inside one closed undo block. "Nothing changed" would
            # be a lie; the project may be partially mutated and unmeasured.
            result["status"] = "UNVERIFIED"
            result["exit_code"] = EXIT_UNVERIFIED
            result["note"] = (
                f"batch failed ({code}): sub-commands that ran before the "
                "failure REMAIN APPLIED (the bridge keeps partial batch "
                "mutations inside one closed undo block). The project may be "
                "partially changed and was not re-measured. Do not retry "
                "blindly. One Ctrl/Cmd+Z reverts the whole batch.")
            return result
        if code in UNCERTAIN_MUTATION_CODES:
            result["status"] = "UNVERIFIED"
            result["exit_code"] = EXIT_UNVERIFIED
            result["note"] = (
                f"mutation outcome UNKNOWN ({code}): the command may have "
                "executed without a readable reply, or may still execute "
                "later. Do not retry blindly — check the track state (or "
                "undo history) first. If it did apply, it is NOT rolled back "
                "(one Ctrl/Cmd+Z); if it applies later, it will land "
                "unmeasured.")
            return result
        result["status"] = "MUTATION_FAILED"
        result["exit_code"] = EXIT_MUTATION_FAILED
        result["note"] = (
            f"the bridge rejected {cmd_type} ({code}); no post-capture "
            "attempted. A command that fails during resolution changes "
            "nothing; if the handler failed mid-edit, any partial change "
            "sits in one closed undo block — one Ctrl/Cmd+Z reverts it.")
        return result

    # From here on the mutation IS applied: any internal failure must come
    # back as an honest UNVERIFIED result, never as a crash whose exit code
    # an agent could read as "nothing was changed".
    try:
        # 3. Post-measure: SAME frozen bounds, and the track pinned by the
        # GUID the pre-capture resolved — a rename/reorder between captures
        # must not silently switch which track the post measures.
        report("mutation applied; post-capture...")
        post = measure(sender, track, output_dir=output_dir, keep_wav=keep_wav,
                       track_guid=pre_guid, _analyzer_loader=_analyzer_loader,
                       _bounds={"start_seconds": pre["bounds"]["start_seconds"],
                                "duration_seconds": pre["bounds"]["duration_seconds"],
                                "bounds_source": "frozen_from_pre"})
        result["post"] = post
        if not post.get("ok"):
            result["status"] = "UNVERIFIED"
            result["exit_code"] = EXIT_UNVERIFIED
            result["note"] = ("mutation applied but the post-capture failed "
                              f"({(post.get('error') or {}).get('code')}). "
                              + NOT_ROLLED_BACK)
            return result
        if post.get("silent"):
            result["status"] = "UNVERIFIED"
            result["exit_code"] = EXIT_UNVERIFIED
            result["note"] = ("mutation applied but the post-capture is SILENT "
                              f"({post.get('silence_reason')}); deltas would "
                              "compare signal against dead air. " + NOT_ROLLED_BACK)
            return result

        # Identity honesty: same track in both captures, or no verdict.
        post_guid = (post.get("track") or {}).get("guid")
        if pre_guid and post_guid and pre_guid != post_guid:
            result["status"] = "UNVERIFIED"
            result["exit_code"] = EXIT_UNVERIFIED
            result["note"] = ("mutation applied but the pre and post captures "
                              f"resolved DIFFERENT tracks (guid {pre_guid} vs "
                              f"{post_guid}); the comparison is contaminated. "
                              + NOT_ROLLED_BACK)
            return result
        # Scope honesty: unlike evidence gets no verdict. (Same-scope but
        # non-isolated captures stay VERIFIED with an explicit warning — the
        # comparison is like-for-like, it just describes the capture scope.)
        if pre.get("capture_scope") != post.get("capture_scope"):
            result["status"] = "UNVERIFIED"
            result["exit_code"] = EXIT_UNVERIFIED
            result["note"] = ("mutation applied but the capture scope changed "
                              f"({pre.get('capture_scope')} -> "
                              f"{post.get('capture_scope')}); pre and post are "
                              "unlike evidence and their difference is not a "
                              "measurement of this change. " + NOT_ROLLED_BACK)
            return result

        # 4. Verdict with measured deltas. ΔLUFS-I is always REPORTED (null
        # with a reason when REAPER did not supply LUFS), and a verdict
        # requires at least one broadband level delta to exist.
        deltas = compute_deltas(pre, post)
        if "lufs_i_delta" not in deltas:
            deltas["lufs_i_delta"] = None
            deltas["lufs_i_note"] = ("LUFS-I unavailable in pre and/or post "
                                     "capture (REAPER did not report it)")
        result["deltas"] = deltas
        if deltas.get("lufs_i_delta") is None and deltas.get("rms_db_delta") is None:
            result["status"] = "UNVERIFIED"
            result["exit_code"] = EXIT_UNVERIFIED
            result["note"] = ("mutation applied but no broadband level metric "
                              "(LUFS-I or RMS) is comparable between the "
                              "captures; there is no measured evidence to "
                              "report. " + NOT_ROLLED_BACK)
            return result
        result["scope_warning"] = _scope_warning(pre, post)
        result["status"] = "VERIFIED"
        result["exit_code"] = EXIT_VERIFIED
        return result
    except Exception as e:  # noqa: BLE001 — honest exit 2 beats a traceback
        result["status"] = "UNVERIFIED"
        result["exit_code"] = EXIT_UNVERIFIED
        result["note"] = ("mutation applied, then verify hit an internal "
                          f"error ({type(e).__name__}: {e}). " + NOT_ROLLED_BACK)
        return result


# ---------------------------------------------------------------------------
# Human-readable formatting (the CLI's default output)
# ---------------------------------------------------------------------------

def format_measure(m):
    """Short human table for a successful measure dict."""
    lines = []
    track = (m.get("track") or {}).get("name", "?")
    scope = m.get("capture_scope", "?")
    iso = "verified" if m.get("isolation_verified") else "NOT verified"
    lines.append(f"[measure] track={track}  scope={scope} ({iso})  "
                 f"source={m.get('metrics_source')}")
    b = m.get("bounds") or {}
    lines.append(f"[measure] bounds: start={b.get('start_seconds', 0):.3f}s  "
                 f"duration={b.get('duration_seconds', 0):.3f}s  "
                 f"(from {m.get('bounds_source', '?')})")
    lufs = m.get("lufs_i")
    parts = [f"LUFS-I: {lufs:.1f}" if lufs is not None else "LUFS-I: n/a"]
    if m.get("true_peak_db") is not None:
        parts.append(f"true peak: {m['true_peak_db']:.1f} dBTP")
    if m.get("sample_peak_db") is not None:
        parts.append(f"peak: {m['sample_peak_db']:.1f} dBFS")
    if m.get("rms_db") is not None:
        parts.append(f"RMS: {m['rms_db']:.1f} dBFS")
    if m.get("silence_fraction") is not None:
        parts.append(f"silence: {m['silence_fraction']:.0%}")
    lines.append("[measure] " + "   ".join(parts))
    if m.get("analysis_error"):
        lines.append(f"[measure] NOTE: Post Mortem analysis failed "
                     f"({m['analysis_error']}); metrics degraded to render_stats")
    if m.get("silent"):
        lines.append(f"[measure] WARNING: SILENT capture — {m.get('silence_reason')}. "
                     "No verdict should be built on this measurement.")
    if m.get("wav_kept") and m.get("file_path"):
        lines.append(f"[measure] WAV kept: {m['file_path']}")
    if scope != "isolated_track" or not m.get("isolation_verified"):
        lines.append("[measure] NOTE: these numbers describe the capture scope "
                     f"({scope}), not necessarily this track alone.")
    return "\n".join(lines)


def _level_summary(m):
    lufs = m.get("lufs_i")
    parts = [f"LUFS-I {lufs:.1f}" if lufs is not None else "LUFS-I n/a"]
    if m.get("rms_db") is not None:
        parts.append(f"RMS {m['rms_db']:.1f} dBFS")
    if m.get("true_peak_db") is not None:
        parts.append(f"true peak {m['true_peak_db']:.1f} dBTP")
    iso = "verified" if m.get("isolation_verified") else "NOT verified"
    parts.append(f"scope {m.get('capture_scope')} ({iso})")
    return " | ".join(parts)


def _kept_wav_line(tag, m):
    """Disclosure line for a WAV a failed capture kept on disk, or None."""
    for key in ("file_path", "output_file"):
        path = (m or {}).get(key)
        if path and os.path.isfile(path):
            return f"[verify] {tag} capture WAV kept for debugging: {path}"
    return None


def format_verify(res):
    """Human-readable report for a verify result dict."""
    lines = []
    pre, post = res.get("pre"), res.get("post")
    if pre and pre.get("ok"):
        lines.append(f"[verify] pre:  {_level_summary(pre)}")
    elif pre:
        err = pre.get("error") or {}
        lines.append(f"[verify] pre-measure FAILED — {err.get('code')}: "
                     f"{err.get('details')}")
        kept = _kept_wav_line("pre", pre)
        if kept:
            lines.append(kept)
    mut = res.get("mutation") or {}
    reply = mut.get("reply")
    if reply is not None:
        if isinstance(reply, dict) and reply.get("ok"):
            lines.append(f"[verify] mutation {mut.get('type')}: ok")
        else:
            err = (reply.get("error") if isinstance(reply, dict) else None) or reply
            lines.append(f"[verify] mutation {mut.get('type')}: FAILED — {err}")
    if post and post.get("ok"):
        lines.append(f"[verify] post: {_level_summary(post)}")
    elif post:
        err = post.get("error") or {}
        lines.append(f"[verify] post-measure FAILED — {err.get('code')}: "
                     f"{err.get('details')}")
        kept = _kept_wav_line("post", post)
        if kept:
            lines.append(kept)

    deltas = res.get("deltas") or {}
    delta_bits = []
    for key, label in (("lufs_i_delta", "dLUFS-I"),
                       ("true_peak_db_delta", "dTruePeak"),
                       ("rms_db_delta", "dRMS"),
                       ("sample_peak_db_delta", "dPeak"),
                       ("crest_factor_db_delta", "dCrest")):
        if deltas.get(key) is not None:
            delta_bits.append(f"{label} {deltas[key]:+.2f} dB"
                              if key != "lufs_i_delta"
                              else f"{label} {deltas[key]:+.2f}")
    if delta_bits:
        lines.append("[verify] " + "   ".join(delta_bits))
    stereo = deltas.get("stereo") or {}
    if stereo:
        lines.append("[verify] stereo deltas: " + ", ".join(
            f"{k[:-len('_delta')]} {v:+g}" for k, v in stereo.items()))
    bands = deltas.get("spectrum_band_deltas") or []
    moved = sorted((b for b in bands if b["delta_db"]),
                   key=lambda b: abs(b["delta_db"]), reverse=True)[:5]
    if moved:
        lines.append("[verify] biggest spectrum moves: " + ", ".join(
            f"{b['freq_hz']} Hz {b['delta_db']:+.1f} dB" for b in moved))
    if res.get("scope_warning"):
        lines.append(f"[verify] SCOPE: {res['scope_warning']}")
    verdict = f"[verify] VERDICT: {res.get('status')}"
    if res.get("note"):
        verdict += f" — {res['note']}"
    lines.append(verdict)
    return "\n".join(lines)
