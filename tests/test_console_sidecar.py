"""Tests for console_sidecar.

Split deliberately in two. The pure section drives plain functions with no
threads, no subprocesses and no clock: the line normalizer, the cost differ,
the lock verdict, the dead-man predicate and the budget gate are pure for the
same reason the bridge's `lock_verdict` is (reaper_agent_bridge.lua:234) —
so the rules can be tested without standing up a daemon.

The integration section drives a real child process (tests/console_fakes.py)
with poll_interval, deadman_seconds and stdin_timeout injected at 10 ms, so a
300 second production timeout is exercised in a third of a second.
"""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import console_sidecar as cs  # noqa: E402
from console_fakes import (  # noqa: E402
    assistant_text, init_event, result, text_delta, tool_result, tool_use,
    write_fake_claude)


# ---------------------------------------------------------------------------
# Fixtures. conftest's `root` creates only inbox/outbox/processing/bridge, so
# the console tree needs its own.
# ---------------------------------------------------------------------------

@pytest.fixture
def console_root(tmp_path):
    for name in ("inbox", "outbox", "processing", "bridge", "logs",
                 "console", "console/prompts", "console/control",
                 "console/events", "console/raw", "console/audio"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    write_heartbeat(str(tmp_path))
    return str(tmp_path)


def write_heartbeat(root, busy=None, project="test.rpp", age=0.0):
    path = os.path.join(root, "bridge", "heartbeat.json")
    body = {"alive_at": cs.utc_iso(), "project_name": project,
            "bridge_version": 3}
    if busy:
        body["busy"] = busy
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def sidecars():
    """Every sidecar a test spawns gets its child killed, pass or fail."""
    made = []
    yield made
    for sidecar in made:
        try:
            sidecar._stopping.set()
            sidecar.kill_child(reason="test-teardown")
        except Exception:  # noqa: BLE001
            pass


def make_sidecar(sidecars, root, scenario, **kwargs):
    argv, paths = write_fake_claude(os.path.join(root, "fake"), scenario)
    options = dict(claude_argv=argv, take_lock=False, poll_interval=0.01,
                   deadman_seconds=0.05, stdin_timeout=2.0, turn_timeout=10.0,
                   bridge_cache_seconds=0.0)
    options.update(kwargs)
    sidecar = cs.ConsoleSidecar(root=root, **options)
    sidecars.append(sidecar)
    sidecar.fake_paths = paths
    return sidecar


def read_events(sidecar):
    with open(sidecar.events_path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def wait_for(predicate, timeout=10.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ===========================================================================
# Pure: line splitting
# ===========================================================================

def test_line_splitter_carries_a_partial_line_across_chunks():
    splitter = cs.LineSplitter()
    assert splitter.feed(b'{"a":1}\n{"b') == ['{"a":1}']
    assert splitter.feed(b'":2}\n') == ['{"b":2}']


def test_line_splitter_survives_a_split_utf8_sequence():
    splitter = cs.LineSplitter()
    blob = "café\n".encode("utf-8")
    assert splitter.feed(blob[:4]) == []
    assert splitter.feed(blob[4:]) == ["café"]


def test_line_splitter_flush_returns_an_unterminated_tail():
    splitter = cs.LineSplitter()
    splitter.feed(b"no newline here")
    assert splitter.flush() == ["no newline here"]
    assert splitter.flush() == []


def test_line_splitter_strips_carriage_returns():
    assert cs.LineSplitter().feed(b"x\r\n") == ["x"]


# ===========================================================================
# Pure: cost
# ===========================================================================

def test_turn_cost_is_a_difference_not_a_sum():
    # total_cost_usd is CUMULATIVE for the process. Summing three results at
    # 0.27 / 0.41 / 0.55 would report $1.23 for a session that cost $0.55.
    assert cs.turn_cost_delta(0.0, 0.27) == pytest.approx(0.27)
    assert cs.turn_cost_delta(0.27, 0.41) == pytest.approx(0.14)
    assert cs.turn_cost_delta(0.41, 0.55) == pytest.approx(0.14)


def test_turn_cost_delta_handles_a_respawned_child_counter():
    # A --resume respawn restarts the child's cumulative counter at zero; a
    # negative delta must never be reported as a refund.
    assert cs.turn_cost_delta(1.20, 0.05) == pytest.approx(0.05)


def test_turn_cost_delta_tolerates_a_missing_total():
    assert cs.turn_cost_delta(0.10, None) == 0.0
    assert cs.turn_cost_delta(None, None) == 0.0


# ===========================================================================
# Pure: normalization
# ===========================================================================

def test_text_delta_becomes_a_streaming_event():
    events = cs.normalize_stream_object(text_delta("hel"))
    assert [e["t"] for e in events] == ["text_delta"]
    assert events[0]["text"] == "hel"


def test_assistant_message_splits_into_text_and_tool_events():
    obj = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "Renaming the track."},
        {"type": "tool_use", "id": "t1", "name": "mcp__reaper-daemon__track",
         "input": {"action": "rename"}}]}}
    events = cs.normalize_stream_object(obj)
    assert [e["t"] for e in events] == ["thinking", "text", "tool"]
    assert events[2]["name"] == "mcp__reaper-daemon__track"
    assert json.loads(events[2]["input"]) == {"action": "rename"}


def test_empty_assistant_text_is_dropped():
    assert cs.normalize_stream_object(assistant_text("   ")) == []


def test_unknown_stream_types_are_ignored_not_leaked():
    # Anything the CLI adds later must not reach a Lua parser as raw JSON.
    assert cs.normalize_stream_object({"type": "something_new", "x": 1}) == []
    assert cs.normalize_stream_object("not a dict") == []


def test_tool_result_unverified_is_neither_ok_nor_not_ok():
    # verifyloop.py:435-437. UNVERIFIED means the mutation WAS sent and nothing
    # was measured. Collapsing it into ok:true is what AGENTS.md:189-192 forbids.
    events = cs.normalize_stream_object(
        tool_result("t1", {"status": "UNVERIFIED", "message": "post-capture silent"}))
    assert events[0]["verdict"] == "unverified"
    assert events[0]["ok"] is None


def test_tool_result_refused_is_a_clean_failure():
    events = cs.normalize_stream_object(tool_result("t1", {"status": "REFUSED"}))
    assert events[0]["verdict"] == "refused"
    assert events[0]["ok"] is False


def test_tool_result_verified_is_ok():
    events = cs.normalize_stream_object(tool_result("t1", {"status": "VERIFIED"}))
    assert (events[0]["verdict"], events[0]["ok"]) == ("verified", True)


def test_tool_result_error_flag_wins_over_a_plain_body():
    events = cs.normalize_stream_object(
        tool_result("t1", {"ok": False, "error": {"code": "NO_TARGET_TRACK"}},
                    is_error=True))
    assert (events[0]["verdict"], events[0]["ok"]) == ("error", False)


def test_measurement_fields_survive_verbatim():
    # These four change what the numbers MEAN, so they are never folded into a
    # summary string (AGENTS.md:194-198).
    payload = {"ok": True, "silent": False, "capture_scope": "full_mix",
               "isolation_verified": False, "metrics_source": "render_stats"}
    events = cs.normalize_stream_object(tool_result("t1", payload))
    assert events[0]["measurement"] == {
        "silent": False, "capture_scope": "full_mix",
        "isolation_verified": False, "metrics_source": "render_stats"}


def test_measurement_fields_from_pre_and_post_are_kept_separately():
    payload = {"status": "VERIFIED",
               "pre": {"silent": False, "capture_scope": "isolated_track"},
               "post": {"silent": True, "capture_scope": "isolated_track"}}
    events = cs.normalize_stream_object(tool_result("t1", payload))
    assert events[0]["measurement"]["post"]["silent"] is True


def test_long_tool_result_is_capped_for_the_ui_thread():
    # One scan_fx result is a single multi-hundred-KB line and the panel parses
    # on REAPER's UI thread.
    events = cs.normalize_stream_object(tool_result("t1", "x" * 300000), cap=2048)
    assert len(events[0]["text"]) < 2200
    assert "full text in console/raw/" in events[0]["text"]


def test_result_event_carries_the_differenced_turn_cost():
    events = cs.normalize_stream_object(result(0.41), prev_total_cost=0.27)
    assert events[0]["t"] == "turn_end"
    assert events[0]["turn_cost_usd"] == pytest.approx(0.14)
    assert events[0]["total_cost_usd"] == pytest.approx(0.41)


def test_aborted_streaming_is_a_cancellation_not_an_error():
    # An interrupt stops the turn mid-stream and a result STILL arrives. Showing
    # it in red would make every Stop press look like a crash.
    events = cs.normalize_stream_object(
        result(0.1, subtype="error_during_execution",
               terminal_reason="aborted_streaming", is_error=True))
    assert events[0]["cancelled"] is True
    assert events[0]["error"] is False


def test_budget_exhaustion_is_flagged_distinctly():
    events = cs.normalize_stream_object(
        result(9.9, subtype="error_max_budget_usd", is_error=True))
    assert events[0]["budget_exhausted"] is True
    assert events[0]["error"] is True


# ===========================================================================
# Pure: lock verdict
# ===========================================================================

def test_lock_verdict_no_lock_proceeds():
    assert cs.lock_verdict(None, time.time(), lambda pid: True) is None


def test_lock_verdict_live_pid_refuses():
    lock = {"pid": 4242, "started": time.time() - 3600, "owner": "a"}
    verdict = cs.lock_verdict(lock, time.time(), lambda pid: True)
    assert verdict and "live pid 4242" in verdict


def test_lock_verdict_dead_pid_is_reclaimable():
    lock = {"pid": 4242, "started": time.time() - 3600, "owner": "a"}
    assert cs.lock_verdict(lock, time.time(), lambda pid: False) is None


def test_lock_verdict_refuses_a_lock_claimed_seconds_ago_even_without_a_pid():
    # The claim-then-confirm window: the other sidecar may not be observable yet.
    lock = {"pid": None, "started": time.time() - 1.0, "owner": "a"}
    assert cs.lock_verdict(lock, time.time(), lambda pid: False)


def test_lock_verdict_ignores_a_corrupt_lock_file():
    # Refusing forever on an unparsable lock is how the bridge once bricked
    # itself until logs/bridge.lock was hand-deleted.
    assert cs.lock_verdict("garbage", time.time(), lambda pid: True) is None


# ===========================================================================
# Pure: dead-man predicate
# ===========================================================================

BASE_DEADMAN = dict(now=1000.0, panel_last_ok=100.0, panel_missing_since=None,
                    panel_miss_count=0, bridge_busy="none",
                    turn_in_flight=False, deadman_seconds=300.0)


def test_deadman_fires_when_genuinely_stale():
    assert cs.deadman_verdict(**BASE_DEADMAN)[0] is True


def test_deadman_never_fires_during_a_render():
    # Measured live: a 9.28 s block cost ~295 missed defer ticks and a 9.66 s
    # gap in panel.json, and CAPTURE_TIMEOUT_MS is 180000.
    args = dict(BASE_DEADMAN, bridge_busy="render")
    fired, reason = cs.deadman_verdict(**args)
    assert fired is False and reason == "bridge_busy_render"


def test_deadman_never_fires_mid_turn():
    fired, reason = cs.deadman_verdict(**dict(BASE_DEADMAN, turn_in_flight=True))
    assert fired is False and reason == "turn_in_flight"


def test_deadman_does_not_arm_before_a_panel_has_ever_been_seen():
    # Headless and --once runs have no panel; self-killing there would be a bug.
    fired, reason = cs.deadman_verdict(**dict(BASE_DEADMAN, panel_last_ok=None))
    assert fired is False and reason == "no_panel_yet"


def test_deadman_treats_a_briefly_missing_panel_as_unknown():
    # The panel's atomic write has a remove window; absence is not death.
    args = dict(BASE_DEADMAN, panel_missing_since=999.0, panel_miss_count=1)
    fired, reason = cs.deadman_verdict(**args)
    assert fired is False and reason == "panel_missing_briefly"


def test_deadman_requires_the_absence_to_span_the_timeout():
    args = dict(BASE_DEADMAN, panel_missing_since=900.0, panel_miss_count=50)
    fired, reason = cs.deadman_verdict(**args)
    assert fired is False and reason == "panel_missing_window"


def test_deadman_fires_once_the_absence_spans_the_timeout():
    args = dict(BASE_DEADMAN, panel_missing_since=600.0, panel_miss_count=50)
    assert cs.deadman_verdict(**args)[0] is True


def test_deadman_holds_off_while_the_panel_is_fresh():
    fired, reason = cs.deadman_verdict(**dict(BASE_DEADMAN, panel_last_ok=999.0))
    assert fired is False and reason == "panel_fresh"


# ===========================================================================
# Pure: budget gate
# ===========================================================================

BASE_GATE = dict(daily_cost=1.0, daily_budget=10.0, session_cost=1.0,
                 session_budget=None, last_turn_cost=0.05, turn_warn_usd=0.25,
                 warn_acknowledged=True, bridge_ok=True, status="idle")


def test_gate_allows_a_normal_send():
    assert cs.budget_gate(**BASE_GATE) == (True, None, None)


def test_gate_refuses_while_the_bridge_is_stale():
    # The cheapest guard in the design: every MCP call would burn a full
    # timeout and the model retries, each retry a full-context Opus turn.
    allowed, code, _ = cs.budget_gate(**dict(BASE_GATE, bridge_ok=False))
    assert (allowed, code) == (False, "BRIDGE_STALE")


def test_gate_refuses_past_the_daily_budget():
    allowed, code, _ = cs.budget_gate(**dict(BASE_GATE, daily_cost=10.0))
    assert (allowed, code) == (False, "DAILY_BUDGET_EXCEEDED")


def test_gate_refuses_past_the_session_budget():
    allowed, code, _ = cs.budget_gate(
        **dict(BASE_GATE, session_cost=6.0, session_budget=5.0))
    assert (allowed, code) == (False, "SESSION_BUDGET_EXCEEDED")


def test_gate_blocks_the_next_send_after_an_expensive_turn():
    allowed, code, _ = cs.budget_gate(
        **dict(BASE_GATE, last_turn_cost=0.9, warn_acknowledged=False))
    assert (allowed, code) == (False, "TURN_WARN_UNACKNOWLEDGED")


def test_gate_clears_once_the_warning_is_acknowledged():
    allowed, _code, _ = cs.budget_gate(
        **dict(BASE_GATE, last_turn_cost=0.9, warn_acknowledged=True))
    assert allowed is True


def test_gate_reports_a_latched_budget_exhaustion_distinctly():
    # --max-budget-usd latches: the process stays ALIVE and answers everything
    # in ~8 ms with the same error, so this is not "child_exited".
    allowed, code, _ = cs.budget_gate(**dict(BASE_GATE, status="budget_exhausted"))
    assert (allowed, code) == (False, "BUDGET_EXHAUSTED")


def test_gate_refuses_when_the_queue_is_full():
    allowed, code, _ = cs.budget_gate(
        **dict(BASE_GATE, queue_depth=5, max_queue_depth=5))
    assert (allowed, code) == (False, "QUEUE_FULL")


def test_gate_puts_the_bridge_check_before_the_money_checks():
    # A stale bridge with a blown budget must report the bridge: it is the
    # cheaper and more actionable fact.
    _allowed, code, _ = cs.budget_gate(
        **dict(BASE_GATE, bridge_ok=False, daily_cost=99.0))
    assert code == "BRIDGE_STALE"


# ===========================================================================
# Pure: retention sweep, redaction, launch line
# ===========================================================================

def test_sweep_plan_drops_files_past_the_age_limit():
    now = 1_000_000.0
    entries = [("old.jsonl", now - 8 * 86400, 10), ("new.jsonl", now - 60, 10)]
    assert cs.sweep_plan(entries, now, 7 * 86400, None) == ["old.jsonl"]


def test_sweep_plan_drops_oldest_first_past_the_size_limit():
    now = 1_000_000.0
    entries = [("a", now - 30, 100), ("b", now - 20, 100), ("c", now - 10, 100)]
    assert cs.sweep_plan(entries, now, None, 150) == ["a", "b"]


def test_sweep_plan_never_deletes_the_file_the_panel_is_tailing():
    now = 1_000_000.0
    entries = [("live.jsonl", now - 90 * 86400, 10 ** 9)]
    assert cs.sweep_plan(entries, now, 7 * 86400, 1, protect=["live.jsonl"]) == []


def test_redaction_covers_the_three_key_shapes_and_the_auth_token():
    text = ("key sk-ant-api03-AAAA token ghp_BBBBBBBBBBBB aws AKIAIOSFODNN7EXAMPLE "
            "shared hunter2hunter2")
    out = cs.redact(text, ("hunter2hunter2",))
    for secret in ("sk-ant-", "ghp_B", "AKIAIOSFODNN7EXAMPLE", "hunter2hunter2"):
        assert secret not in out
    assert out.count(cs.REDACTED) == 4


def test_redaction_ignores_a_short_or_empty_token():
    assert cs.redact("abc", (None,)) == "abc"
    assert cs.redact("abc", ("ab",)) == "abc"


def test_launch_line_carries_all_three_hermeticity_flags():
    # --setting-sources '' alone still loads 129 tools and 7 remote connectors.
    # All three or none.
    argv = cs.build_argv("claude.exe", "C:/repo", cs.DEFAULT_CONFIG,
                         session_id="abc")
    assert "--setting-sources" in argv and argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert "--disable-slash-commands" in argv


def test_launch_line_blocks_the_one_call_that_hangs_under_bypass():
    argv = cs.build_argv("claude.exe", "C:/repo", cs.DEFAULT_CONFIG, session_id="abc")
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--disallowedTools") + 1] == "AskUserQuestion"


def test_launch_line_mints_a_session_id_or_resumes_but_never_both():
    fresh = cs.build_argv("c", "r", cs.DEFAULT_CONFIG, session_id="abc")
    assert "--session-id" in fresh and "--resume" not in fresh
    # Reusing a MINTED id fails on stderr with zero stdout events, so a restart
    # always goes through --resume.
    warm = cs.build_argv("c", "r", cs.DEFAULT_CONFIG, session_id="abc",
                         resume_id="abc")
    assert "--resume" in warm and "--session-id" not in warm


def test_launch_line_holds_no_secret():
    # --mcp-config is visible in any local process listing.
    argv = cs.build_argv("c", "r", cs.DEFAULT_CONFIG, session_id="abc")
    blob = " ".join(argv)
    assert "auth_token" not in blob and "sk-ant-" not in blob


def test_mcp_config_points_the_server_at_this_repo():
    config = json.loads(cs.mcp_config_json("C:/repo", "py.exe"))
    server = config["mcpServers"]["reaper-daemon"]
    assert server["command"] == "py.exe"
    assert server["env"]["REAPER_DAEMON_ROOT"] == "C:/repo"
    assert server["env"]["REAPER_DAEMON_CONSOLE_MODE"] == "1"


def test_child_env_disables_the_tool_search_hop():
    # Above ~50 tools the CLI defers schemas and the model burns a whole turn
    # on ToolSearch before it can reach a REAPER tool.
    env = cs.child_env("C:/repo", base={"PATH": "x"})
    assert env["ENABLE_TOOL_SEARCH"] == "0"
    assert env["REAPER_DAEMON_ROOT"] == "C:/repo"


def test_a_cmd_shim_is_refused_when_no_real_binary_sits_beside_it():
    # Killing a shim leaves node alive holding a paid session.
    path, failure = cs.resolve_claude_path("C:/bin/claude.cmd",
                                           isfile=lambda p: p.endswith(".cmd"))
    assert path is None
    assert failure["error"]["code"] == "CLAUDE_SHIM_UNSUPPORTED"


def test_a_cmd_shim_resolves_to_its_sibling_exe_when_one_exists():
    path, failure = cs.resolve_claude_path(
        "C:/bin/claude.cmd", isfile=lambda p: p.endswith((".cmd", ".exe")))
    assert failure is None and path.endswith("claude.exe")


def test_a_missing_claude_fails_closed():
    assert cs.resolve_claude_path(None)[1]["error"]["code"] == "CLAUDE_NOT_FOUND"


def test_user_message_line_is_one_turn_per_line():
    line = cs.user_message_line("sid", "hello")
    assert line["type"] == "user" and line["session_id"] == "sid"
    assert line["message"]["content"][0]["text"] == "hello"


# ===========================================================================
# Pure-ish: the atomic write retry
# ===========================================================================

def test_atomic_write_retries_a_windows_permission_error(tmp_path, monkeypatch):
    # tests/bridge_fakes.py:33-41 has this failure class on record, reproduced
    # live on windows-latest / Python 3.14. The sidecar rewrites state.json
    # every second for hours while the panel holds it open.
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "used by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(cs.os, "replace", flaky)
    target = str(tmp_path / "state.json")
    assert cs.atomic_write_json(target, {"a": 1}, attempts=5, delay=0.001) is None
    assert calls["n"] == 3
    assert json.load(open(target, encoding="utf-8")) == {"a": 1}


def test_atomic_write_gives_up_and_returns_a_structured_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cs.os, "replace",
                        lambda s, d: (_ for _ in ()).throw(PermissionError(13, "locked")))
    failure = cs.atomic_write_json(str(tmp_path / "s.json"), {"a": 1},
                                   attempts=2, delay=0.001)
    assert failure["ok"] is False
    assert failure["error"]["code"] == "ATOMIC_WRITE_FAILED"


def test_read_json_returns_none_for_a_missing_file(tmp_path):
    assert cs.read_json(str(tmp_path / "nope.json")) is None


# ===========================================================================
# Integration: a real child process
# ===========================================================================

def test_turn_round_trip_produces_the_panel_event_set(console_root, sidecars):
    scenario = {"turns": [[
        init_event(),
        text_delta("Renam"),
        text_delta("ing."),
        assistant_text("Renaming the drums track."),
        tool_use("t1", "mcp__reaper-daemon__track", {"action": "rename"}),
        tool_result("t1", {"ok": True, "message": "renamed"}),
        result(0.27),
    ]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    assert sidecar.spawn() is None
    assert sidecar.send_now("rename the drums track") is None
    assert wait_for(lambda: sidecar._result_event.is_set())

    kinds = [e["t"] for e in read_events(sidecar)]
    assert kinds == ["user", "text_delta", "text_delta", "text", "tool",
                     "tool_result", "turn_end"]
    assert sidecar.turn_cost == pytest.approx(0.27)
    assert sidecar.status == "idle"
    assert sidecar.turn_in_flight is False
    assert "interrupt_receipt_v1" in sidecar.capabilities


def test_cost_is_differenced_across_turns_not_summed(console_root, sidecars):
    scenario = {"turns": [
        [init_event(), assistant_text("one"), result(0.27)],
        [init_event(), assistant_text("two"), result(0.41)],
        [init_event(), assistant_text("three"), result(0.55)],
    ]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    costs = []
    for prompt in ("a", "b", "c"):
        sidecar._result_event.clear()
        sidecar.send_now(prompt)
        assert wait_for(lambda: sidecar._result_event.is_set())
        costs.append(sidecar.turn_cost)
    assert costs == pytest.approx([0.27, 0.14, 0.14])
    # Summing total_cost_usd would have said $1.23 for a $0.55 session.
    assert sidecar.session_cost == pytest.approx(0.55)
    assert sidecar.daily_cost == pytest.approx(0.55)


def test_daily_cost_survives_a_restart(console_root, sidecars):
    scenario = {"turns": [[init_event(), result(0.42)]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("spend")
    assert wait_for(lambda: sidecar._result_event.is_set())
    sidecar.kill_child()

    # A fresh process: --max-budget-usd is per process, and the panel's Restart
    # button would otherwise zero the day.
    reborn = cs.ConsoleSidecar(root=console_root, take_lock=False,
                               claude_argv=["noop"])
    sidecars.append(reborn)
    assert reborn.daily_cost == pytest.approx(0.42)


def test_a_malformed_stdout_line_does_not_end_the_session(console_root, sidecars):
    scenario = {"turns": [[
        init_event(),
        {"__raw": "this is not json at all"},
        {"__raw": "{\"type\": \"assistant\", TRUNCATED"},
        assistant_text("still here"),
        result(0.05),
    ]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("hi")
    assert wait_for(lambda: sidecar._result_event.is_set())
    kinds = [e["t"] for e in read_events(sidecar)]
    assert kinds == ["user", "text", "turn_end"]
    # The bad lines are still in the raw log, which is where debugging happens.
    with open(sidecar.raw_path, "r", encoding="utf-8") as fh:
        assert "not json at all" in fh.read()


def test_a_child_crash_is_reported_with_its_stderr(console_root, sidecars):
    # Reusing a minted --session-id fails with "already in use" on STDERR and
    # ZERO stdout events. A stdout-only supervisor sees a silent empty stream.
    scenario = {"startup": [
        {"__stderr": "Session ID 0a1b-2c3d is already in use."},
        {"__exit": 1},
    ]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    assert wait_for(lambda: sidecar.status == "child_exited")
    assert wait_for(lambda: any("already in use" in line
                                for line in sidecar.stderr_tail))
    ends = [e for e in read_events(sidecar) if e["t"] == "session_end"]
    assert ends and ends[0]["reason"] == "child_exited"


def test_a_stderr_flood_does_not_deadlock_the_stdout_reader(console_root, sidecars):
    # 64 KB of unread stderr blocks the child's write. A supervisor draining
    # only stdout would hang here and never see the result.
    scenario = {"turns": [[
        init_event(),
        {"__stderr_bytes": 262144},
        {"__stdout_bytes": 262144},
        result(0.02),
    ]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("flood")
    assert wait_for(lambda: sidecar._result_event.is_set(), timeout=20.0)
    events = read_events(sidecar)
    text = [e for e in events if e["t"] == "text"][0]
    # Capped for the panel, full text preserved in raw.
    assert len(text["text"]) < 2200
    assert os.path.getsize(sidecar.raw_path) > 200000


def test_a_budget_latch_becomes_its_own_state(console_root, sidecars):
    # The process does NOT exit; it answers every later turn in ~8 ms with the
    # same error. Offering Send again would just burn the click.
    scenario = {"turns": [[
        init_event(),
        result(9.99, subtype="error_max_budget_usd", is_error=True),
    ]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("expensive")
    assert wait_for(lambda: sidecar._result_event.is_set())
    assert sidecar.status == "budget_exhausted"
    assert sidecar.proc.poll() is None  # still alive, that is the point
    allowed, code, _ = sidecar.gate()
    assert (allowed, code) == (False, "BUDGET_EXHAUSTED")


def test_an_interrupt_while_idle_returns_success_and_emits_no_result(
        console_root, sidecars):
    scenario = {"turns": [[init_event(), assistant_text("done"), result(0.03)]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("go")
    assert wait_for(lambda: sidecar._result_event.is_set())
    before = len(read_events(sidecar))

    answer = sidecar.interrupt()
    assert answer["ok"] is True
    # Nothing may block waiting for a result that is never coming.
    time.sleep(0.3)
    assert len(read_events(sidecar)) == before


def test_an_interrupt_is_refused_before_the_capability_is_known(
        console_root, sidecars):
    # init is LAZY: nothing is emitted until the first user message arrives.
    scenario = {"turns": [[init_event(), result(0.01)]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    failure = sidecar.interrupt()
    assert failure["error"]["code"] == "INTERRUPT_UNSUPPORTED"


def test_an_interrupt_mid_turn_reads_as_cancelled_not_as_an_error(
        console_root, sidecars):
    scenario = {"turns": [[
        init_event(),
        assistant_text("thinking"),
        {"__sleep": 0.4},
        result(0.30),
    ]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("long one")
    assert wait_for(lambda: "interrupt_receipt_v1" in sidecar.capabilities)
    assert sidecar.interrupt()["ok"] is True
    assert wait_for(lambda: sidecar._result_event.is_set())
    end = [e for e in read_events(sidecar) if e["t"] == "turn_end"][-1]
    assert end["cancelled"] is True and end["error"] is False


def test_the_process_tree_dies_with_the_child(console_root, sidecars):
    # claude.exe spawns the MCP server as a GRANDCHILD. Killing only the direct
    # child leaves python holding the bridge queue (and node holding a paid
    # session behind an npm shim).
    marker = os.path.join(console_root, "grandchild.txt")
    scenario = {"turns": [[init_event(), {"__spawn_child": marker}, result(0.01)]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("spawn")
    assert wait_for(lambda: sidecar._result_event.is_set())
    assert wait_for(lambda: os.path.isfile(marker) and os.path.getsize(marker) > 0)

    sidecar.kill_child(reason="tree-kill test")
    time.sleep(0.4)
    settled = os.path.getsize(marker)
    time.sleep(0.5)
    assert os.path.getsize(marker) == settled


def test_the_singleton_lock_refuses_a_second_sidecar(console_root, sidecars):
    first = make_sidecar(sidecars, console_root, {}, take_lock=True)
    second = make_sidecar(sidecars, console_root, {}, take_lock=True)
    assert first.claim_lock(confirm_delay=0.01) is None
    failure = second.claim_lock(confirm_delay=0.01)
    assert failure["error"]["code"] == "CONSOLE_ALREADY_RUNNING"
    # And it never touches the bridge's own lock.
    assert not os.path.exists(os.path.join(console_root, "logs", "bridge.lock"))
    first.release_lock()
    assert second.claim_lock(confirm_delay=0.01) is None
    second.release_lock()


def test_a_lock_from_a_dead_pid_is_reclaimed(console_root, sidecars):
    lock_path = os.path.join(console_root, "console", "sidecar.lock")
    cs.atomic_write_json(lock_path, {"pid": 999999, "owner": "ghost",
                                     "started": time.time() - 7200})
    sidecar = make_sidecar(sidecars, console_root, {}, take_lock=True)
    assert sidecar.claim_lock(confirm_delay=0.01) is None
    sidecar.release_lock()


def test_a_send_during_a_live_turn_is_queued_not_forwarded(console_root, sidecars):
    scenario = {"turns": [
        [init_event(), {"__sleep": 0.5}, result(0.05)],
        [init_event(), assistant_text("second"), result(0.08)],
    ]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("first")
    assert sidecar.enqueue("second") is None
    sidecar.pump_queue()  # must be a no-op while a turn is live
    assert len(sidecar.queue) == 1
    sidecar.publish_state()
    state = cs.read_json(sidecar.state_path)
    assert state["queue_depth"] == 1 and state["turn_in_flight"] is True

    assert wait_for(lambda: sidecar._result_event.is_set())
    sidecar._result_event.clear()
    sidecar.pump_queue()
    assert sidecar.queue == []
    assert wait_for(lambda: sidecar._result_event.is_set())
    assert sidecar.turns == 2


def test_the_queue_refuses_past_its_cap(console_root, sidecars):
    sidecar = make_sidecar(sidecars, console_root, {},
                           config={"max_queue_depth": 2})
    sidecar._open_session_files("cap-test")
    assert sidecar.enqueue("one") is None
    assert sidecar.enqueue("two") is None
    failure = sidecar.enqueue("three")
    assert failure["error"]["code"] == "QUEUE_FULL"


def test_a_prompt_file_is_consumed_and_carries_its_focus_envelope(
        console_root, sidecars):
    sidecar = make_sidecar(sidecars, console_root, {})
    sidecar._open_session_files("prompt-test")
    path = os.path.join(console_root, "console", "prompts",
                        cs.stamped_id() + ".json")
    cs.atomic_write_json(path, {"text": "make it louder",
                                "focus": {"track": "Drums", "bars": "17-24"}})
    sidecar.poll_prompts()
    assert not os.path.exists(path)
    assert len(sidecar.queue) == 1
    assert "<focus>" in sidecar.queue[0]["text"]
    assert "Drums" in sidecar.queue[0]["text"]


def test_a_stale_bridge_blocks_the_prompt_before_any_money_is_spent(
        console_root, sidecars):
    scenario = {"turns": [[init_event(), result(0.5)]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    write_heartbeat(console_root, age=3600)  # stale, not busy
    sidecar.enqueue("do something expensive")
    sidecar.pump_queue()
    assert sidecar.turn_in_flight is False
    assert sidecar.queue  # held, not dropped: this is recoverable
    assert sidecar.last_error["code"] == "BRIDGE_STALE"


def test_the_deadman_holds_off_while_reaper_is_rendering(console_root, sidecars):
    scenario = {"turns": [[init_event(), result(0.01)]]}
    sidecar = make_sidecar(sidecars, console_root, scenario,
                           deadman_seconds=0.05)
    write_heartbeat(console_root, busy="render")
    panel = os.path.join(console_root, "console", "panel.json")
    cs.atomic_write_json(panel, {"alive_at": cs.utc_iso()})
    old = time.time() - 600
    os.utime(panel, (old, old))  # 10 minutes stale, far past the timeout

    thread = threading.Thread(target=sidecar.run, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: sidecar.proc is not None)
        time.sleep(0.4)  # ~40 poll cycles at a 50 ms timeout
        assert sidecar.proc.poll() is None, "killed a live REAPER mid-render"
        assert not sidecar._stopping.is_set()

        # Render over: now the same staleness is a real death.
        write_heartbeat(console_root, busy=None)
        assert wait_for(lambda: sidecar._stopping.is_set(), timeout=5.0)
        assert sidecar.stop_reason == "deadman"
    finally:
        sidecar._stopping.set()
        thread.join(timeout=10)


def test_a_shutdown_control_file_stops_the_daemon_cleanly(console_root, sidecars):
    sidecar = make_sidecar(sidecars, console_root, {}, deadman_seconds=600.0)
    thread = threading.Thread(target=sidecar.run, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: sidecar.proc is not None)
        cs.atomic_write_json(
            os.path.join(console_root, "console", "control",
                         cs.stamped_id() + ".json"), {"action": "shutdown"})
        assert wait_for(lambda: sidecar.stop_reason == "shutdown_control", timeout=5.0)
        thread.join(timeout=10)
        assert sidecar.proc.poll() is not None
        ends = [e for e in read_events(sidecar) if e["t"] == "session_end"]
        assert any(e["reason"] == "shutdown_control" for e in ends)
    finally:
        sidecar._stopping.set()


def test_state_json_is_published_for_the_panel(console_root, sidecars):
    scenario = {"turns": [[init_event(), result(0.11)]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("hello")
    assert wait_for(lambda: sidecar._result_event.is_set())
    state = cs.read_json(sidecar.state_path)
    assert state["schema"] == cs.SCHEMA_VERSION
    assert state["status"] == "idle"
    assert state["session_cost_usd"] == pytest.approx(0.11)
    assert state["events_file"].startswith("console/events/")
    assert state["warn_pending"] is False


def test_an_expensive_turn_arms_the_warning_for_the_next_send(
        console_root, sidecars):
    scenario = {"turns": [[init_event(), result(0.90)]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("pricey")
    assert wait_for(lambda: sidecar._result_event.is_set())
    assert sidecar.warn_acknowledged is False
    allowed, code, _ = sidecar.gate()
    assert (allowed, code) == (False, "TURN_WARN_UNACKNOWLEDGED")
    cs.atomic_write_json(
        os.path.join(console_root, "console", "control", cs.stamped_id() + ".json"),
        {"action": "ack_warn"})
    sidecar.poll_control()
    assert sidecar.gate()[0] is True


def test_three_failing_tool_results_break_the_circuit(console_root, sidecars):
    scenario = {"turns": [[
        init_event(),
        tool_use("t1", "mcp__reaper-daemon__get_status"),
        tool_result("t1", {"ok": False}, is_error=True),
        tool_use("t2", "mcp__reaper-daemon__get_status"),
        tool_result("t2", {"ok": False}, is_error=True),
        tool_use("t3", "mcp__reaper-daemon__get_status"),
        tool_result("t3", {"ok": False}, is_error=True),
        {"__sleep": 0.5},
        result(0.20),
    ]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("loop against a dead bridge")
    assert wait_for(lambda: sidecar._result_event.is_set(), timeout=10.0)
    codes = [e.get("code") for e in read_events(sidecar) if e["t"] == "error"]
    assert "TOOL_ERROR_CIRCUIT_BREAK" in codes


def test_unverified_results_do_not_trip_the_circuit_breaker(console_root, sidecars):
    # UNVERIFIED means the mutation landed. The model must stay free to measure.
    scenario = {"turns": [[
        init_event(),
        tool_result("t1", {"status": "UNVERIFIED"}),
        tool_result("t2", {"status": "UNVERIFIED"}),
        tool_result("t3", {"status": "UNVERIFIED"}),
        result(0.05),
    ]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("measure")
    assert wait_for(lambda: sidecar._result_event.is_set())
    codes = [e.get("code") for e in read_events(sidecar) if e["t"] == "error"]
    assert "TOOL_ERROR_CIRCUIT_BREAK" not in codes


def test_stranded_inbox_commands_are_withdrawn_on_a_kill(console_root, sidecars):
    # A killed MCP child never reaches reaperd's withdraw path (reaperd.py:281-290)
    # and a queued batch would land minutes later in whatever project is open then.
    sidecar = make_sidecar(sidecars, console_root, {})
    sidecar._open_session_files("sweep-test")
    inbox = os.path.join(console_root, "inbox")
    old = os.path.join(inbox, "someone-elses.json")
    cs.atomic_write_json(old, {"id": "someone-elses", "created_by": "cli"})
    stamp = time.time() - 600
    os.utime(old, (stamp, stamp))
    turn_started = time.time()
    mine = os.path.join(inbox, "cli-mine.json")
    cs.atomic_write_json(mine, {"id": "cli-mine", "created_by": "cli"})

    removed = sidecar.sweep_inbox_for_console_commands(turn_started)
    assert removed == ["cli-mine.json"]
    assert os.path.exists(old), "a command predating the turn is not ours to delete"


def test_sweep_survives_a_coarse_filesystem_clock(console_root, sidecars):
    # A file written just AFTER the turn's timestamp can carry an mtime just
    # BEFORE it: time.time() is the precise clock on Windows since 3.13, NTFS
    # stamps from the coarse tick. Backdated explicitly so the assertion does
    # not depend on the runner's clock the way the live race does.
    sidecar = make_sidecar(sidecars, console_root, {})
    sidecar._open_session_files("skew-test")
    inbox = os.path.join(console_root, "inbox")
    turn_started = time.time()
    mine = os.path.join(inbox, "cli-skewed.json")
    cs.atomic_write_json(mine, {"id": "cli-skewed", "created_by": "mcp"})
    stamp = turn_started - (cs.MTIME_GRANULARITY_SLACK / 2)
    os.utime(mine, (stamp, stamp))

    assert sidecar.sweep_inbox_for_console_commands(turn_started) == ["cli-skewed.json"]


def test_sweep_slack_does_not_reach_an_unrelated_command(console_root, sidecars):
    # The slack is clock tolerance, not a wider net: a command from David's own
    # terminal, older than the slack, still survives.
    sidecar = make_sidecar(sidecars, console_root, {})
    sidecar._open_session_files("slack-bound-test")
    inbox = os.path.join(console_root, "inbox")
    turn_started = time.time()
    theirs = os.path.join(inbox, "cli-theirs.json")
    cs.atomic_write_json(theirs, {"id": "cli-theirs", "created_by": "cli"})
    stamp = turn_started - (cs.MTIME_GRANULARITY_SLACK * 2)
    os.utime(theirs, (stamp, stamp))

    assert sidecar.sweep_inbox_for_console_commands(turn_started) == []
    assert os.path.exists(theirs)


def test_no_sweep_happens_when_no_turn_was_in_flight(console_root, sidecars):
    sidecar = make_sidecar(sidecars, console_root, {})
    inbox = os.path.join(console_root, "inbox")
    cs.atomic_write_json(os.path.join(inbox, "cli-x.json"), {"created_by": "cli"})
    assert sidecar.sweep_inbox_for_console_commands(None) == []
    assert os.path.exists(os.path.join(inbox, "cli-x.json"))


def test_retention_sweep_protects_the_live_session(console_root, sidecars):
    sidecar = make_sidecar(sidecars, console_root, {},
                           config={"retention_days": 7,
                                   "retention_bytes": 10 ** 9})
    sidecar._open_session_files("live-session")
    stale = os.path.join(console_root, "console", "events", "ancient.jsonl")
    with open(stale, "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))
    os.utime(sidecar.events_path, (old, old))

    doomed = sidecar.sweep_retention(interval=0.0)
    assert stale in doomed
    assert sidecar.events_path not in doomed
    assert os.path.exists(sidecar.events_path)


def test_the_launch_line_reaches_the_child_intact(console_root, sidecars):
    """The fake records its own argv, so the flag set is checked end to end."""
    argv_path = os.path.join(console_root, "fake", "scenario.json.argv.json")
    sidecar = make_sidecar(sidecars, console_root, {"turns": [[result(0.0)]]})
    # Re-spawn through the real builder by handing the fake a full flag set.
    fake_argv = list(sidecar.claude_argv)
    sidecar.claude_argv = fake_argv + cs.build_argv(
        "ignored", console_root, cs.DEFAULT_CONFIG, session_id="abc")[1:]
    sidecar.spawn()
    assert wait_for(lambda: os.path.isfile(argv_path))
    recorded = json.load(open(argv_path, encoding="utf-8"))
    assert "--strict-mcp-config" in recorded
    assert "--include-partial-messages" in recorded
    assert recorded[recorded.index("--model") + 1] == "opus"
    assert recorded[recorded.index("--effort") + 1] == "medium"


def test_control_requests_from_the_child_are_always_answered(console_root, sidecars):
    # Silently dropping one makes the CLI block forever with no visible failure.
    scenario = {"turns": [[
        init_event(),
        {"type": "control_request", "request_id": "srv_1",
         "request": {"subtype": "can_use_tool", "input": {"a": 1}}},
        {"__sleep": 0.15},
        result(0.01),
    ]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("go")
    assert wait_for(lambda: sidecar._result_event.is_set())
    with open(sidecar.fake_paths["stdin"], "r", encoding="utf-8") as fh:
        sent = [json.loads(line) for line in fh if line.strip()]
    answers = [m for m in sent if m.get("type") == "control_response"]
    assert answers and answers[0]["response"]["request_id"] == "srv_1"
    assert answers[0]["response"]["subtype"] == "success"


def test_an_unknown_control_request_still_gets_an_error_answer(console_root, sidecars):
    scenario = {"turns": [[
        init_event(),
        {"type": "control_request", "request_id": "srv_9",
         "request": {"subtype": "teleport_me"}},
        {"__sleep": 0.15},
        result(0.01),
    ]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("go")
    assert wait_for(lambda: sidecar._result_event.is_set())
    with open(sidecar.fake_paths["stdin"], "r", encoding="utf-8") as fh:
        sent = [json.loads(line) for line in fh if line.strip()]
    answers = [m for m in sent if m.get("type") == "control_response"]
    assert answers[0]["response"]["subtype"] == "error"


def test_secrets_never_reach_the_raw_transcript(console_root, sidecars):
    scenario = {"turns": [[
        init_event(),
        assistant_text("the key is sk-ant-api03-DEADBEEFDEADBEEF"),
        result(0.01),
    ]]}
    sidecar = make_sidecar(sidecars, console_root, scenario)
    sidecar.spawn()
    sidecar.send_now("leak it")
    assert wait_for(lambda: sidecar._result_event.is_set())
    with open(sidecar.raw_path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    assert "sk-ant-api03-DEADBEEF" not in raw
    assert cs.REDACTED in raw


def test_pid_alive_agrees_with_a_process_we_control():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert cs.pid_alive(child.pid) is True
    finally:
        child.kill()
        child.wait(timeout=10)
    assert cs.pid_alive(child.pid) is False
    assert cs.pid_alive(0) is False
    assert cs.pid_alive("nope") is False
