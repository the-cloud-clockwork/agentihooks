import fcntl
import http.client
import http.server
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span

from hooks.observability import agent_trace, otel, trace_flush


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "agent_trace")
    return tmp_path


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(otel, "langfuse_exporter_config", lambda: {"endpoint": "http://unused", "headers": {}})


class Clock:
    def __init__(self, limit=200):
        self.now = 0.0
        self.hooks = []
        self.reads = 0
        self.limit = limit

    def __call__(self):
        self.reads += 1
        if self.reads > 5_000:
            raise RuntimeError("clock read without progress")
        return self.now

    def sleep(self, seconds):
        assert seconds == trace_flush.POLL_SEC
        self.now += seconds
        if self.now > self.limit:
            raise RuntimeError("supervisor never exited")
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
    clock, calls, owners, owners_file = Clock(), [], [], []

    def send(session, path, timeout, trigger):
        calls.append((clock(), trigger))
        owners_file.append(json.loads(trace_flush.owner_path("session").read_text()))
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
    me = {"supervisor_pid": os.getpid(), "supervisor_start": trace_flush.start_time(os.getpid())}
    assert owners_file == [me, me]


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


def test_an_attempt_is_killed_at_its_timeout(tmp_path, monkeypatch, capsys):
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    monkeypatch.setattr(trace_flush.sys, "executable", str(slow))
    started = time.monotonic()
    assert trace_flush.attempt("s", "/t", 0.5, "interval") is False
    assert time.monotonic() - started < 5
    assert capsys.readouterr().err == "trace_flush s: attempt timed out after 0.5s\n"
    quick = tmp_path / "quick"
    quick.write_text(f'#!/bin/sh\ntest "${trace_flush.TRIGGER_ENV}" = interval\n')
    quick.chmod(0o755)
    monkeypatch.setattr(trace_flush.sys, "executable", str(quick))
    assert trace_flush.attempt("s", "/t", 5, "interval") is True


class Receiver(http.server.BaseHTTPRequestHandler):
    spans: list = []
    posts = 0
    delay = 0.0

    def do_POST(self):
        spans = type(self).spans
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).posts += 1
        time.sleep(type(self).delay)
        request = ExportTraceServiceRequest.FromString(body)
        for resource in request.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    attributes = {a.key: a.value for a in span.attributes}
                    spans.append((span.name, attributes))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def receiver():
    Receiver.spans, Receiver.posts, Receiver.delay = [], 0, 0.0
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


def test_a_late_post_lands_in_the_spans_current_when_it_arrived(receiver):
    Receiver.delay = 1.0
    body = ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[Span(name="late")])])]
    ).SerializeToString()
    earlier = Receiver.spans
    connection = http.client.HTTPConnection("127.0.0.1", receiver.server_port, timeout=10)
    post = threading.Thread(target=lambda: (connection.request("POST", "/", body), connection.getresponse().read()))
    post.start()
    assert _wait(lambda: Receiver.posts == 1, 5)
    Receiver.spans = []
    post.join(10)
    assert Receiver.spans == []
    assert earlier == [("late", {})]


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
    Receiver.delay = 30.0
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in _records()))
    _request("session", transcript, owner=live_export.pid)
    started = time.monotonic()
    assert (
        trace_flush._drain("session", str(transcript), trace_flush.Budget(1, 8, 1), trace_flush.attempt, "x") is False
    )
    assert time.monotonic() - started < 8 + 4
    assert Receiver.posts, "the attempt never reached the endpoint"
    assert agent_trace._cursor("session")["pending"]
    Receiver.delay = 0.0
    assert (
        trace_flush._drain("session", str(transcript), trace_flush.Budget(1, 30, 1), trace_flush.attempt, "x") is True
    )
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


def test_hook_request_passes_empty_defaults_and_logs_a_failure(monkeypatch):
    from hooks import hook_manager

    requests, logged = [], []
    monkeypatch.setattr(trace_flush, "request", lambda *args: requests.append(args))
    monkeypatch.setattr(hook_manager, "log", lambda *args: logged.append(args))
    hook_manager._request_trace_flush({}, "stop")
    assert requests == [("", "", "stop")]

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(trace_flush, "request", fail)
    hook_manager._request_trace_flush({"session_id": "s"}, "stop")
    assert logged == [("trace flush request failed", {"error": "disk full"})]


def _stat(proc, pid, start, comm="agent"):
    (proc / str(pid)).mkdir()
    fields = " ".join(str(n) for n in range(1, 19))
    (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S {fields} {start} 0\n")


def test_owner_liveness_reads_the_given_proc_table(tmp_path):
    _stat(tmp_path, 1, 7)
    _stat(tmp_path, 2, 9, comm="a) b")
    assert trace_flush.start_time(2, tmp_path) == 9
    assert trace_flush.alive(trace_flush.Owner(2, 9), tmp_path)
    assert not trace_flush.alive(trace_flush.Owner(2, 8), tmp_path)
    assert not trace_flush.alive(trace_flush.Owner(1, 7), tmp_path)
    assert not trace_flush.alive(trace_flush.Owner(3, 0), tmp_path)


def test_state_file_names_and_owner_parsing(home):
    assert trace_flush.request_path("s").name == "s.request.json"
    assert trace_flush.owner_path("s").name == "s.owner.json"
    assert trace_flush.owner_path("s").parent == agent_trace.CURSOR_DIR
    assert trace_flush._owner({}, "owner_") == trace_flush.Owner(0, 0)
    assert trace_flush._owner({"owner_pid": "5", "owner_start": 6}, "owner_") == trace_flush.Owner(5, 6)
    assert trace_flush._owner({"owner_pid": None, "owner_start": 6}, "owner_") == trace_flush.Owner(0, 0)
    assert trace_flush._owner({"owner_pid": "x", "owner_start": 6}, "owner_") == trace_flush.Owner(0, 0)


def test_request_record_defaults(home, enabled, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    assert not trace_flush.request("", "/t", "start", owner_pid=os.getpid(), spawn=lambda s: None)
    trace_flush.request("s", "", "start", owner_pid=os.getpid(), spawn=lambda s: None)
    first = json.loads(trace_flush.request_path("s").read_text())
    assert first["transcript"] == "" and first["target"] == "claude"
    assert isinstance(first["at"], int) and first["at"] > 0
    trace_flush.request("s", "", "stop", owner_pid=os.getpid(), spawn=lambda s: None)
    assert json.loads(trace_flush.request_path("s").read_text())["at"] > first["at"]


def test_spawn_detaches_the_exporter(monkeypatch):
    calls = []
    monkeypatch.setattr("hooks._async.fork_and_call", lambda fn, *a, **k: calls.append((fn, a, k)))
    trace_flush._spawn("s")
    assert calls == [
        (trace_flush.run, ("s",), {"timeout_sec": 7 * 24 * 3600, "task_name": "trace_flush"}),
        (trace_flush.recover, (), {"timeout_sec": 60, "task_name": "trace_recovery"}),
    ]


def test_run_clears_the_alarm_lowers_priority_and_logs_the_outcome(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(trace_flush.signal, "alarm", lambda n: calls.append(("alarm", n)))
    monkeypatch.setattr(trace_flush.os, "nice", lambda n: calls.append(("nice", n)))
    monkeypatch.setattr(trace_flush, "supervise", lambda s: calls.append(("supervise", s)) or "owner exited")
    trace_flush.run("s")
    assert calls == [("alarm", 0), ("nice", 10), ("supervise", "s")]
    assert capsys.readouterr().err == "trace_flush s: owner exited\n"


def test_flush_once_reports_pending_work(monkeypatch, home):
    calls = []

    def export(session, path):
        calls.append((session, path))
        trace_flush._write(agent_trace._cursor_path(session), {"pending": pending})

    monkeypatch.setattr(agent_trace, "export_session", export)
    pending = [{"span": 1}]
    assert trace_flush.flush_once("s", "/t") == 1
    pending = []
    assert trace_flush.flush_once("s", "/t") == 0
    assert calls == [("s", "/t"), ("s", "/t")]


def test_stopped_codex_cursor_replays_all_pages_once(home, monkeypatch):
    from tests.observability.test_trace_paging import Receiver, _codex, _line

    transcript = home / "rollout.jsonl"
    transcript.write_text("".join(_line(record) for record in _codex(6, 2)))
    receiver = Receiver()
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", 6000)
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: receiver)
    monkeypatch.setattr(agent_trace, "_root_attributes", lambda session: {})
    monkeypatch.setattr(agent_trace, "_collector_outcomes", lambda *args: None)
    monkeypatch.setattr("hooks.context.context_usage.session_cost", lambda session: None)
    trace_flush._write(
        agent_trace._cursor_path("session"),
        {
            "version": 2,
            "records": {},
            "accepted": {},
            "pending": [],
            "source": {},
            "overflow": {"bytes": transcript.stat().st_size, "limit": 6000},
        },
    )

    assert trace_flush.flush_once("session", str(transcript)) == 0
    state = agent_trace._cursor("session")
    assert state["source"]["accepted_bytes"] == transcript.stat().st_size
    tools = [span for span in receiver.observations.values() if span.attributes["langfuse.observation.type"] == "tool"]
    assert len(tools) == 12
    before = dict(receiver.observations)
    calls = receiver.calls
    assert trace_flush.flush_once("session", str(transcript)) == 0
    assert trace_flush.flush_once("session", str(transcript)) == 0
    assert receiver.calls == calls
    assert receiver.observations == before


def test_new_exporter_recovers_stopped_codex_gaps_and_skips_completed_sessions(home, enabled, monkeypatch):
    from tests.observability.test_trace_paging import Receiver, _codex, _line

    transcript = home / "rollout.jsonl"
    transcript.write_text("".join(_line(record) for record in _codex(6, 2)))
    receiver = Receiver()
    monkeypatch.setattr(agent_trace, "PENDING_MAX_BYTES", 6000)
    monkeypatch.setattr(otel, "langfuse_exporter", lambda: receiver)
    monkeypatch.setattr(agent_trace, "_root_attributes", lambda session: {})
    monkeypatch.setattr(agent_trace, "_collector_outcomes", lambda *args: None)
    monkeypatch.setattr("hooks.context.context_usage.session_cost", lambda session: None)
    record = {"target": "codex", "transcript": str(transcript), "owner_pid": 0}
    trace_flush._write(trace_flush.request_path("session"), record)
    trace_flush._write(
        agent_trace._cursor_path("session"),
        {
            "version": 2,
            "records": {},
            "accepted": {},
            "pending": [],
            "source": {},
            "overflow": {"bytes": transcript.stat().st_size, "limit": 6000},
        },
    )
    calls = []
    monkeypatch.setattr("hooks._async.fork_and_call", lambda fn, *args, **kwargs: calls.append((fn, args)))
    trace_flush._spawn("new-session")
    assert (trace_flush.recover, ()) in calls

    def send(session, path, timeout, trigger):
        assert trigger == "recovery"
        return trace_flush.flush_once(session, path) == 0

    assert trace_flush.recover(send=send) == 1
    assert agent_trace._cursor("session")["source"]["accepted_bytes"] == transcript.stat().st_size
    before = receiver.calls
    monkeypatch.setattr(trace_flush.time, "time", lambda: 10**20)
    assert trace_flush.recover(send=send) == 0
    assert trace_flush.recover(send=send) == 0
    assert receiver.calls == before


def _gap(session, transcript, accepted=0, **cursor):
    trace_flush._write(
        trace_flush.request_path(session), {"target": "codex", "transcript": str(transcript), "owner_pid": 0}
    )
    trace_flush._write(agent_trace._cursor_path(session), {"source": {"accepted_bytes": accepted}, **cursor})


@pytest.mark.parametrize(
    ("record", "cursor", "supervised", "found"),
    [
        ({"target": "claude"}, {"source": {"accepted_bytes": 0}}, False, False),
        ({"owner_pid": os.getpid(), "owner_start": trace_flush.start_time(os.getpid())}, {"source": {}}, False, False),
        ({}, {"source": {}}, True, False),
        ({}, None, False, False),
        ({"transcript": "/missing/rollout.jsonl"}, {"source": {}}, False, False),
        ({}, {"source": {"accepted_bytes": 10}}, False, False),
        ({}, {"pending": [{}], "source": {"accepted_bytes": 10}}, False, True),
        ({}, {"source": {"accepted_bytes": 9}}, False, True),
        ({}, {"pending": []}, False, True),
    ],
)
def test_replay_source_picks_stopped_codex_sessions_behind_their_rollout(
    home, monkeypatch, record, cursor, supervised, found
):
    transcript = home / "rollout.jsonl"
    transcript.write_text("0123456789")
    monkeypatch.setattr(
        "hooks.targets.normalizer.codex_rollout_path", lambda session: str(transcript) if session == "s" else ""
    )
    if cursor is not None:
        trace_flush._write(agent_trace._cursor_path("s"), cursor)
    if supervised:
        me = {"supervisor_pid": os.getpid(), "supervisor_start": trace_flush.start_time(os.getpid())}
        trace_flush._write(trace_flush.owner_path("s"), me)
    assert trace_flush._replay_source("s", {"target": "codex", **record}) == (str(transcript) if found else "")


def test_replay_source_counts_a_missing_cursor_offset_as_zero(home):
    transcript = home / "rollout.jsonl"
    transcript.write_text("0")
    trace_flush._write(agent_trace._cursor_path("s"), {"source": {}})
    assert trace_flush._replay_source("s", {"target": "codex", "transcript": str(transcript)}) == str(transcript)


def test_flush_once_stops_when_a_page_makes_no_progress(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_trace, "export_session", lambda *args: calls.append(args))
    stuck = {"overflow": {"bytes": 9}, "source": {"accepted_bytes": 0}}
    monkeypatch.setattr(agent_trace, "_cursor", lambda session: stuck if len(calls) < 4 else {})
    assert trace_flush.flush_once("s", "/t") == 1
    assert calls == [("s", "/t"), ("s", "/t")]


def test_recover_skips_while_another_scan_holds_the_lock(home):
    _gap("A", home / "missing")
    with (agent_trace.CURSOR_DIR / "recovery.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert trace_flush.recover(send=lambda *args: pytest.fail("sent while locked")) == 0
    assert not (agent_trace.CURSOR_DIR / "recovery.scan.json").exists()


def test_recover_creates_a_missing_cursor_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "missing" / "agent_trace")
    assert trace_flush.recover() == 0
    assert (tmp_path / "missing" / "agent_trace" / "recovery.lock").exists()


def test_recover_rotates_bounded_scans_and_retries_a_failed_replay(home, monkeypatch):
    transcript = home / "rollout.jsonl"
    transcript.write_text("0123456789")
    for session in ("A", "M", "Y", "Z"):
        _gap(session, transcript)
    _gap("B", transcript, accepted=10)
    sent, failed = [], []

    def send(session, path, timeout, trigger):
        assert (path, trigger) == (str(transcript), "recovery")
        sent.append(session)
        if session == "Y" and not failed:
            failed.append(session)
            return False
        trace_flush._write(agent_trace._cursor_path(session), {"source": {"accepted_bytes": 10}})
        return True

    now = [60.0]
    monkeypatch.setattr(trace_flush.time, "time", lambda: now[0])
    stamp = agent_trace.CURSOR_DIR / "recovery.scan.json"
    limits = trace_flush.Budget(attempts=1)

    assert trace_flush.recover(limits, send) == 3
    assert sent == ["A", "M", "Y"]
    assert json.loads(stamp.read_text()) == {"at": 60.0, "after": "Y.request.json"}
    now[0] = 119.0
    assert trace_flush.recover(limits, send) == 0
    assert sent == ["A", "M", "Y"]
    now[0] = 120.0
    assert trace_flush.recover(limits, send) == 2
    assert sent == ["A", "M", "Y", "Z", "Y"]
    assert json.loads(stamp.read_text()) == {"at": 120.0, "after": "Y.request.json"}
    now[0] = 180.0
    assert trace_flush.recover(limits, send) == 0
    assert sent == ["A", "M", "Y", "Z", "Y"]
    assert json.loads(stamp.read_text()) == {"at": 180.0, "after": "Y.request.json"}


def test_size_of_a_missing_transcript(tmp_path):
    assert trace_flush._size(str(tmp_path / "missing")) is None
    (tmp_path / "t").write_text("abc")
    assert trace_flush._size(str(tmp_path / "t")) == 3


def _run(session, limits, outcome, clock, state):
    calls = []

    def send(session, path, timeout, trigger):
        calls.append((clock(), trigger, timeout, path))
        return outcome

    result = trace_flush.supervise(session, limits, send, clock, clock.sleep, lambda owner: state["alive"])
    return result, calls


def test_custom_budget_is_used_for_interval_attempts_and_timeout(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    clock, state = Clock(), {"alive": True}
    clock.hooks += [(25, lambda: state.update(alive=False))]
    result, calls = _run("session", trace_flush.Budget(10, 4, 2), False, clock, state)
    assert result == "owner exited"
    assert [(at, trigger, timeout) for at, trigger, timeout, _ in calls] == (
        [(0, "request:start", 4)] * 2 + [(10, "interval", 4)] * 2 + [(20, "interval", 4)] * 2 + [(25, "final", 4)] * 2
    )
    assert {path for *_, path in calls} == {str(transcript)}


def test_default_budget_is_read_when_none_is_given(home, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TRACE_FLUSH_ATTEMPTS", "2")
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    clock, state = Clock(), {"alive": False}
    result, calls = _run("session", None, False, clock, state)
    assert [(at, trigger, timeout) for at, trigger, timeout, _ in calls] == [(0, "final", 5.0)] * 2


def test_nothing_is_sent_without_a_readable_transcript(home, tmp_path):
    _request("session", tmp_path / "missing.jsonl")
    clock, state = Clock(), {"alive": True}
    clock.hooks += [(20, lambda: state.update(alive=False))]
    result, calls = _run("session", trace_flush.Budget(15, 5, 3), True, clock, state)
    assert result == "owner exited" and calls == []
    trace_flush._write(trace_flush.request_path("session"), {"owner_pid": 0, "reason": "start", "at": 1})
    result, calls = _run("session", trace_flush.Budget(15, 5, 3), True, Clock(), state)
    assert calls == []


def test_a_wake_without_a_transcript_does_not_hold_later_requests(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    trace_flush._write(
        trace_flush.request_path("session"),
        {"owner_pid": os.getpid(), "owner_start": trace_flush.start_time(os.getpid()), "target": "claude", "at": 1},
    )
    clock, state = Clock(), {"alive": True}
    clock.hooks += [(4, lambda: _request("session", transcript, "prompt"))]
    clock.hooks += [(6, lambda: state.update(alive=False))]
    result, calls = _run("session", trace_flush.Budget(15, 5, 3), True, clock, state)
    assert [(at, trigger) for at, trigger, *_ in calls] == [(4, "request:prompt")]


def test_a_request_without_a_reason_is_named_plainly(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    record = json.loads(trace_flush.request_path("session").read_text())
    del record["reason"]
    trace_flush._write(trace_flush.request_path("session"), record)
    clock, state = Clock(), {"alive": True}
    clock.hooks += [(1, lambda: state.update(alive=False))]
    result, calls = _run("session", trace_flush.Budget(15, 5, 3), True, clock, state)
    assert [trigger for _, trigger, *_ in calls] == ["request:"]


def test_the_owner_record_names_the_running_exporter_until_it_exits(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    me = {"supervisor_pid": os.getpid(), "supervisor_start": trace_flush.start_time(os.getpid())}
    seen = []
    clock, state = Clock(), {"alive": True}
    clock.hooks += [(3, lambda: (_grow(transcript), state.update(alive=False)))]

    def send(*args):
        seen.append(json.loads(trace_flush.owner_path("session").read_text()))
        return True

    trace_flush.supervise("session", trace_flush.Budget(15, 5, 3), send, clock, clock.sleep, lambda o: state["alive"])
    assert seen == [me, me]
    assert not trace_flush.owner_path("session").exists()


def test_exporters_create_their_state_folder_and_run_one_after_another(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path / "a" / "b")
    for _ in range(2):
        clock = Clock()
        assert trace_flush.supervise("s", trace_flush.Budget(15, 5, 3), None, clock, clock.sleep, lambda o: False) == (
            "owner exited"
        )


def test_root_reports_the_trigger_and_unwritten_events(monkeypatch, home):
    monkeypatch.setattr("hooks.observability.correlation.resolve", lambda session: {})
    monkeypatch.setattr("hooks.observability.signals.attributes", lambda session: {})
    monkeypatch.delenv(agent_trace.TRIGGER_ENV, raising=False)
    root = agent_trace._root_attributes("s")
    assert root["agentihooks.export.trigger"] == "direct"
    assert root["agentihooks.export.unwritten_events.state"] == "unavailable"
    monkeypatch.setenv("AGENTIHOOKS_TRACE_FLUSH_TRIGGER", "interval")
    assert agent_trace._root_attributes("s")["agentihooks.export.trigger"] == "interval"


def test_the_trigger_does_not_make_an_observation_new():
    spec = agent_trace.SpanSpec("root", 1, None, 0, 1, {"agentihooks.export.trigger": "interval", "a": 1})
    later = agent_trace.SpanSpec("root", 1, None, 0, 1, {"agentihooks.export.trigger": "final", "a": 1})
    changed = agent_trace.SpanSpec("root", 1, None, 0, 1, {"agentihooks.export.trigger": "final", "a": 2})
    assert agent_trace._revision(spec) == agent_trace._revision(later) != agent_trace._revision(changed)


def test_state_writes_create_folders_and_stay_beside_their_target(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "no-shared-temp"))
    target = tmp_path / "a" / "b" / "s.request.json"
    trace_flush._write(target, {"at": 1})
    trace_flush._write(target, {"at": 2})
    assert json.loads(target.read_text()) == {"at": 2}
    assert [p.name for p in target.parent.iterdir()] == ["s.request.json"]


def test_the_exporter_checks_the_owner_named_in_the_request(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    owners = []
    clock = Clock()
    trace_flush.supervise(
        "session", trace_flush.Budget(15, 5, 3), lambda *a: True, clock, clock.sleep, lambda o: owners.append(o)
    )
    assert owners[0] == trace_flush.Owner(os.getpid(), trace_flush.start_time(os.getpid()))


def test_the_exporter_resolves_a_codex_rollout_for_its_session(home, tmp_path, monkeypatch):
    rollout = tmp_path / "rollout-session.jsonl"
    _grow(rollout)
    monkeypatch.setattr("hooks.targets.normalizer.codex_rollout_path", lambda s: str(tmp_path / f"rollout-{s}.jsonl"))
    trace_flush._write(
        trace_flush.request_path("session"),
        {"owner_pid": 0, "owner_start": 0, "target": "codex", "reason": "prompt", "at": 1},
    )
    clock, state = Clock(), {"alive": False}
    result, calls = _run("session", trace_flush.Budget(15, 5, 3), True, clock, state)
    assert [(trigger, path) for _, trigger, _, path in calls] == [("final", str(rollout))]


def test_a_missing_owner_record_does_not_stop_the_final_drain(home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    _grow(transcript)
    _request("session", transcript)
    clock = Clock()

    def send(*args):
        trace_flush.owner_path("session").unlink()
        return True

    result = trace_flush.supervise("session", trace_flush.Budget(15, 5, 3), send, clock, clock.sleep, lambda o: False)
    assert result == "owner exited"
