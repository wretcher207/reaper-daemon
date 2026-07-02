#!/usr/bin/env python3
"""reaperd — cross-platform CLI for the Reaper Daemon REAPER agent bridge.

A single entry point that replaces the old per-task shell helpers. Works on
macOS, Linux, and Windows (run with `python3` on macOS/Linux, `python` on
Windows). The bridge itself is agent- and OS-agnostic; this CLI just reads and
writes JSON files in inbox/outbox and talks to REAPER's on-disk plugin cache.

Subcommands:
  send       send a command JSON file (optionally wait for the reply)
  cmd        send by <type> + <payload-json> (auto-resolves add_fx names,
             repairs set_fx_param field aliases)
  status     liveness check via the bridge heartbeat
  fxload     resolve an installed plugin name and add it to a track
  setparam   set any plugin parameter to a display value, with verify
  eq         set one EQ band (freq/gain/Q) and confirm it took
  groove     render a DSL drum beat and insert it onto a track
  jam        render a DSL beat from stdin onto the selected track
  list-maps  print the available drum-kit maps
  discover-map  probe a drum track's MIDI note names and propose a kit map
  add-map    save a drum-kit map to the user overlay (JSON or inline roles)
  remove-map remove a user-overlay drum-kit map

Run `python3 reaperd.py <subcommand> -h` for per-subcommand options.
"""

import argparse
import datetime
import glob
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import tempfile
import time

BRIDGE_ROOT = os.environ.get("REAPER_DAEMON_ROOT") or os.path.dirname(
    os.path.abspath(__file__)
)

BEGIN_MARKER = "-- >>> reaper-agent-bridge (managed) >>>"
END_MARKER = "-- <<< reaper-agent-bridge (managed) <<<"


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def find_resource_dir():
    """Locate REAPER's per-user resource directory on any OS."""
    env = os.environ.get("REAPER_RESOURCE_PATH")
    if env:
        return env
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/REAPER")
    if system == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
        return os.path.join(appdata, "REAPER")
    # Linux / other Unix: REAPER uses XDG_CONFIG_HOME/REAPER (default ~/.config/REAPER).
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(xdg, "REAPER")


def reaper_running():
    """Best-effort check that REAPER is running. Returns True/False/None
    (None = could not determine; fall back to the heartbeat)."""
    system = platform.system()
    try:
        if system == "Windows":
            r = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq REAPER.exe", "/NH"],
                capture_output=True, text=True, timeout=6,
            )
            return "REAPER.exe" in (r.stdout or "")
        if system == "Darwin":
            r = subprocess.run(["pgrep", "-x", "REAPER"], capture_output=True, timeout=6)
            return r.returncode == 0
        # Linux: the binary is lowercase `reaper`, and packaging varies (wine,
        # flatpak wrappers), so match both cases and treat a miss as UNKNOWN
        # rather than dead — the heartbeat is the authority there.
        r = subprocess.run(["pgrep", "-x", "REAPER|reaper"],
                           capture_output=True, timeout=6)
        return True if r.returncode == 0 else None
    except Exception:
        return None


def load_drum_config(bridge_root=None):
    """Read optional drum-config.json (user kit defaults). None if absent."""
    bridge_root = bridge_root or BRIDGE_ROOT
    path = os.path.join(bridge_root, "drum-config.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# FX name resolver (replaces the grep/sed pipeline in fxload.sh / send_cmd.sh)
# ---------------------------------------------------------------------------

# Trailing "Name (Vendor)" at end of a cache line, excluding junk prefix chars.
_FX_NAME_RE = re.compile(r"[^,|{=]+\([A-Za-z0-9 .&_-]+\)\s*$")


def resolve_fx_name(query, resource_dir=None):
    """Resolve a fuzzy plugin query to REAPER's exact listed name.

    Tokenises the query on non-alphanumerics (splitting letter<->digit
    boundaries too), then matches the resulting tokens in order with any
    separators between them against REAPER's VST/CLAP/AU plugin cache .ini
    files. Returns the shortest clean "Name (Vendor)" candidate, or None.
    """
    resource_dir = resource_dir or find_resource_dir()
    if not query or not os.path.isdir(resource_dir):
        return None
    q = re.sub(r"([A-Za-z])([0-9])", r"\1 \2", query)
    q = re.sub(r"([0-9])([A-Za-z])", r"\1 \2", q)
    tokens = re.findall(r"[A-Za-z0-9]+", q)
    if not tokens:
        return None
    pattern = re.compile(r"[^A-Za-z0-9]*".join(tokens), re.IGNORECASE)
    candidates = []
    for name_glob in ("reaper-vstplugins*.ini", "reaper-clap*.ini", "reaper-auplugins*.ini"):
        for fpath in glob.glob(os.path.join(resource_dir, name_glob)):
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if pattern.search(line):
                            m = _FX_NAME_RE.search(line)
                            if m:
                                cand = m.group().strip()
                                if cand:
                                    candidates.append(cand)
            except OSError:
                continue
    if not candidates:
        return None
    return min(candidates, key=len)


def repair_set_fx_param(payload):
    """Accept the obvious field names for the normalized value."""
    if "normalized_value" in payload:
        return payload
    for syn in ("value", "norm", "normalized"):
        if syn in payload:
            payload["normalized_value"] = payload[syn]
            del payload[syn]
            break
    return payload


# ---------------------------------------------------------------------------
# Core send / poll
# ---------------------------------------------------------------------------

def send_command(cmd, wait=False, timeout_ms=30000, bridge_root=None, verbose=False):
    """Write one command JSON atomically to inbox/, optionally poll outbox/.

    Returns (id, reply_text_or_None). Raises TimeoutError if --wait times out.
    Auto-fills id / version / created_at / created_by when absent.
    """
    bridge_root = bridge_root or BRIDGE_ROOT
    cmd = dict(cmd)
    cid = str(cmd.get("id", "")).strip()
    if not cid or cid == "<auto>":
        stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        cid = f"cli-{stamp}-{secrets.token_hex(8)}"
        cmd["id"] = cid
    cmd.setdefault("version", 3)
    cmd.setdefault("created_at", datetime.datetime.now().astimezone().isoformat())
    cmd.setdefault("created_by", "cli")
    cmd.setdefault("dry_run", False)
    token = _auth_token(bridge_root)
    if token:
        cmd.setdefault("token", token)

    inbox = os.path.join(bridge_root, "inbox", cid + ".json")
    outbox = os.path.join(bridge_root, "outbox", cid + ".json")
    os.makedirs(os.path.dirname(inbox), exist_ok=True)

    # A leftover reply with this id (fixed-id command file, or an unread reply
    # from a previous run) would be read back as THIS command's result.
    try:
        os.remove(outbox)
    except OSError:
        pass

    data = json.dumps(cmd, separators=(",", ":"))
    tmp = inbox + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, inbox)  # atomic rename; poller skips *.tmp
    if verbose:
        print(f"Sent command {cid}")
    if not wait:
        return cid, None

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if os.path.isfile(outbox):
            with open(outbox, "r", encoding="utf-8") as f:
                reply = f.read()
            # The reader owns its reply: the bridge no longer count-sweeps
            # outbox/ (it was deleting unread replies on big batches), so delete
            # it here once read to keep the queue from growing unbounded.
            try:
                os.remove(outbox)
            except OSError:
                pass
            return cid, reply
        time.sleep(0.05)
    # Withdraw the command so a TIMEOUT report stays true: a file left in
    # inbox/ (or requeued from processing/ at bridge startup) would still
    # execute later against whatever project is open then. If the bridge
    # already grabbed it, this remove misses; the bridge's requeue age gate
    # covers that side.
    try:
        os.remove(inbox)
    except OSError:
        pass
    raise TimeoutError(f"timed out after {timeout_ms}ms waiting for {outbox}")


def send_type(cmd_type, payload, bridge_root=None, timeout_ms=10000,
              resolve=True, repair=True, verbose=False):
    """Send a command by type + payload, wait for the reply, return parsed dict.

    On add_fx: resolves a fuzzy fx_name to the exact installed name.
    On set_fx_param: repairs value/norm/normalized -> normalized_value.
    Returns the parsed result JSON (always a dict; ok:false on timeout/bad reply).
    """
    payload = dict(payload)
    if resolve and cmd_type == "add_fx" and payload.get("fx_name"):
        name = resolve_fx_name(payload["fx_name"])
        if name:
            if verbose:
                print(f"[cmd] resolved {payload['fx_name']!r} -> {name}", file=sys.stderr)
            payload["fx_name"] = name
    if repair and cmd_type == "set_fx_param":
        payload = repair_set_fx_param(payload)
    cmd = {"version": 3, "type": cmd_type, "payload": payload}
    try:
        _cid, raw = send_command(cmd, wait=True, timeout_ms=timeout_ms,
                                 bridge_root=bridge_root, verbose=verbose)
    except TimeoutError as e:
        return {"ok": False, "error": {"code": "TIMEOUT", "details": str(e)}}
    if raw is None:
        return {"ok": False, "error": {"code": "NO_REPLY", "details": "no reply"}}
    try:
        return json.loads(raw)
    except Exception as e:
        return {"ok": False, "error": {"code": "BAD_REPLY", "details": str(e)}}


def scan_fx_parameters(base, bridge_root, include_values=False):
    """Every parameter of one FX, paginated past the bridge's 1000-per-reply
    cap (Kontakt-scale plugins) with the field the bridge actually reads
    (`limit`; `max_params` was silently ignored, hiding EQ bands past index
    200 on big plugins). Returns (params, None) or (None, error)."""
    payload = dict(base)
    if include_values:
        payload["include_values"] = True
    params, offset = [], 0
    while True:
        res = send_type("get_fx_parameters", {**payload, "limit": 1000, "offset": offset},
                        bridge_root=bridge_root, resolve=False, repair=False)
        if not res.get("ok"):
            return None, res.get("error")
        data = res.get("data", {})
        chunk = data.get("parameters", [])
        params.extend(chunk)
        if not data.get("has_more") or not chunk:
            return params, None
        offset += len(chunk)


# ---------------------------------------------------------------------------
# Small shared bits
# ---------------------------------------------------------------------------

def _num_from(s):
    """First (signed) number in a formatted string; handles kHz."""
    if s is None:
        return None
    text = str(s)
    if "inf" in text.lower():
        return 1e30 if "-" not in text.lower() else -1e30
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not m:
        return None
    v = float(m.group())
    if "khz" in text.lower():
        v *= 1000
    return v


def _exit_for(res):
    """Exit code from a parsed reply: 0 if ok, 1 otherwise."""
    return 0 if (isinstance(res, dict) and res.get("ok")) else 1


def _auth_token(bridge_root):
    """Optional shared secret from bridge_config.json. None if the user set none.
    The bridge reads the same key, so setting auth_token once wires both sides."""
    path = os.path.join(bridge_root, "bridge", "bridge_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (json.load(f).get("auth_token") or "").strip() or None
    except (OSError, ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_send(args):
    if not os.path.isfile(args.file):
        print(f"error: command JSON file not found: {args.file!r}", file=sys.stderr)
        return 1
    with open(args.file, "r", encoding="utf-8") as f:
        try:
            cmd = json.load(f)
        except Exception as e:
            print(f"error: could not parse {args.file}: {e}", file=sys.stderr)
            return 1
    try:
        cid, reply = send_command(cmd, wait=args.wait, timeout_ms=args.timeout,
                                  bridge_root=args.bridge_root, verbose=True)
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        print("       (is the bridge running? try: python3 reaperd.py status)",
              file=sys.stderr)
        return 1
    if reply is not None:
        print(reply)
        # The installer points users here to verify the bridge; exiting 0 on
        # an ok:false reply would report a broken command as success.
        try:
            res = json.loads(reply)
        except ValueError:
            print("error: reply is not valid JSON", file=sys.stderr)
            return 1
        return _exit_for(res)
    return 0


def cmd_cmd(args):
    try:
        payload = json.loads(args.payload)
    except Exception as e:
        print(f"error: payload is not valid JSON: {e}", file=sys.stderr)
        return 1
    res = send_type(args.type, payload, bridge_root=args.bridge_root,
                    timeout_ms=args.timeout, verbose=True)
    print(json.dumps(res, separators=(",", ":")))
    return _exit_for(res)


def cmd_status(args):
    ok = status_ok(args.bridge_root, quiet=args.quiet)
    return 0 if ok else 1


def status_ok(bridge_root=None, quiet=False):
    """True if the bridge heartbeat is fresh (or busy). Prints unless quiet."""
    bridge_root = bridge_root or BRIDGE_ROOT
    hb = os.path.join(bridge_root, "bridge", "heartbeat.json")

    def say(msg):
        if not quiet:
            print(msg)

    running = reaper_running()
    if running is False:
        say("DEAD: REAPER is not running. Launch REAPER (auto-start will load the bridge).")
        return False
    if not os.path.isfile(hb):
        if running:
            say("DEAD: REAPER is up but no heartbeat file. Bridge never loaded.")
        else:
            say("DEAD: no heartbeat file. Bridge never loaded (is REAPER running?).")
        say("      Fix: python3 setup/install.py, then relaunch REAPER.")
        return False
    age = time.time() - os.path.getmtime(hb)
    proj = alive = busy = "?"
    try:
        with open(hb, "r", encoding="utf-8") as f:
            d = json.load(f)
        proj = d.get("project_name", "?")
        alive = d.get("alive_at", "?")
        busy = d.get("busy") or "none"
    except Exception:
        pass
    fresh_secs = 15
    if age <= fresh_secs:
        say(f"CONNECTED: heartbeat {age:.0f}s old | project: {proj} | alive_at {alive}")
        return True
    if busy != "none":
        say(f"BUSY: {busy} in progress | heartbeat {age:.0f}s old | project: {proj}")
        say(f"      A synchronous {busy} blocks the loop on purpose; this is not a death.")
        return True
    say(f"STALE: heartbeat {age:.0f}s old (>{fresh_secs}s). The bridge loop has stopped.")
    say(f"       Last alive_at: {alive} | project: {proj}")
    say("       Revive: re-run the bridge action (Actions list), or relaunch REAPER so")
    say("       __startup.lua reloads it. Then re-run this check.")
    return False


def cmd_fxload(args):
    name = resolve_fx_name(args.query)
    if not name:
        print(f"NO_MATCH: nothing installed matching {args.query!r}", file=sys.stderr)
        return 1
    track = args.track or "master"
    print(f"[fxload] resolved {args.query!r} -> {name}  (track: {track})")
    res = send_type("add_fx", {"target_track_name": track, "fx_name": name, "show": False},
                    bridge_root=args.bridge_root, verbose=False)
    print(json.dumps(res, separators=(",", ":")))
    return _exit_for(res)


def cmd_setparam(args):
    br = args.bridge_root
    # 1. Resolve track -> guid (or master).
    if args.track == "master":
        base = {"target_track_name": "master"}
    else:
        ctx = send_type("get_context", {"include_fx": False}, bridge_root=br,
                        resolve=False, repair=False)
        if not ctx.get("ok"):
            print(f"[setparam] ERROR: get_context failed: {ctx.get('error')}",
                  file=sys.stderr)
            return 1
        guid = None
        for t in ctx.get("data", {}).get("tracks", []):
            if t.get("name") == args.track:
                guid = t.get("guid")
                break
        if not guid:
            print(f"[setparam] ERROR: track {args.track!r} not found", file=sys.stderr)
            return 1
        base = {"target_track_guid": guid}

    # 2. FX selector. A bare #index needs an explicit scope (bridge rejects an
    # ambiguous one); the CLI's #N always means a track FX.
    if args.fx.startswith("#"):
        base = {**base, "fx_index": int(args.fx[1:]), "fx_scope": "track"}
    else:
        base = {**base, "fx_name_contains": args.fx}

    # 3. Scan params.
    params, err = scan_fx_parameters(base, br)
    if err is not None:
        print(f"[setparam] ERROR: scan failed: {err}", file=sys.stderr)
        return 1

    # 4. Resolve param by #index or unique substring.
    if args.param.startswith("#"):
        idx = int(args.param[1:])
        match = next((p for p in params if p.get("index") == idx), None)
        if not match:
            print(f"[setparam] ERROR: param index {idx} out of range", file=sys.stderr)
            return 1
    else:
        q = args.param.lower()
        matches = [p for p in params if q in (p.get("name") or "").lower()]
        if not matches:
            print(f"[setparam] ERROR: no param matches {args.param!r} on this FX",
                  file=sys.stderr)
            print("[setparam] Available params:", file=sys.stderr)
            for p in params[:30]:
                print(f'  #{p.get("index")}  {p.get("name")} = {p.get("formatted_value")}',
                      file=sys.stderr)
            return 1
        if len(matches) > 1:
            print(f"[setparam] ERROR: {args.param!r} matched {len(matches)} params "
                  f"(ambiguous):", file=sys.stderr)
            for p in matches:
                print(f'  #{p.get("index")}  {p.get("name")} = {p.get("formatted_value")}',
                      file=sys.stderr)
            print("[setparam] Narrow with a longer substring, or use #<index>.",
                  file=sys.stderr)
            return 1
        match = matches[0]

    pidx = match.get("index")
    pname = match.get("name")
    before = match.get("formatted_value")
    print(f'[setparam] track={args.track}  fx={args.fx}  param=#{pidx} '
          f'{pname!r}  before={before}  target={args.value}')

    # 5. Set: norm= for direct normalized, else formatted_value (bridge searches).
    if args.value.startswith("norm="):
        payload = {**base, "param_index": pidx, "normalized_value": float(args.value[5:])}
    else:
        payload = {**base, "param_index": pidx, "formatted_value": args.value}
    res = send_type("set_fx_param", payload, bridge_root=br, resolve=False, repair=False)
    if not res.get("ok"):
        print(f"[setparam] ERROR: set failed: {res.get('error')}", file=sys.stderr)
        return 1

    # 6. Verify by re-reading the formatted value.
    params2, err2 = scan_fx_parameters(base, br)
    if err2 is not None:
        print(f"[setparam] ERROR: verify re-scan failed: {err2} "
              f"— set was sent but the landed value is UNVERIFIED.", file=sys.stderr)
        return 1
    after = None
    for p in params2:
        if p.get("index") == pidx:
            after = p.get("formatted_value")
            break
    print(f"[setparam] AFTER: {after}")
    if args.value.startswith("norm="):
        print(f"[setparam] RESULT: set {pname} to normalized {args.value[5:]} "
              f"(displays as {after})")
        return 0
    return _judge_landed(pname, args.value, after)


def _judge_landed(pname, target, after):
    """Exit code for a formatted-value set: 0 when the landed display is on
    target (or close), 1 on a real miss. Split out so the tolerance logic is
    testable without a live bridge."""
    tn = _num_from(target)
    an = _num_from(after)
    if tn is None:
        # Enum/string target ("Bell", "Off"): no tolerance math; compare text.
        if after is not None and str(after).strip().lower() == str(target).strip().lower():
            print(f"[setparam] RESULT: OK — {pname} = {after}")
            return 0
        print(f"[setparam] RESULT: MISSED — {pname} displays {after!r}, "
              f"target was {target!r}.", file=sys.stderr)
        return 1
    if an is None:
        print(f"[setparam] RESULT: SET but display is non-numeric ({after}) "
              f"— UNVERIFIED, re-scan before trusting it.", file=sys.stderr)
        return 1
    diff = abs(an - tn)
    if diff <= max(abs(tn) * 0.02, 0.5):
        print(f"[setparam] RESULT: OK — {pname} = {after} (target was {target})")
        return 0
    if diff <= max(abs(tn) * 0.10, 1.0):
        print(f"[setparam] RESULT: CLOSE — {pname} = {after} (target was {target}; "
              f"re-run or use norm= for exact)")
        return 0
    print(f"[setparam] RESULT: MISSED — {pname} = {after}, target was {target} "
          f"(>10% off). NOT claiming success.", file=sys.stderr)
    return 1


def cmd_eq(args):
    br = args.bridge_root
    if args.fx.startswith("#"):
        base = {"target_track_name": args.track, "fx_index": int(args.fx[1:]), "fx_scope": "track"}
    else:
        base = {"target_track_name": args.track, "fx_name_contains": args.fx}

    params, err = scan_fx_parameters(base, br, include_values=True)
    if err is not None:
        print(f"[eqband] FAILED reading params: {err}", file=sys.stderr)
        print("[eqband] (AMBIGUOUS_FX = duplicate instances; target one with #0 / #1)",
              file=sys.stderr)
        return 1

    # Discover this band's param indices by name across EQ naming conventions.
    band = args.band
    band_re = re.compile(r"(^|\b)(eq\s*)?band\s*0*%d\b" % band)
    band_re2 = re.compile(r"\s*0*%d\s*[:\-]" % band)
    # Enable-param naming varies: ReaEQ "Enabled", FabFilter Pro-Q "On"/"Used",
    # others expose only an inverted "Bypass", and some bands are always-on with
    # no enable param at all. Detect any of them; require only Freq+Gain.
    roles = [("enable", r"\b(used|enabled?|on|active)\b"),
             ("bypass", r"\bbypass\b"),
             ("freq", r"\b(freq(uency)?)\b"),
             ("gain", r"\bgain\b"), ("q", r"\b(q|bandwidth|width)\b")]
    idx = {}
    for p in params:
        n = (p.get("name") or "").strip().lower()
        if not (band_re.search(n) or band_re2.match(n)):
            continue
        for key, pat in roles:
            if key not in idx and re.search(pat, n):
                idx[key] = p.get("index")
    ifreq, igain, iq = idx.get("freq"), idx.get("gain"), idx.get("q")
    ienable, enable_inverted = idx.get("enable"), False
    if ienable is None and idx.get("bypass") is not None:
        ienable, enable_inverted = idx.get("bypass"), True  # 0.0 = not bypassed = live
    if ifreq is None or igain is None:
        print(f"[eqband] could not find Band {band} Frequency/Gain params. "
              f"This EQ names bands differently.", file=sys.stderr)
        return 1

    def setn(i, v):
        return send_type("set_fx_param", {**base, "param_index": i, "normalized_value": v},
                         bridge_root=br, resolve=False, repair=False).get("ok", False)

    def setfmt(i, display):
        return send_type("set_fx_param", {**base, "param_index": i, "formatted_value": display},
                         bridge_root=br, resolve=False, repair=False).get("ok", False)

    # Enable the band FIRST (else the curve stays flat), then dial freq/gain/Q.
    # Every set's reply matters: a dead bridge mid-sequence must fail loudly,
    # not fall through to a success banner.
    sets = []
    if ienable is not None:
        sets.append(("enable", setn(ienable, 0.0 if enable_inverted else 1.0)))
    sets.append(("freq", setfmt(ifreq, f"{args.freq} Hz")))
    sets.append(("gain", setfmt(igain, f"{args.gain} dB")))
    if args.q is not None and iq is not None:
        sets.append(("q", setfmt(iq, str(args.q))))
    failed = [name for name, ok in sets if not ok]
    if failed:
        print(f"[eqband] FAILED: set did not take for: {', '.join(failed)}. "
              f"Band {band} is NOT verified — do NOT claim success.", file=sys.stderr)
        return 1

    # Verify: read the band back and report real values.
    final, ferr = scan_fx_parameters(base, br, include_values=True)
    if ferr is not None:
        print(f"[eqband] FAILED verify re-scan: {ferr}. Sets were "
              f"acknowledged but the band is UNVERIFIED.", file=sys.stderr)
        return 1
    pmap = {p.get("index"): p for p in final}

    def fv(i):
        p = pmap.get(i)
        return p.get("formatted_value", p.get("value")) if p else "?"

    line = f"[eqband] Band {band} ->"
    if ienable is not None:
        line += f"  {'Bypass' if enable_inverted else 'Enabled'}={fv(ienable)}"
    line += f"  Freq={fv(ifreq)}  Gain={fv(igain)}"
    if iq is not None:
        line += f"  Q={fv(iq)}"
    print(line)
    if ienable is None:
        # Always-on band: no enable param exists. All sets returned ok and the
        # re-scan read the values back, so "live" rests on that evidence.
        live = True
    elif enable_inverted:
        live = True  # bypass display is ambiguous; the un-bypass set returned ok
    else:
        live = str(fv(ienable)).strip().lower() in ("used", "on", "enabled", "active", "1", "true", "yes")
    print("[eqband] RESULT:",
          "BAND IS LIVE on the curve." if live
          else "BAND DID NOT TAKE (enable is off) — do NOT claim success.")
    return 0 if live else 1


def cmd_groove(args):
    br = args.bridge_root
    if not os.path.isfile(args.dsl):
        print(f"[groove] ERROR: DSL file not found: {args.dsl}", file=sys.stderr)
        return 1
    cfg = load_drum_config(br) or {}
    track = args.track or cfg.get("track")
    cfg_map = args.map or cfg.get("map")

    # Position: empty = edit cursor; a value forces that time in seconds.
    if args.position is not None:
        pos = {"type": "time", "seconds": float(args.position)}
    else:
        pos = {"type": "cursor"}

    groovegen = os.path.join(br, "skills", "drum-apparatus", "groovegen.py")
    if not os.path.isfile(groovegen):
        print(f"[groove] ERROR: drum engine not found at {groovegen}", file=sys.stderr)
        return 1

    midi = tempfile.NamedTemporaryFile(suffix=".mid", delete=False).name
    gen = [sys.executable, groovegen, "--dsl", args.dsl, "--out", midi]
    if cfg_map:
        gen += ["--map", cfg_map]
    if args.seed is not None:
        gen += ["--seed", str(args.seed)]
    print(f"[groove] Rendering DSL: {args.dsl}")
    r = subprocess.run(gen, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr or r.stdout, file=sys.stderr)
        try:
            os.unlink(midi)
        except OSError:
            pass
        return 1
    if r.stdout:
        print(r.stdout.rstrip())

    if track:
        payload = {"target_track_name": track, "midi_path": midi, "position": pos}
    else:
        payload = {"use_selected_track": True, "midi_path": midi, "position": pos}
    if args.tempo is not None:
        payload["project_tempo"] = args.tempo
    res = send_type("insert_midi_file", payload, bridge_root=br,
                    timeout_ms=20000, resolve=False, repair=False)
    try:
        os.unlink(midi)
    except OSError:
        pass
    if not res.get("ok"):
        print(f"[groove] FAILED: {res.get('error')}", file=sys.stderr)
        return 1
    tname = res.get("data", {}).get("track", {}).get("name", "track")
    print(f"[groove] OK: inserted on {tname} at "
          f"{args.position if args.position is not None else 'cursor'}s")
    return 0


def cmd_jam(args):
    if not status_ok(args.bridge_root, quiet=False):
        print("[jam] REAPER is DOWN — relaunch it, nothing inserted.", file=sys.stderr)
        return 1
    text = sys.stdin.read()
    if not text.strip():
        print("[jam] ERROR: no DSL on stdin", file=sys.stderr)
        return 1
    tmp = tempfile.NamedTemporaryFile(suffix=".dsl", delete=False, mode="w",
                                      encoding="utf-8")
    tmp.write(text)
    tmp.close()
    # jam always uses the SELECTED track — never guesses a track.
    jam_args = argparse.Namespace(
        bridge_root=args.bridge_root, dsl=tmp.name, track=None,
        position=None, tempo=None, seed=None, map=None)
    rc = cmd_groove(jam_args)
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    return rc


def cmd_riff(args):
    """Read a guitar stem's transients into a proposed kick grid (David's step 1).

    Reads the SAVED .rpp on disk, parses the named guitar track's audio item,
    detects onsets, and prints the kick grid at 100/50/30% attack strength. It's
    a PROPOSAL David corrects (the percentile is an attack-strength heuristic,
    not true open-vs-muted) — transcribe the row you like into a groove DSL.
    """
    if not os.path.isfile(args.project):
        print(f"[riff] ERROR: project not found: {args.project}", file=sys.stderr)
        return 1
    skill_dir = os.path.join(args.bridge_root, "skills", "drum-apparatus")
    cmd = [sys.executable, "-m", "drumgen.riff", args.project, args.track,
           str(args.bars), str(args.start_bar)]
    r = subprocess.run(cmd, cwd=skill_dir, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout.rstrip())
    if r.returncode != 0:
        print(r.stderr.rstrip() or "[riff] failed to read the project", file=sys.stderr)
        return 1
    print("\n[riff] Proposal — David corrects it. Transcribe a kick row into a DSL, "
          "then: reaperd.py groove <dsl> --track <name>")
    return 0


def cmd_list_maps(args):
    skill_dir = os.path.join(args.bridge_root, "skills", "drum-apparatus")
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    try:
        from drumgen.catalog import load_maps  # noqa: E402
    except Exception as e:
        print(f"error: could not load drum maps: {e}", file=sys.stderr)
        return 1
    for name in sorted(load_maps().keys()):
        print(name)
    return 0


OVERLAY_DIR_NAME = "maps"  # sibling of catalog/, under skills/drum-apparatus/


def _overlay_dir(bridge_root):
    return os.path.join(bridge_root, "skills", "drum-apparatus", OVERLAY_DIR_NAME)


def _skill_path(bridge_root):
    p = os.path.join(bridge_root, "skills", "drum-apparatus")
    return p, p in sys.path or sys.path.insert(0, p)


def cmd_discover_map(args):
    br = args.bridge_root
    skill_dir, _ = _skill_path(br)
    try:
        from drumgen.mapdetect import match_roles, format_report  # noqa: E402
    except Exception as e:
        print(f"error: could not load mapdetect: {e}", file=sys.stderr)
        return 1

    channels = [int(c) for c in args.channels.split(",") if c.strip() != ""]
    payload = {"target_track_name": args.track, "channels": channels,
               "max_pitch": args.max_pitch}
    if args.guid:
        payload = {"target_track_guid": args.guid, "channels": channels,
                   "max_pitch": args.max_pitch}
    res = send_type("discover_drum_map", payload, bridge_root=br,
                    timeout_ms=15000, resolve=False, repair=False)
    if not res.get("ok"):
        print(f"[discover-map] FAILED: {res.get('error')}", file=sys.stderr)
        return 1
    data = res.get("data", {})
    notes_raw = data.get("notes", {})
    # flatten {"36": {name, channel}} -> {36: name} for the matcher
    notes = {}
    for p, info in notes_raw.items():
        try:
            notes[int(p)] = info.get("name") if isinstance(info, dict) else info
        except (TypeError, ValueError):
            continue
    print("[discover-map] track: %s  fx: %s" % (
        data.get("track", {}).get("name", "?"),
        ", ".join(data.get("fx", [])) or "(none)"))
    if not data.get("has_note_names"):
        print("[discover-map] No MIDI note names on this track.")
        print("[discover-map] The drum library did not install a .midnam, so there")
        print("[discover-map] is nothing to auto-discover. Options:")
        print("[discover-map]   - load the kit's .midnam into REAPER (track piano roll ->")
        print("[discover-map]     Note names -> load), then re-run; or")
        print("[discover-map]   - build the map by hand: reaperd.py add-map <name>")
        print("[discover-map]     (see reaperd.py add-map -h)")
        return 2

    map_dict, report = match_roles(notes)
    print(format_report(notes, report, map_dict))
    if args.save:
        _save_overlay_map(br, args.save, map_dict)
        print("")
        print(f"[discover-map] Saved map '{args.save}' to the user overlay.")
        print(f"[discover-map] Use it with: @map {args.save}  (or --map {args.save})")
    elif report["complete"]:
        print("")
        print("[discover-map] Looks good. Save it with --save <name> to use in the DSL.")
    return 0 if report["complete"] else 2


def _save_overlay_map(bridge_root, name, role_map):
    odir = _overlay_dir(bridge_root)
    os.makedirs(odir, exist_ok=True)
    path = os.path.join(odir, name + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(role_map, separators=(",", ":")))
    os.replace(tmp, path)
    return path


def cmd_add_map(args):
    br = args.bridge_root
    # Source: a JSON file, or inline roles on the command line.
    role_map = None
    if args.file:
        if not os.path.isfile(args.file):
            print(f"error: map file not found: {args.file}", file=sys.stderr)
            return 1
        with open(args.file, "r", encoding="utf-8") as f:
            try:
                role_map = json.load(f)
            except Exception as e:
                print(f"error: could not parse {args.file}: {e}", file=sys.stderr)
                return 1
    elif args.roles:
        try:
            role_map = json.loads(args.roles)
        except Exception as e:
            print(f"error: --roles is not valid JSON: {e}", file=sys.stderr)
            return 1
    elif not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            try:
                role_map = json.loads(text)
            except Exception as e:
                print(f"error: stdin is not valid JSON: {e}", file=sys.stderr)
                return 1
    if not isinstance(role_map, dict):
        print("error: no map provided. Use --file, --roles, or pipe JSON on stdin.",
              file=sys.stderr)
        return 1
    # Accept either {name: {role:pitch}} or bare {role:pitch}.
    if len(role_map) == 1 and isinstance(next(iter(role_map.values())), dict):
        name, role_map = next(iter(role_map.items()))
    else:
        name = args.name
    if not name:
        print("error: map name required (positional arg or a single-key file).",
              file=sys.stderr)
        return 1
    # Validate roles are integers; warn on unknown roles.
    skill_dir, _ = _skill_path(br)
    try:
        from drumgen.catalog import ROLE_KEYS  # noqa: E402
    except Exception:
        ROLE_KEYS = []
    known = set(ROLE_KEYS)
    cleaned = {}
    for k, v in role_map.items():
        try:
            cleaned[k] = int(v)
        except (TypeError, ValueError):
            print(f"warning: skipping non-integer role {k!r} = {v!r}", file=sys.stderr)
        if known and k not in known:
            print(f"warning: {k!r} is not a known groovekit role", file=sys.stderr)
    path = _save_overlay_map(br, name, cleaned)
    print(f"[add-map] Saved {len(cleaned)} roles as '{name}' -> {path}")
    print(f"[add-map] Use it with: @map {name}  (or reaperd.py groove ... --map {name})")
    return 0


def cmd_remove_map(args):
    odir = _overlay_dir(args.bridge_root)
    path = os.path.join(odir, args.name + ".json")
    if not os.path.isfile(path):
        print(f"error: no user map named {args.name!r} in {odir}", file=sys.stderr)
        return 1
    os.replace(path, path + ".trash")
    try:
        os.unlink(path + ".trash")
    except OSError:
        pass
    print(f"[remove-map] Removed '{args.name}'.")
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="reaperd",
        description="Cross-platform CLI for the Reaper Daemon REAPER agent bridge.",
    )
    p.add_argument("--bridge-root", default=BRIDGE_ROOT,
                   help=f"bridge root (default: {BRIDGE_ROOT}; env REAPER_DAEMON_ROOT)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("send", help="send a command JSON file")
    s.add_argument("file", help="path to a command JSON file")
    s.add_argument("--wait", action="store_true", help="poll for the reply")
    s.add_argument("--timeout", type=int, default=30000, help="reply timeout in ms")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("cmd", help="send by <type> + <payload-json>")
    s.add_argument("type", help="command type (get_context, add_fx, ...)")
    s.add_argument("payload", help="payload as a JSON string")
    s.add_argument("--timeout", type=int, default=10000, help="reply timeout in ms")
    s.set_defaults(func=cmd_cmd)

    s = sub.add_parser("status", help="bridge liveness check")
    s.add_argument("--quiet", action="store_true", help="exit code only")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("fxload", help="resolve an installed plugin name and add it")
    s.add_argument("query", help="plugin name query (fuzzy)")
    s.add_argument("track", nargs="?", default="master",
                   help="track name or 'master' (default: master)")
    s.set_defaults(func=cmd_fxload)

    s = sub.add_parser("setparam", help="set any plugin parameter, with verify")
    s.add_argument("track", help="track name or 'master'")
    s.add_argument("fx", help="FX substring or #index")
    s.add_argument("param", help="param substring or #index")
    s.add_argument("value", help='display value ("80 Hz") or norm=0..1')
    s.set_defaults(func=cmd_setparam)

    s = sub.add_parser("eq", help="set one EQ band (freq Hz, gain dB, [Q])")
    s.add_argument("track", help="track name")
    s.add_argument("fx", help="FX substring or #index")
    s.add_argument("band", type=int, help="band number")
    s.add_argument("freq", help="frequency in Hz")
    s.add_argument("gain", help="gain in dB")
    s.add_argument("q", nargs="?", default=None, help="Q (optional)")
    s.set_defaults(func=cmd_eq)

    s = sub.add_parser("groove", help="render a DSL drum beat and insert it")
    s.add_argument("dsl", help="path to a DSL file")
    s.add_argument("--track", default=None, help="track name (default: selected track)")
    s.add_argument("--position", type=float, default=None,
                   help="time in seconds (default: edit cursor)")
    s.add_argument("--tempo", type=int, default=None, help="project tempo override")
    s.add_argument("--seed", type=int, default=None, help="RNG seed")
    s.add_argument("--map", default=None, help="drum-kit map (default: GM Standard)")
    s.set_defaults(func=cmd_groove)

    s = sub.add_parser("jam", help="render a DSL beat from stdin onto the selected track")
    s.set_defaults(func=cmd_jam)

    s = sub.add_parser("riff",
                       help="read a guitar stem's transients into a proposed kick grid")
    s.add_argument("project", help="path to the saved .rpp project file")
    s.add_argument("track", help="name of the guitar track to read")
    s.add_argument("--bars", type=int, default=4, help="bars to read (default 4)")
    s.add_argument("--start-bar", type=int, default=0,
                   help="first bar to read, 0-indexed (default 0)")
    s.set_defaults(func=cmd_riff)

    s = sub.add_parser("list-maps", help="print available drum-kit maps")
    s.set_defaults(func=cmd_list_maps)

    s = sub.add_parser("discover-map",
                       help="probe a drum track's MIDI note names and propose a kit map")
    s.add_argument("track", help="track name (or use --guid)")
    s.add_argument("--guid", default=None, help="track GUID instead of name")
    s.add_argument("--channels", default="0",
                   help="comma-separated MIDI channels to scan (default: 0)")
    s.add_argument("--max-pitch", type=int, default=127,
                   help="highest MIDI pitch to probe (default: 127)")
    s.add_argument("--save", default=None,
                   help="save the proposed map to the user overlay under this name")
    s.set_defaults(func=cmd_discover_map)

    s = sub.add_parser("add-map", help="save a drum-kit map to the user overlay")
    s.add_argument("name", nargs="?", default=None,
                   help="map name (required unless the JSON file is single-key)")
    s.add_argument("--file", default=None, help="path to a map JSON file")
    s.add_argument("--roles", default=None,
                   help='inline JSON roles, e.g. \'{"KICK_R":36,"SNARE":38}\'')
    s.set_defaults(func=cmd_add_map)

    s = sub.add_parser("remove-map", help="remove a user-overlay drum-kit map")
    s.add_argument("name", help="map name to remove")
    s.set_defaults(func=cmd_remove_map)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
