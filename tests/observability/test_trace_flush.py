import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from hooks.observability import agent_trace, otel, trace_flush


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "agent_trace")
    return tmp_path


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(otel, "langfuse_exporter_config", lambda: {"endpoint": "http://unused", "headers": {}})


class Clock:
    def __init__(self):
        self.now = 0.0
        self.hooks = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        for at, action in list(self.hooks):
            if self.now >= at:
                self.hooks.remove((at, action))
                action()


def _request(session, transcript, reason="start", owner=os.getpid(), at=None):
    trace_flush._write(
        trace_flush.request_path(session),
        {
            "owner_pid": owner,
            "owner_start": trace_flush.start_time(owner),
            "transcript": str(transcript),
            "reason": reason,
            "at": at or time.time_ns(),
        },
    )


def _grow(path, text="x"):
    with path.open("a") as handle:
        handle.write(text + "\n")


def test_budget_defaults_and_overrides():
    assert trace_flush.budget({}) == trace_flush.Budget(15.0, 5.0, 3)
    assert trace_flush.budget(
        {
            "AGENTIHOOKS_TRACE_FLUSH_INTERVAL_SEC": "2",
            "AGENTIHOOKS_TRACE_FLUSH_ATTEMPT_TIMEOUT_SEC": "0.5",
            "AGENTIHOOKS_TRACE_FLUSH_ATTEMPTS": "4",
        }
    ) == trace_flush.Budget(2.0, 0.5, 4)
    bad = {"AGENTIHOOKS_TRACE_FLUSH_INTERVAL_SEC": "soon", "AGENTIHOOKS_TRACE_FLUSH_ATTEMPTS": "0"}
    assert trace_flush.budget(bad) == trace_flush.Budget(15.0, 5.0, 3)


def test_owner_liveness_is_bound_to_process_start():
    me = os.getpid()
    assert trace_flush.alive(trace_flush.Owner(me, trace_flush.start_time(me)))
    assert not trace_flush.alive(trace_flush.Owner(me, trace_flush.start_time(me) + 1))
    assert not trace_flush.alive(trace_flush.Owner(1, trace_flush.start_time(1) or 0))
    assert trace_flush.start_time(2**31) is None


def test_request_is_silent_when_langfuse_is_off(home):
    spawned = []
    assert trace_flush.request("session", "/t", "stop", owner_pid=os.getpid(), spawn=spawned.append) is False
    assert spawned == []
    assert not trace_flush.request_path("session").exists()


def test_request_starts_one_exporter_and_keeps_the_known_transcript(home, enabled, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    spawned = []
    assert trace_flush.request("session", "/t.jsonl", "start", owner_pid=os.getpid(), spawn=spawned.append)
    me = {"supervisor_pid": os.getpid(), "supervisor_start": trace_flush.start_time(os.getpid())}
    trace_flush._write(trace_flush.owner_path("session"), me)
    assert not trace_flush.request("session", "", "stop", owner_pid=os.getpid(), spawn=spawned.append)
    assert spawned == ["session"]
    record = json.loads(trace_flush.request_path("session").read_text())
    assert record["transcript"] == "/t.jsonl"
    assert record["reason"] == "stop"
    assert record["target"] == "codex"
    assert record["owner_pid"] == os.getpid()
    assert record["owner_start"] == trace_flush.start_time(os.getpid())


def test_request_replaces_a_dead_exporter(home, enabled):
    trace_flush._write(trace_flush.owner_path("session"), {"supervisor_pid": os.getpid(), "supervisor_start": 1})
    spawned = []
    assert trace_flush.request("session", "/t", "prompt", owner_pid=os.getpid(), spawn=spawned.append)
    assert spawned == ["session"]


def test_request_defaults_to_the_agent_process(home, enabled, monkeypatch):
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: os.getpid())
    trace_flush.request("session", "/t", "start", spawn=lambda s: None)
    assert json.loads(trace_flush.request_path("session").read_text())["owner_pid"] == os.getpid()


def test_exporter_flushes_during_an_active_turn_and_once_after_the_owner_exits(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    clock, calls, state = Clock(), [], {"alive": True}
    clock.hooks += [(20, lambda: _grow(transcript)), (50, lambda: _grow(transcript))]
    clock.hooks += [(70, lambda: state.update(alive=False))]

    def send(session, path, timeout, trigger):
        calls.append((clock(), trigger, timeout))
        return True

    result = trace_flush.supervise(
        "session", trace_flush.Budget(15, 5, 3), send, clock, clock.sleep, lambda owner: state["alive"]
    )
    assert result == "owner exited"
    assert [(at, trigger) for at, trigger, _ in calls] == [
        (0, "request:start"),
        (30, "interval"),
        (60, "interval"),
    ]
    assert {timeout for _, _, timeout in calls} == {5}
    assert not trace_flush.owner_path("session").exists()


def test_owner_exit_drains_new_records_with_a_final_trigger(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    clock, calls, state = Clock(), [], {"alive": True}
    clock.hooks += [(3, lambda: (_grow(transcript), state.update(alive=False)))]
    trace_flush.supervise(
        "session",
        trace_flush.Budget(15, 5, 3),
        lambda *args: calls.append((clock(), args[3])) or True,
        clock,
        clock.sleep,
        lambda owner: state["alive"],
    )
    assert calls == [(0, "request:start"), (3, "final")]


def test_failed_export_retries_three_times_per_wake_and_keeps_pending_work(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    clock, calls, state = Clock(), [], {"alive": True}
    clock.hooks += [(40, lambda: state.update(alive=False))]
    trace_flush.supervise(
        "session",
        trace_flush.Budget(15, 5, 3),
        lambda *args: calls.append((clock(), args[3])) or False,
        clock,
        clock.sleep,
        lambda owner: state["alive"],
    )
    assert calls == [(0, "request:start")] * 3 + [(15, "interval")] * 3 + [(30, "interval")] * 3 + [(40, "final")] * 3


def test_a_request_wakes_the_exporter_before_its_interval(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    clock, calls, state = Clock(), [], {"alive": True}
    clock.hooks += [(4, lambda: (_grow(transcript), _request("session", transcript, "stop")))]
    clock.hooks += [(6, lambda: _request("session", transcript, "prompt"))]
    clock.hooks += [(8, lambda: state.update(alive=False))]
    trace_flush.supervise(
        "session",
        trace_flush.Budget(15, 5, 3),
        lambda *args: calls.append((clock(), args[3])) or True,
        clock,
        clock.sleep,
        lambda owner: state["alive"],
    )
    assert calls == [(0, "request:start"), (4, "request:stop")]


def test_requests_do_not_wake_a_failing_exporter_before_its_interval(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    clock, calls, state = Clock(), [], {"alive": True}
    clock.hooks += [(5, lambda: _request("session", transcript, "stop"))]
    clock.hooks += [(20, lambda: state.update(alive=False))]
    trace_flush.supervise(
        "session",
        trace_flush.Budget(15, 5, 3),
        lambda *args: calls.append((clock(), args[3])) or False,
        clock,
        clock.sleep,
        lambda owner: state["alive"],
    )
    assert calls == [(0, "request:start")] * 3 + [(15, "request:stop")] * 3 + [(20, "final")] * 3


def test_a_resumed_owner_takes_over_the_running_exporter(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript, owner=1)
    clock, calls, owners = Clock(), [], []

    def send(session, path, timeout, trigger):
        calls.append((clock(), trigger))
        if trigger == "final":
            _grow(transcript)
            _request("session", transcript, "start", owner=os.getpid())
        return True

    def is_alive(owner):
        owners.append(owner.pid)
        return owner.pid == os.getpid() and clock() < 20

    assert trace_flush.supervise("session", trace_flush.Budget(15, 5, 3), send, clock, clock.sleep, is_alive) == (
        "owner exited"
    )
    assert calls == [(0, "final"), (0, "request:start")]
    assert owners[0] == 1 and os.getpid() in owners


def test_a_second_exporter_waits_then_leaves_the_session_to_the_first(home, tmp_path):
    trace_flush.owner_path("session").parent.mkdir(parents=True)
    import fcntl

    with trace_flush.owner_path("session").with_suffix(".lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        clock, calls = Clock(), []
        result = trace_flush.supervise(
            "session", trace_flush.Budget(15, 5, 3), lambda *a: calls.append(a) or True, clock, clock.sleep
        )
    assert result == "another exporter owns the session"
    assert calls == []
    assert clock() == 30


def test_supervisor_waits_for_a_draining_owner_lock(home, tmp_path):
    import fcntl

    trace_flush.owner_path("session").parent.mkdir(parents=True)
    held = trace_flush.owner_path("session").with_suffix(".lock").open("a")
    fcntl.flock(held, fcntl.LOCK_EX)
    clock = Clock()
    clock.hooks.append((10, held.close))
    result = trace_flush.supervise(
        "session", trace_flush.Budget(15, 5, 3), lambda *a: True, clock, clock.sleep, lambda owner: False
    )
    assert result == "owner exited"
    assert clock() == 10


def test_codex_transcript_is_resolved_when_the_hook_had_none(home, monkeypatch):
    monkeypatch.setattr("hooks.targets.normalizer.codex_rollout_path", lambda session: f"/rollout/{session}")
    assert trace_flush._transcript("s", {"transcript": "/given"}) == "/given"
    assert trace_flush._transcript("s", {"target": "codex"}) == "/rollout/s"
    assert trace_flush._transcript("s", {"target": "claude"}) == ""


def test_an_attempt_is_killed_at_its_timeout(tmp_path, monkeypatch):
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    monkeypatch.setattr(trace_flush.sys, "executable", str(slow))
    started = time.monotonic()
    assert trace_flush.attempt("s", "/t", 0.5, "interval") is False
    assert time.monotonic() - started < 5
    quick = tmp_path / "quick"
    quick.write_text(f'#!/bin/sh\ntest "${trace_flush.TRIGGER_ENV}" = interval\n')
    quick.chmod(0o755)
    monkeypatch.setattr(trace_flush.sys, "executable", str(quick))
    assert trace_flush.attempt("s", "/t", 5, "interval") is True


class Receiver(http.server.BaseHTTPRequestHandler):
    spans: list = []
    delay = 0.0

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(type(self).delay)
        request = ExportTraceServiceRequest.FromString(body)
        for resource in request.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    attributes = {a.key: a.value for a in span.attributes}
                    type(self).spans.append((span.name, attributes))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def receiver():
    Receiver.spans, Receiver.delay = [], 0.0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def _records(stamp="2026-10-07T10:00:00Z"):
    return [
        {"type": "user", "uuid": "prompt", "timestamp": stamp, "message": {"content": "probe"}},
        {
            "type": "assistant",
            "uuid": "call",
            "timestamp": stamp,
            "message": {
                "id": "generation",
                "model": "model",
                "content": [{"type": "tool_use", "id": "tool", "name": "shell", "input": {"cmd": "probe"}}],
            },
        },
        {
            "type": "user",
            "uuid": "result",
            "timestamp": stamp,
            "message": {"content": [{"type": "tool_result", "tool_use_id": "tool", "content": "done"}]},
        },
    ]


@pytest.fixture
def live_export(home, receiver, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    monkeypatch.setenv("AGENTIHOOKS_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("OTEL_LANGFUSE_ENDPOINT", f"http://127.0.0.1:{receiver.server_port}")
    monkeypatch.setenv("OTEL_LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("OTEL_LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield owner
    owner.kill()
    owner.wait()


def _supervise_in_background(session):
    result = {}
    thread = threading.Thread(
        target=lambda: result.update(
            outcome=trace_flush.supervise(session, trace_flush.Budget(1, 5, 3), sleep=lambda s: time.sleep(0.1))
        ),
        daemon=True,
    )
    thread.start()
    return thread, result


def _wait(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_live_exporter_ships_an_open_turn_and_the_rest_after_the_owner_is_killed(live_export, tmp_path):
    transcript = tmp_path / "t.jsonl"
    records = _records()
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records[:2]))
    _request("session", transcript, owner=live_export.pid)
    thread, result = _supervise_in_background("session")

    def tools():
        return [a for name, a in Receiver.spans if name == "shell"]

    assert _wait(tools, 20), "open turn not exported while the owner was working"
    assert tools()[-1]["tool.outcome.state"].string_value == "missing"
    root = next(a for name, a in Receiver.spans if "langfuse.trace.name" in a)
    assert root["agentihooks.export.trigger"].string_value == "request:start"
    assert root["agentihooks.export.unwritten_events.state"].string_value == "unavailable"
    _grow(transcript, json.dumps(records[2]))
    live_export.kill()
    live_export.wait()
    thread.join(30)
    assert result["outcome"] == "owner exited"
    assert tools()[-1]["langfuse.observation.output"].string_value == "done"
    final = [a for name, a in Receiver.spans if "langfuse.trace.name" in a][-1]
    assert final["agentihooks.export.trigger"].string_value == "final"


def test_slow_endpoint_is_bounded_per_attempt_and_retried_later(live_export, tmp_path):
    Receiver.delay = 3.0
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in _records()))
    _request("session", transcript, owner=live_export.pid)
    limits = trace_flush.Budget(1, 1, 2)
    started = time.monotonic()
    assert trace_flush._drain("session", str(transcript), limits, trace_flush.attempt, "interval") is False
    assert time.monotonic() - started < 2 * 1 + 3
    assert agent_trace._cursor("session")["pending"]
    Receiver.delay = 0.0
    assert trace_flush._drain("session", str(transcript), limits, trace_flush.attempt, "interval") is True
    assert not agent_trace._cursor("session")["pending"]


@pytest.mark.parametrize(
    ("handler", "event", "reason"),
    [
        ("on_session_start", "SessionStart", "start"),
        ("on_user_prompt_submit", "UserPromptSubmit", "prompt"),
        ("on_pre_compact", "PreCompact", "compact"),
        ("on_stop", "Stop", "stop"),
        ("on_session_end", "SessionEnd", "end"),
    ],
)
def test_hooks_only_enqueue_a_flush(handler, event, reason, home, enabled, monkeypatch, tmp_path):
    from hooks import hook_manager

    requests, forked = [], []
    monkeypatch.setattr(trace_flush, "request", lambda *args: requests.append(args))
    monkeypatch.setattr("hooks.lifecycle.deps_kick.kick", lambda: None)
    monkeypatch.setattr("hooks.observability.transcript.POSITION_DIR", tmp_path / "positions")
    monkeypatch.setattr("hooks._async.fork_and_call", lambda fn, *a, **k: forked.append(fn.__name__))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    payload = {"hook_event_name": event, "session_id": "s", "transcript_path": str(transcript), "cwd": str(tmp_path)}
    payload["prompt"] = "hello"
    getattr(hook_manager, handler)(payload)
    assert requests == [("s", str(transcript), reason)]
    assert "export_session" not in forked
