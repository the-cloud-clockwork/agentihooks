import json
import os
import time
from pathlib import Path

import pytest

from scripts import init_agent
from scripts.profiles import binding


def _slow_route(tmp_path, monkeypatch, detach, route_timeout):
    from scripts import select_profile

    home = tmp_path / "master" / "claude"
    home.mkdir(parents=True)
    monkeypatch.setattr(
        select_profile,
        "prepare",
        lambda *a: ({"AGENTIHOOKS_PROFILE": "master", binding.HOMES["claude"]: str(home)}, a[4]),
    )
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: ("claude", "explicit"))
    monkeypatch.setattr(init_agent.claude_trust, "ensure_trusted", lambda *a: ("trusted", ""))
    renamed = []
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *a: renamed.append(a) or True)
    request = binding.request
    requested = []
    monkeypatch.setattr(
        binding,
        "request",
        lambda report, profile, agent, home: requested.append(report) or request(report, profile, agent),
    )

    def launch(launcher, directory, name, args, agent, environ):
        init_agent._started_marker(launcher).touch()
        return ["pane_id=w1:p1"]

    monkeypatch.setattr(init_agent, "_start_herdr", launch)
    monkeypatch.setattr(init_agent, "_detach", lambda: detach)
    argv = ["--host", "herdr", "--agent", "claude", "--profile", "master", "--dir", str(tmp_path), "--defer-canary"]
    result = init_agent.main([*argv, "--route-timeout", str(route_timeout)], {"XDG_RUNTIME_DIR": str(tmp_path / "rt")})
    return result, renamed, requested


@pytest.mark.wall_clock
def test_a_deferred_launch_returns_within_seconds_of_a_slow_route(tmp_path, monkeypatch):
    started = time.monotonic()
    assert _slow_route(tmp_path, monkeypatch, detach=True, route_timeout=10)[0] == 0
    assert time.monotonic() - started < 5


@pytest.mark.wall_clock
def test_the_detached_child_waits_its_whole_route_timeout(tmp_path, monkeypatch):
    started = time.monotonic()
    assert _slow_route(tmp_path, monkeypatch, detach=False, route_timeout=1)[0] == 3
    assert time.monotonic() - started >= 1


def test_a_deferred_launch_returns_before_a_slow_route(tmp_path, monkeypatch, capsys):
    result, renamed, _ = _slow_route(tmp_path, monkeypatch, detach=True, route_timeout=10)
    assert result == 0
    assert renamed == []
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "host=herdr"
    assert lines[-5] == "status=started"
    assert lines[-4].startswith("launcher_at=")
    assert lines[-3:-1] == ["route_status=pending", "profile_validation=pending"]
    report = lines[-1].removeprefix("profile_report=")
    assert json.loads(Path(report).read_text()) == {"profile": "master", "harness": "claude", "state": "pending"}


def test_the_detached_child_of_a_deferred_launch_still_waits_on_the_route(tmp_path, monkeypatch, capsys):
    result, renamed, requested = _slow_route(tmp_path, monkeypatch, detach=False, route_timeout=1)
    assert result == 3
    assert renamed == []
    captured = capsys.readouterr()
    assert captured.err == "agentihooks init-agent: the harness did not report its route within 1s\n"
    (report,) = requested
    assert json.loads(Path(report).read_text()) == {
        "profile": "master",
        "harness": "claude",
        "state": "failed",
        "reason": "the harness did not report its route within 1s",
    }


def test_a_spent_route_wait_never_sleeps(tmp_path, monkeypatch):
    monkeypatch.setattr(init_agent.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(init_agent.time, "sleep", lambda seconds: pytest.fail("the route wait slept"))
    assert init_agent._await_route(tmp_path / "route", 0, {}) == ({}, None)


def test_the_route_wait_reads_a_report_written_while_it_sleeps(tmp_path, monkeypatch):
    route = tmp_path / "route"
    slept = []

    def sleep(seconds):
        if slept:
            pytest.fail("the route wait slept on after its report arrived")
        slept.append(seconds)
        route.write_text("status=routed\naccount=acct\n")

    monkeypatch.setattr(init_agent.time, "sleep", sleep)
    found, written = init_agent._await_route(route, 30, {})
    assert found == {"status": "routed", "account": "acct"}
    assert isinstance(written, int)
    assert slept == [0.25]
    assert not route.exists()


def test_only_a_deferred_launch_refuses_its_profile_request(tmp_path):
    report = tmp_path / "report.json"
    binding.request(report, "master", "claude")
    plain = init_agent._parser().parse_args([])
    init_agent._refuse_deferred(plain, {binding.REPORT: str(report)}, ValueError("route lost"))
    assert json.loads(report.read_text())["state"] == "pending"
    deferred = init_agent._parser().parse_args(["--defer-canary"])
    init_agent._refuse_deferred(deferred, {}, ValueError("route lost"))
    assert json.loads(report.read_text())["state"] == "pending"
    init_agent._refuse_deferred(deferred, {binding.REPORT: str(report)}, ValueError("route lost"))
    assert json.loads(report.read_text()) == {
        "profile": "master",
        "harness": "claude",
        "state": "failed",
        "reason": "route lost",
    }


def test_only_a_deferred_launch_with_a_profile_request_detaches(monkeypatch):
    calls = []
    monkeypatch.setattr(init_agent, "_detach", lambda: calls.append(1) or True)
    deferred, plain = init_agent._parser().parse_args(["--defer-canary"]), init_agent._parser().parse_args([])
    assert init_agent._detached(plain, {binding.REPORT: "/r.json"}) is False
    assert init_agent._detached(deferred, {}) is False
    assert calls == []
    assert init_agent._detached(deferred, {binding.REPORT: "/r.json"}) is True
    assert calls == [1]


def test_detach_returns_in_the_caller_and_carries_on_in_a_new_session():
    read, write, caller = *os.pipe(), os.getpid()
    held, (stdin, feed) = os.dup(0), os.pipe()
    os.dup2(stdin, 0)
    try:
        detached = init_agent._detach()
    finally:
        if os.getpid() == caller:
            os.dup2(held, 0)
            for descriptor in (held, stdin, feed):
                os.close(descriptor)
    if os.getpid() != caller:
        try:
            streams = [os.readlink(f"/proc/self/fd/{number}") for number in (0, 1, 2)]
            os.write(write, json.dumps([detached, os.getsid(0) == os.getpid(), *streams]).encode())
        finally:
            os._exit(0)
    assert detached is True
    os.close(write)
    seen = b""
    while chunk := os.read(read, 4096):
        seen += chunk
    os.close(read)
    os.wait()
    assert json.loads(seen) == [False, True, os.devnull, os.devnull, os.devnull]
