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
# Default reply timeout for ordinary (non-capture) sends. Shared contract:
# reaperd._bridge_sender and reaper_mcp._verify_sender default to this.
DEFAULT_SENDER_TIMEOUT_MS = 10000

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


def _as_dict(value):
    """value when it is a dict, else {}. Reply `data` and its sub-objects
    are bridge-controlled: a wrong-shaped one must read as absent and fail
    closed through the normal error paths, never crash into a traceback."""
    return value if isinstance(value, dict) else {}


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
    data = _as_dict(ctx.get("data"))
    ts = _as_dict(data.get("time_selection"))
    if ts.get("active"):
        try:
            start = float(ts.get("start", 0))
            end = float(ts.get("end", 0))
        except (TypeError, ValueError):
            return _error("BOUNDS_UNRESOLVED",
                          "active time selection has non-numeric bounds "
                          f"(start={ts.get('start')!r}, "
                          f"end={ts.get('end')!r})")
        length = end - start
        if length <= 0:
            return _error("BOUNDS_UNRESOLVED",
                          "active time selection has non-positive length")
        if length < 1.0:
            # The same 1 s floor measure enforces on explicit seconds: a
            # stray tiny selection must not silently produce verdicts on
            # windows the module's own validation deems too short.
            return _error("BOUNDS_UNRESOLVED",
                          f"active time selection is {length:g} s long, "
                          "shorter than the 1 s minimum capture duration; "
                          "move or clear the selection, or pass an explicit "
                          "start")
        return {"ok": True, "start_seconds": start,
                "duration_seconds": min(float(seconds), length),
                "bounds_source": "time_selection"}
    cursor = _as_dict(data.get("cursor")).get("seconds")
    if cursor is None:
        return _error("BOUNDS_UNRESOLVED", "get_context reply has no cursor position")
    try:
        cursor = float(cursor)
    except (TypeError, ValueError):
        return _error("BOUNDS_UNRESOLVED",
                      f"get_context cursor position is not a number ({cursor!r})")
    return {"ok": True, "start_seconds": cursor,
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
    pdata = _as_dict(pre.get("data"))
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
    data = _as_dict(res.get("data"))
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
    if not same_file:
        try:
            mtime = os.path.getmtime(file_path)
        except OSError as e:
            # The file vanished between the isfile check and the stat: its
            # freshness can no longer be proven — same refusal as stale.
            return _error("STALE_CAPTURE_FILE",
                          f"{file_path} vanished before its freshness could "
                          f"be checked ({e}); refusing to trust it",
                          file_path=file_path)
        if mtime < sent_at:
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
                            "STALE_REPLY_LOCKED", "MUTATOR_EXCEPTION"}


def _error_code(reply):
    """The reply's error code as a clean non-empty string, else None. A
    malformed code (empty, non-string, unhashable) must classify as
    outcome-unknown — never crash a set-membership test or masquerade as a
    provable clean rejection."""
    err = reply.get("error") if isinstance(reply, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    if isinstance(code, str) and code.strip():
        return code.strip()
    return None


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
    1 REFUSED — the mutation command was never sent (pre-measure failed,
      silent pre-capture, or no pinnable GUID; only read/capture traffic
      reached the bridge). Only here is "nothing was mutated" honest.
    2 UNVERIFIED — the mutation was SENT and the outcome is not a verified
      measurement: applied-but-unmeasured, partially applied, rejected by
      the bridge (a handler that failed mid-edit can leave a partial change
      inside one closed undo block, indistinguishable from a clean
      resolution rejection from this side), or transport-unknown. Nothing
      is rolled back. Do not retry blindly on exit 2.
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
    if not pre_guid:
        # Fail closed BEFORE mutating: without the pre-capture's GUID the
        # post-capture cannot be pinned to the same track, so a rename during
        # the mutation could silently switch what "post" measures.
        result["status"] = "REFUSED"
        result["exit_code"] = EXIT_MUTATION_FAILED
        result["note"] = ("pre-capture reply carries no track GUID (older "
                          "bridge?); the post-capture could not be pinned to "
                          "the same track, so no trustworthy comparison is "
                          "possible. Nothing was mutated. Update the bridge, "
                          "or use `cmd` for an unmeasured change.")
        return result

    # 2. Mutate, through the same path `cmd` uses. A mutator exception is
    # outcome-unknown, not "nothing happened": the command may already be in
    # the inbox.
    report("mutating (post-capture pending — if this process dies now, the "
           "mutation stays applied; one Ctrl/Cmd+Z reverts it)...")
    try:
        reply = mutator(cmd_type, payload)
    except Exception as e:  # noqa: BLE001
        reply = {"ok": False, "error": {"code": "MUTATOR_EXCEPTION",
                                        "details": f"{type(e).__name__}: {e}"}}
    result["mutation"]["reply"] = reply

    def _uncertain(code_text):
        result["status"] = "UNVERIFIED"
        result["exit_code"] = EXIT_UNVERIFIED
        result["note"] = (
            f"mutation outcome UNKNOWN ({code_text}): the command may have "
            "executed without a readable reply, or may still execute "
            "later. Do not retry blindly — check the track state (or "
            "undo history) first. If it did apply, it is NOT rolled back "
            "(one Ctrl/Cmd+Z); if it applies later, it will land "
            "unmeasured.")
        return result

    if not isinstance(reply, dict):
        # A non-object reply (raw null, a bare string...) is a wrong-shaped
        # transport result AFTER the command was queued: outcome unknown.
        return _uncertain(f"malformed reply: {reply!r}")
    batch_partial_note = None
    if reply.get("ok") is True and cmd_type == "batch":
        # stop_on_error:false lets the bridge return top-level ok:true while
        # individual sub-commands failed. That is NOT the requested change.
        # The reply must carry one well-formed result PER requested command,
        # or completion cannot be proven at all (outcome unknown).
        data_obj = reply.get("data")
        results_list = (data_obj.get("results")
                        if isinstance(data_obj, dict) else None)
        expected = len((payload or {}).get("commands") or []) \
            if isinstance(payload, dict) else 0
        well_formed = (isinstance(results_list, list)
                       and len(results_list) == expected
                       and all(isinstance(r, dict) and "ok" in r
                               for r in results_list))
        if not well_formed:
            return _uncertain(
                "batch reply lacks a well-formed result for every "
                "sub-command; completion cannot be proven")
        failed = [r for r in results_list if r.get("ok") is not True]
        if failed:
            what = ", ".join(f"#{r.get('index')} {r.get('type')}"
                             for r in failed[:6])
            batch_partial_note = (
                f"batch applied PARTIALLY: {len(failed)} sub-command(s) "
                f"failed ({what}) while the rest ran. The measured deltas "
                "describe this partially-applied state, NOT the requested "
                "change. One Ctrl/Cmd+Z reverts the whole batch.")
            result["mutation"]["partial_failures"] = [
                {"index": r.get("index"), "type": r.get("type")}
                for r in failed]
    if reply.get("ok") is not True:
        code = _error_code(reply)
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
        if code is None or code in UNCERTAIN_MUTATION_CODES:
            # A missing/wrong-shaped error block or code is version skew,
            # not a provable clean rejection — outcome unknown.
            return _uncertain(
                code or f"wrong-shaped error field: {reply.get('error')!r}")
        # A readable rejection code is still NOT proof of a clean project:
        # resolution-stage rejections (NO_TARGET_TRACK, AMBIGUOUS_FX, ...)
        # change nothing, but a handler that threw mid-edit leaves a partial
        # change inside one closed undo block, and this side cannot tell the
        # two apart without trusting a code audit of whatever bridge version
        # happens to be installed. Exit 1's "nothing was changed" promise is
        # therefore reserved for refusals BEFORE the mutation was sent; every
        # post-send rejection fails toward exit 2.
        result["status"] = "UNVERIFIED"
        result["exit_code"] = EXIT_UNVERIFIED
        result["mutation"]["rejection_code"] = code
        result["note"] = (
            f"the bridge rejected {cmd_type} ({code}); no post-capture "
            "attempted. A rejection during resolution changes nothing, but "
            "if the handler failed mid-edit a partial change sits in one "
            "closed undo block, and that cannot be told apart from here, "
            "so the project MAY have changed. Do not retry blindly; one "
            "Ctrl/Cmd+Z reverts any partial change.")
        return result

    def _unote(text):
        # A detected partial batch must be disclosed on EVERY later outcome,
        # not only the happy path (post-capture failures included).
        if batch_partial_note:
            return text + " ALSO: " + batch_partial_note
        return text

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
            result["note"] = _unote("mutation applied but the post-capture "
                                    "failed "
                                    f"({(post.get('error') or {}).get('code')}). "
                                    + NOT_ROLLED_BACK)
            return result
        if post.get("silent"):
            result["status"] = "UNVERIFIED"
            result["exit_code"] = EXIT_UNVERIFIED
            result["note"] = _unote("mutation applied but the post-capture is "
                                    f"SILENT ({post.get('silence_reason')}); "
                                    "deltas would compare signal against dead "
                                    "air. " + NOT_ROLLED_BACK)
            return result

        # Identity honesty: same track in both captures, or no verdict. A
        # missing post GUID cannot "confirm by omission" — fail closed.
        post_guid = (post.get("track") or {}).get("guid")
        if not post_guid or pre_guid != post_guid:
            result["status"] = "UNVERIFIED"
            result["exit_code"] = EXIT_UNVERIFIED
            result["note"] = (("mutation applied but the post-capture did not "
                               "confirm the track identity (no GUID in its "
                               "reply); the comparison cannot be trusted. ")
                              if not post_guid else
                              ("mutation applied but the pre and post captures "
                               f"resolved DIFFERENT tracks (guid {pre_guid} vs "
                               f"{post_guid}); the comparison is contaminated. ")
                              ) + NOT_ROLLED_BACK
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
        if batch_partial_note:
            # Measured deltas exist, but they describe a PARTIALLY applied
            # batch — exit 0 would tell an agent the whole batch succeeded.
            result["status"] = "UNVERIFIED"
            result["exit_code"] = EXIT_UNVERIFIED
            result["note"] = batch_partial_note
            return result
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
# tune_param: outcome-driven parameter search
# ---------------------------------------------------------------------------

TUNE_MAX_ITERATIONS = 5   # each iteration is a render; hard cap by spec
TUNE_METRICS = ("lufs_i", "band_db")


# Floor for the band power-sum: levels so low the power terms underflow to
# zero read as this instead of crashing log10(0). Far below any real signal.
BAND_ENERGY_FLOOR_DB = -400.0


def band_energy_db(spectrum, band_hz):
    """Power-sum of the 1/3-octave bands whose center lies in [lo, hi] Hz.
    Arithmetic over Post Mortem's already-computed band levels — no new DSP.
    None when no band center falls in the range or no level is finite;
    BAND_ENERGY_FLOOR_DB when the selected levels underflow the sum to 0."""
    lo, hi = band_hz
    levels = []
    for band in spectrum or []:
        if not isinstance(band, dict):
            continue
        freq, level = band.get("freq_hz"), band.get("level_db")
        if (isinstance(freq, (int, float)) and not isinstance(freq, bool)
                and lo <= freq <= hi
                and isinstance(level, (int, float))
                and not isinstance(level, bool) and math.isfinite(level)):
            levels.append(level)
    if not levels:
        return None
    total = sum(10.0 ** (db / 10.0) for db in levels)
    if total <= 0.0:
        return BAND_ENERGY_FLOOR_DB
    return round(max(10.0 * math.log10(total), BAND_ENERGY_FLOOR_DB), 2)


def _tune_metric(m, target):
    """Extract the target metric's value from a measure dict, or None."""
    if target["metric"] == "lufs_i":
        value = m.get("lufs_i")
        return (value if isinstance(value, (int, float))
                and not isinstance(value, bool) else None)
    return band_energy_db(m.get("spectrum_third_octave"), target["band_hz"])


def _resolve_tune_param(scanner, base, param_selector):
    """Resolve the target parameter once: (pin, identity, param, None) or
    (None, None, None, error_dict). pin is the exact selector every set uses
    (track GUID + fx index/scope + param index) so renames and chain edits
    mid-tune cannot retarget the sets."""
    data, err = scanner(base)
    if err is not None:
        return None, None, None, _error(err.get("code") or "SCAN_FAILED",
                                        f"parameter scan failed: {err}")
    params = data.get("parameters") or []
    if "param_index" in param_selector:
        idx = param_selector["param_index"]
        matches = [p for p in params if p.get("index") == idx]
    else:
        query = str(param_selector.get("param_name_contains", "")).lower()
        matches = [p for p in params if query in (p.get("name") or "").lower()]
    if not matches:
        return None, None, None, _error(
            "NO_PARAM", f"no parameter matches {param_selector!r}")
    if len(matches) > 1:
        names = ", ".join(f"#{p.get('index')} {p.get('name')}" for p in matches[:8])
        return None, None, None, _error(
            "AMBIGUOUS_PARAM",
            f"{param_selector!r} matches {len(matches)} parameters ({names}); "
            "narrow the selector or use param_index")
    param = matches[0]
    track_id = data.get("track") or {}
    fx_id = data.get("fx") or {}
    pin = {"param_index": param.get("index")}
    if track_id.get("guid"):
        pin["target_track_guid"] = track_id["guid"]
    if fx_id.get("index") is not None and fx_id.get("scope"):
        pin["fx_index"] = fx_id["index"]
        pin["fx_scope"] = fx_id["scope"]
    return pin, {"track": track_id, "fx": fx_id}, param, None


def tune_param(sender, scanner, track, fx_selector, param_selector, target,
               seconds=None, start_seconds=None, output_dir=None,
               progress=None, _analyzer_loader=_load_analyzer):
    """Search a parameter's normalized value until a MEASURED audio outcome
    hits the target, or report honestly why it could not.

    scanner: callable(base_payload) -> (data, error) in the shape of
    reaperd.scan_fx_parameter_data (identity + full parameter list).
    target: {"metric": "lufs_i"|"band_db", "delta": float,
             "tolerance": float (default 0.5), "band_hz": [lo, hi]} —
    delta is relative to the pre-measure baseline.

    Safety/honesty contract:
    - The track is pinned by GUID, and the FX identity (GUID) plus parameter
      name are RE-VERIFIED before every set — a chain edit mid-tune aborts
      with IDENTITY_CHANGED instead of retargeting another plugin (the
      bridge's set command cannot target an FX GUID directly, so the pinned
      index is proven to still mean the same plugin before each use).
    - Per-track claims need per-track evidence: the baseline must be a
      verified isolated_track capture (master_output is allowed for the
      master); a scope change mid-tune aborts with SCOPE_CHANGED.
    - The reported `final` state is READ BACK from the live project after
      the search, never assumed from replies.
    - ASSUMPTION (documented, enforced): the metric is monotone in the
      normalized value — true for gain-like params. NON_MONOTONE (a midpoint
      falling outside its bracket) aborts and restores the initial value.
      A flat metric is monotone but useless: that reports UNREACHABLE.

    Every iteration is one render (hard cap 5 after the baseline, so up to
    6 renders total). Each set_fx_param lands as its own undo point; values
    are deliberately left applied between iterations. On UNCONVERGED /
    UNREACHABLE the best-observed value is left set and reported.
    """
    def report(msg):
        if progress is not None:
            progress(msg)

    # -- validate the target ------------------------------------------------
    if not isinstance(target, dict) or target.get("metric") not in TUNE_METRICS:
        return _error("BAD_TARGET",
                      f"target.metric must be one of {TUNE_METRICS}")
    target = dict(target)
    delta = target.get("delta")
    if not isinstance(delta, (int, float)) or isinstance(delta, bool) \
            or not math.isfinite(delta) or delta == 0:
        return _error("BAD_TARGET", "target.delta must be a nonzero finite number")
    tolerance = target.get("tolerance", 0.5)
    if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) \
            or not math.isfinite(tolerance) or tolerance <= 0:
        return _error("BAD_TARGET", "target.tolerance must be a positive number")
    target["tolerance"] = tolerance
    if target["metric"] == "band_db":
        band = target.get("band_hz")
        if (not isinstance(band, (list, tuple)) or len(band) != 2
                or not all(isinstance(v, (int, float)) and math.isfinite(v)
                           for v in band) or not band[0] < band[1]):
            return _error("BAD_TARGET",
                          "band_db needs band_hz: [low, high] with low < high")
        target["band_hz"] = [float(band[0]), float(band[1])]

    # -- baseline measure ---------------------------------------------------
    report("baseline capture...")
    pre = measure(sender, track, seconds=seconds, start_seconds=start_seconds,
                  output_dir=output_dir, _analyzer_loader=_analyzer_loader)
    if not pre.get("ok"):
        return {"status": "REFUSED", "pre": pre,
                "note": "baseline measure failed; nothing was changed."}
    if pre.get("silent"):
        return {"status": "REFUSED", "pre": pre,
                "note": ("baseline capture is SILENT "
                         f"({pre.get('silence_reason')}); tuning against dead "
                         "air is meaningless. Nothing was changed.")}
    baseline_scope = pre.get("capture_scope")
    scope_ok = (baseline_scope == "master_output"
                or (baseline_scope == "isolated_track"
                    and pre.get("isolation_verified")))
    if not scope_ok:
        return {"status": "REFUSED", "pre": pre,
                "note": (f"baseline capture scope is {baseline_scope!r} "
                         "(isolation not verified): tuning would optimize "
                         "against the whole capture scope while claiming a "
                         "per-track outcome. Nothing was changed.")}
    track_guid = (pre.get("track") or {}).get("guid")
    if not track_guid:
        return {"status": "REFUSED", "pre": pre,
                "note": ("pre-capture reply carries no track GUID; the "
                         "iteration captures could not be pinned to the same "
                         "track. Nothing was changed.")}
    if target["metric"] == "band_db" and pre.get("metrics_source") != "postmortem":
        return {"status": "REFUSED", "pre": pre,
                "note": ("band_db needs Post Mortem's spectrum analysis, "
                         "which is not available; only lufs_i works without "
                         "it. Nothing was changed.")}
    baseline = _tune_metric(pre, target)
    if baseline is None:
        return {"status": "REFUSED", "pre": pre,
                "note": (f"metric {target['metric']} is unavailable in the "
                         "baseline capture; cannot tune against it. Nothing "
                         "was changed.")}
    goal = baseline + delta
    frozen = {"start_seconds": pre["bounds"]["start_seconds"],
              "duration_seconds": pre["bounds"]["duration_seconds"],
              "bounds_source": "frozen_from_pre"}

    # -- resolve the parameter once, pin it ---------------------------------
    base = {"target_track_guid": track_guid}
    base.update(fx_selector or {})
    pin, identity, param, err = _resolve_tune_param(scanner, base, param_selector or {})
    if err is not None:
        err["status"] = "REFUSED"
        err["note"] = "parameter did not resolve; nothing was changed."
        return err
    fx_guid = (identity.get("fx") or {}).get("guid")
    if not fx_guid or "fx_index" not in pin:
        return {"status": "REFUSED",
                "note": ("the FX reply carries no GUID/scoped index to pin "
                         "its identity; a chain edit mid-tune could not be "
                         "detected. Nothing was changed (fail closed per the "
                         "stable-identity contract).")}
    param_name = param.get("name")
    n0 = param.get("normalized_value")
    if not isinstance(n0, (int, float)) or not 0.0 <= n0 <= 1.0:
        return {"status": "REFUSED",
                "note": f"parameter has no usable normalized_value ({n0!r}); "
                        "nothing was changed."}
    # The pinned scanner base re-resolves by scoped index; the GUID check in
    # _identity_check proves that index still means the same plugin.
    base_pinned = {"target_track_guid": track_guid,
                   "fx_index": pin["fx_index"], "fx_scope": pin["fx_scope"]}

    result = {"status": None, "target": target, "baseline": baseline,
              "target_value": round(goal, 2), "pre": pre, "param": {
                  **identity, "param_index": param.get("index"),
                  "param_name": param_name,
                  "initial_normalized": n0,
                  "initial_formatted": param.get("formatted_value")},
              "metrics_source": pre.get("metrics_source"),
              "iterations": [{"normalized": n0, "metric": baseline,
                              "source": "baseline"}]}
    observations = [(n0, baseline)]
    applied = {"n": n0}
    # Measurement slack: below this, disagreement is noise, not evidence.
    slack = max(0.1, tolerance / 2.0)
    iterations_used = 0

    def _identity_check():
        """None when the pinned FX identity still holds, else a failure dict."""
        data, scan_err = scanner(dict(base_pinned))
        if scan_err is not None:
            return {"status": "IDENTITY_CHANGED",
                    "note": ("could not re-verify the target FX before "
                             f"setting ({scan_err}); stopping rather than "
                             "setting blind.")}
        fx = data.get("fx") or {}
        if fx.get("guid") != fx_guid:
            return {"status": "IDENTITY_CHANGED",
                    "note": ("the FX at the pinned chain position changed "
                             f"(guid {fx.get('guid')!r}, expected {fx_guid!r})"
                             " — an FX-chain edit mid-tune; stopping rather "
                             "than setting a different plugin.")}
        live = next((p for p in data.get("parameters") or []
                     if p.get("index") == pin["param_index"]), None)
        if live is None or (param_name and (live.get("name") or "") != param_name):
            return {"status": "IDENTITY_CHANGED",
                    "note": (f"parameter #{pin['param_index']} is no longer "
                             f"{param_name!r}; stopping rather than setting "
                             "the wrong parameter.")}
        return None

    def _read_back():
        """(normalized, formatted, error_text) for the parameter's LIVE
        state — the authority for `final`, never assumed from replies."""
        data, scan_err = scanner(dict(base_pinned))
        if scan_err is not None:
            code = scan_err.get("code") if isinstance(scan_err, dict) else scan_err
            return None, None, f"final state could not be re-read ({code})"
        fx = data.get("fx") or {}
        if fx.get("guid") != fx_guid:
            return None, None, ("final read-back found a DIFFERENT FX at the "
                                "pinned position")
        for p in data.get("parameters") or []:
            if p.get("index") == pin["param_index"]:
                return p.get("normalized_value"), p.get("formatted_value"), None
        return None, None, "parameter missing from the final read-back"

    def finish(status, note):
        # Restore policy: UNCONVERGED/UNREACHABLE -> best observed;
        # NON_MONOTONE -> initial (the relationship is untrustworthy);
        # everything else leaves the last applied value.
        restore_n = restore_v = why = None
        if status in ("UNCONVERGED", "UNREACHABLE"):
            measured = [(n, v) for n, v in observations if v is not None]
            if measured:
                restore_n, restore_v = min(measured,
                                           key=lambda o: abs(o[1] - goal))
                why = "best observed"
        elif status == "NON_MONOTONE":
            restore_n, restore_v, why = n0, baseline, "initial"
        if restore_n is not None and abs(restore_n - applied["n"]) > 1e-12:
            id_fail = _identity_check()
            if id_fail is not None:
                # The stable-identity contract covers the restore write too:
                # never set a parameter on a retargeted FX.
                note += (f" Re-applying the {why} value was SKIPPED "
                         "(IDENTITY_CHANGED): " + id_fail["note"])
            else:
                set_reply = sender("set_fx_param",
                                   {**pin, "normalized_value": restore_n})
                if isinstance(set_reply, dict) and set_reply.get("ok"):
                    applied["n"] = restore_n
                    note += (f" The {why} value (normalized {restore_n:.4f}, "
                             f"metric {restore_v:.2f}) was re-applied.")
                else:
                    code = _error_code(set_reply)
                    if code is None or code in UNCERTAIN_MUTATION_CODES:
                        note += (f" Re-applying the {why} value got no "
                                 f"readable reply ({code}): whether it took "
                                 "is UNKNOWN: trust the read-back below, "
                                 "not this note.")
                    else:
                        note += (f" Re-applying the {why} value was rejected "
                                 f"({code}); the last set value should "
                                 "remain.")
        rb_norm, rb_fmt, rb_err = _read_back()
        final = {"normalized": rb_norm, "formatted_value": rb_fmt,
                 "read_back": rb_err is None}
        if rb_err:
            note += (f" {rb_err}; the final parameter state is UNVERIFIED "
                     "(last known applied value: normalized "
                     f"{applied['n']:.4f}).")
            final["normalized"] = None
            final["last_known_normalized"] = round(applied["n"], 6)
        metric_at_final = None
        if final["normalized"] is not None:
            for n, v in reversed(observations):
                if v is not None and abs(n - final["normalized"]) <= 5e-3:
                    metric_at_final = v
                    break
        final["metric"] = metric_at_final
        final["delta_achieved"] = (round(metric_at_final - baseline, 2)
                                   if metric_at_final is not None else None)
        final["error_from_target"] = (round(metric_at_final - goal, 2)
                                      if metric_at_final is not None else None)
        result.update({"status": status, "note": note,
                       "iterations_used": iterations_used, "final": final})
        return result

    # -- already there? -----------------------------------------------------
    if abs(baseline - goal) <= tolerance:
        return finish("CONVERGED",
                      "the baseline is already within tolerance of the "
                      "target; nothing was changed (0 iterations).")

    def set_and_measure(n):
        """Returns (metric_value, None) or (None, failure_dict)."""
        nonlocal iterations_used
        fail = _identity_check()
        if fail:
            return None, fail
        iterations_used += 1
        report(f"iteration {iterations_used}/{TUNE_MAX_ITERATIONS}: "
               f"normalized={n:.4f}, rendering...")
        set_reply = sender("set_fx_param", {**pin, "normalized_value": n})
        if not (isinstance(set_reply, dict) and set_reply.get("ok")):
            code = _error_code(set_reply)
            if code is None or code in UNCERTAIN_MUTATION_CODES:
                note = (f"iteration {iterations_used}'s set got no readable "
                        f"reply ({code}): whether it applied is UNKNOWN.")
            else:
                note = (f"set_fx_param was rejected on iteration "
                        f"{iterations_used} ({code}).")
            return None, {"status": "SET_FAILED",
                          "note": note + " Stopping; `final` reads the "
                                         "actual parameter state back."}
        applied["n"] = n
        m = measure(sender, track, output_dir=output_dir,
                    track_guid=track_guid, _analyzer_loader=_analyzer_loader,
                    _bounds=frozen)
        if not m.get("ok") or m.get("silent"):
            why = (m.get("error") or {}).get("code") if not m.get("ok") \
                else f"silent ({m.get('silence_reason')})"
            result["iterations"].append({"normalized": round(n, 6),
                                         "metric": None})
            return None, {"status": "MEASURE_FAILED",
                          "note": f"iteration {iterations_used} capture "
                                  f"unusable ({why}). Stopping; `final` reads "
                                  "the actual parameter state back."}
        if (m.get("capture_scope") != baseline_scope
                or (baseline_scope == "isolated_track"
                    and not m.get("isolation_verified"))):
            result["iterations"].append({"normalized": round(n, 6),
                                         "metric": None})
            return None, {"status": "SCOPE_CHANGED",
                          "note": (f"iteration {iterations_used}'s capture "
                                   f"scope became {m.get('capture_scope')!r} "
                                   f"(baseline was {baseline_scope!r}); the "
                                   "per-track evidence is gone. Stopping.")}
        if (m.get("track") or {}).get("guid") != track_guid:
            result["iterations"].append({"normalized": round(n, 6),
                                         "metric": None})
            return None, {"status": "IDENTITY_CHANGED",
                          "note": (f"iteration {iterations_used}'s capture "
                                   "resolved a different track; stopping.")}
        value = _tune_metric(m, target)
        if value is None:
            result["iterations"].append({"normalized": round(n, 6),
                                         "metric": None})
            return None, {"status": "MEASURE_FAILED",
                          "note": f"metric {target['metric']} vanished from "
                                  f"iteration {iterations_used}'s capture. "
                                  "Stopping; `final` reads the actual "
                                  "parameter state back."}
        observations.append((n, value))
        result["iterations"].append({"normalized": round(n, 6),
                                     "metric": value})
        return value, None

    # -- direction probe ----------------------------------------------------
    # Assume gain-like (metric increases with normalized) and probe the
    # boundary on the target's side; a contradicted or flat probe gets ONE
    # flip to the other boundary (starting AT a boundary uses the flip
    # immediately). Probe results are direction DETECTION — NON_MONOTONE is
    # only ever declared from a bracket violation during bisection.
    #
    # From the first set onward, any internal failure must come back as an
    # honest result that still runs the restore policy and the final
    # read-back — never a crash that leaves a mutation applied and
    # undisclosed (mirrors verify's post-mutation guard).
    try:
        primary = 1.0 if delta > 0 else 0.0
        b = primary if abs(primary - n0) > 1e-9 else 1.0 - primary
        flipped = abs(primary - n0) <= 1e-9
        m_b = None
        probe_results = []
        while True:
            if iterations_used >= TUNE_MAX_ITERATIONS:
                return finish("UNCONVERGED",
                              "iteration budget exhausted during the "
                              "direction probe.")
            m_b, failure = set_and_measure(b)
            if failure:
                return finish(failure["status"], failure["note"])
            probe_results.append((b, m_b))
            if abs(m_b - goal) <= tolerance:
                return finish("CONVERGED",
                              f"target reached at normalized {b:g}.")
            if (goal - baseline) * (goal - m_b) <= 0:
                break  # goal bracketed between baseline and this boundary
            progressed = abs(m_b - goal) < abs(baseline - goal) - 1e-12
            flip_target = 1.0 - b
            if (not progressed and not flipped
                    and abs(flip_target - n0) > 1e-9):
                flipped = True
                b = flip_target
                continue
            if abs(m_b - baseline) <= slack:
                moved = [p for p in probe_results
                         if abs(p[1] - baseline) > slack]
                if moved:
                    # An earlier probe DID move the metric (away from the
                    # target); "does not move anywhere" would contradict
                    # the recorded iterations. Report BOTH probes.
                    desc = ", ".join(
                        f"{'up' if pn > n0 else 'down'}: "
                        f"{pm - baseline:+.2f}"
                        for pn, pm in probe_results)
                    return finish("UNREACHABLE",
                                  "neither probed direction approaches the "
                                  f"target for {target['metric']} ({desc} "
                                  "vs baseline); unreachable with this "
                                  "parameter.")
                return finish("UNREACHABLE",
                              f"the parameter does not move "
                              f"{target['metric']} anywhere in its range "
                              f"(full-range change {m_b - baseline:+.2f}, "
                              "within noise); a flat metric is monotone but "
                              "the target is unreachable with this "
                              "parameter.")
            if progressed:
                return finish("UNREACHABLE",
                              f"the target ({goal:.2f}) lies beyond what "
                              f"this parameter can reach (its extreme gives "
                              f"{m_b:.2f}).")
            return finish("UNREACHABLE",
                          f"every probed direction moves {target['metric']} "
                          f"away from the target (baseline {baseline:.2f} "
                          "is the closest achievable); unreachable with "
                          "this parameter.")

        # -- bisection inside the bracket -----------------------------------
        lo_n, lo_m = n0, baseline
        hi_n, hi_m = b, m_b
        while iterations_used < TUNE_MAX_ITERATIONS:
            mid_n = (lo_n + hi_n) / 2.0
            mid_m, failure = set_and_measure(mid_n)
            if failure:
                return finish(failure["status"], failure["note"])
            low, high = min(lo_m, hi_m), max(lo_m, hi_m)
            if not (low - slack <= mid_m <= high + slack):
                return finish("NON_MONOTONE",
                              f"the metric at normalized {mid_n:.4f} "
                              f"({mid_m:.2f}) fell outside its bracket "
                              f"[{low:.2f}, {high:.2f}] (plus noise); the "
                              "parameter is not monotone here. Refusing to "
                              "thrash.")
            if abs(mid_m - goal) <= tolerance:
                return finish("CONVERGED",
                              f"target hit within tolerance after "
                              f"{iterations_used} iteration(s).")
            if (mid_m < goal) == (lo_m < goal):
                lo_n, lo_m = mid_n, mid_m
            else:
                hi_n, hi_m = mid_n, mid_m
        return finish("UNCONVERGED",
                      f"not within tolerance after {TUNE_MAX_ITERATIONS} "
                      "iterations.")
    except Exception as e:  # noqa: BLE001 — honest result beats a traceback
        note = ("tune_param hit an internal error "
                f"({type(e).__name__}: {e}) after {iterations_used} "
                "iteration(s).")
        if abs(applied["n"] - n0) > 1e-12:
            note += (f" A set_fx_param mutation IS applied (normalized "
                     f"{applied['n']:.4f}, initially {n0:.4f}) and is NOT "
                     "rolled back; one Ctrl/Cmd+Z per applied set reverts "
                     "it.")
        else:
            note += " No applied value differs from the initial state."
        try:
            return finish("INTERNAL_ERROR", note)
        except Exception:  # noqa: BLE001 — finish() itself may be poisoned
            result.update({
                "status": "INTERNAL_ERROR", "note": note,
                "iterations_used": iterations_used,
                "final": {"normalized": None, "formatted_value": None,
                          "metric": None, "delta_achieved": None,
                          "error_from_target": None, "read_back": False,
                          "last_known_normalized": round(applied["n"], 6)}})
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
        partial = mut.get("partial_failures")
        if isinstance(reply, dict) and reply.get("ok") and partial:
            lines.append(f"[verify] mutation {mut.get('type')}: PARTIAL — "
                         f"{len(partial)} sub-command(s) failed: " + ", ".join(
                             f"#{f.get('index')} {f.get('type')}"
                             for f in partial))
        elif isinstance(reply, dict) and reply.get("ok"):
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
        elif key == "lufs_i_delta" and key in deltas:
            # ΔLUFS-I is always reported — n/a with its reason, never
            # silently dropped from the human report.
            delta_bits.append("dLUFS-I n/a")
    if delta_bits:
        lines.append("[verify] " + "   ".join(delta_bits))
    if deltas.get("lufs_i_delta") is None and deltas.get("lufs_i_note"):
        lines.append(f"[verify] NOTE: {deltas['lufs_i_note']}")
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
