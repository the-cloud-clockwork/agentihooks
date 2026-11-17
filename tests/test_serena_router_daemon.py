from __future__ import annotations

import itertools
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts import serena_router_daemon as daemon


def test_unit_renders_python_port_and_checkout(monkeypatch, tmp_path):
    shim = tmp_path / "bin" / "serena"
    shim.parent.mkdir()
    shim.write_text("#!/bin/sh\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim.parent}:/usr/bin:/mnt/c/Program Files/x")
    monkeypatch.setenv("AGENTIHOOKS_SERENA_ROUTER_PORT", "9911")
    unit = daemon.render_unit("/opt/py/bin/python3")
    assert f"--port 9911 --serena {shim}" in unit
    assert f'Environment="PATH={shim.parent}:/usr/bin:/mnt/c/Program Files/x"' in unit
    assert f"WorkingDirectory={daemon.REPO_ROOT}" in unit
    assert "Environment=SERENA_USAGE_REPORTING=false" in unit
    assert "__" not in unit


def test_unit_passes_the_bundle_context_when_the_bundle_ships_one(monkeypatch, tmp_path):
    context = tmp_path / "claude-code-worktrees.yml"
    monkeypatch.setattr(daemon, "bundle_context", lambda: context)
    assert f"--context {context}\n" in daemon.render_unit("/py")
    monkeypatch.setattr(daemon, "bundle_context", lambda: None)
    unit = daemon.render_unit("/py")
    assert "--context" not in unit and "__" not in unit


def test_release_reports_failure_when_router_is_down(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SERENA_ROUTER_PORT", "1")
    monkeypatch.setattr(daemon.mcp_daemon, "port_open", lambda *a, **k: False)
    assert daemon.release("/nowhere") == 1


def test_ensure_reports_whether_the_started_router_came_up(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SERENA", "true")
    monkeypatch.setattr(daemon, "start", lambda: 0)
    assert daemon.ensure() is True
    monkeypatch.delenv("AGENTIHOOKS_SERENA")
    monkeypatch.setattr(daemon, "start", lambda: 1)
    assert daemon.ensure() is False


def test_ensure_skips_the_router_when_serena_is_off(monkeypatch):
    def refuse():
        raise AssertionError("the router starts with Serena off")

    monkeypatch.setenv("AGENTIHOOKS_SERENA", "false")
    monkeypatch.setattr(daemon, "start", refuse)
    assert daemon.ensure() is True


@pytest.mark.parametrize("error", [OSError("no serena"), subprocess.TimeoutExpired("systemctl", 10)])
def test_ensure_reports_a_router_start_that_raises_as_down(monkeypatch, capsys, error):
    def fail():
        raise error

    monkeypatch.delenv("AGENTIHOOKS_SERENA", raising=False)
    monkeypatch.setattr(daemon, "start", fail)
    assert daemon.ensure() is False
    assert capsys.readouterr().err == f"serena router failed to start: {error}\n"


class _Clock:
    """The start wait's clock: each sleep moves it forward without waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Router:
    """A started router process that exited with returncode, or still runs while returncode is None."""

    pid = 4242

    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def _start_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probes: Iterator[bool]) -> _Clock:
    clock = _Clock()

    def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
        return next(probes)

    monkeypatch.setenv("AGENTIHOOKS_SERENA_ROUTER_PORT", "9911")
    monkeypatch.setattr(daemon, "time", clock)
    monkeypatch.setattr(daemon, "bundle_context", lambda: None)
    monkeypatch.setattr(daemon.mcp_daemon, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(daemon.mcp_daemon, "port_open", port_open)
    return clock


def _pod(monkeypatch: pytest.MonkeyPatch, router: _Router) -> None:
    def systemd(python: str) -> str:
        raise AssertionError(f"a pod without a systemd user bus started the router unit with {python}")

    def popen(argv: list[str], **kwargs: object) -> _Router:
        assert argv[1:3] == ["-m", "hooks.serena_router"]
        return router

    monkeypatch.setattr(daemon.mcp_daemon, "_systemctl_usable", lambda: False)
    monkeypatch.setattr(daemon, "_start_systemd", systemd)
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)


@pytest.mark.feature("serena-router-start-stops-waiting-on-an-exited-router")
def test_start_reports_the_router_down_as_soon_as_its_process_exits_before_opening_its_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _start_wait(monkeypatch, tmp_path, itertools.repeat(False))
    _pod(monkeypatch, _Router(1))

    assert daemon.start("/opt/venv/bin/python") == 1
    assert clock.now == 0
    log = tmp_path / "logs" / "serena-router.log"
    assert capsys.readouterr().err == f"serena router exited 1 before opening http://127.0.0.1:9911; see {log}\n"
    assert (tmp_path / "serena-router.pid").read_text() == "4242"


@pytest.mark.feature("serena-router-start-stops-waiting-on-an-exited-router")
def test_start_keeps_waiting_for_a_running_router_until_its_port_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _start_wait(monkeypatch, tmp_path, iter([False, False, False, True]))
    _pod(monkeypatch, _Router(None))

    assert daemon.start("/opt/venv/bin/python") == 0
    assert clock.now == 2
    assert capsys.readouterr().out == "serena router running at http://127.0.0.1:9911\n"


def test_start_waits_on_the_systemd_unit_port_with_no_router_process_to_watch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[str] = []

    def systemd(python: str) -> str:
        started.append(python)
        return ""

    def pidfile(python: str) -> _Router:
        raise AssertionError(f"a systemd user session started the router process with {python}")

    clock = _start_wait(monkeypatch, tmp_path, iter([False, False, True]))
    monkeypatch.setattr(daemon.mcp_daemon, "_systemctl_usable", lambda: True)
    monkeypatch.setattr(daemon, "_start_systemd", systemd)
    monkeypatch.setattr(daemon, "_start_pidfile", pidfile)

    assert daemon.start("/opt/venv/bin/python") == 0
    assert started == ["/opt/venv/bin/python"]
    assert clock.now == 1
    assert capsys.readouterr().out == "serena router running at http://127.0.0.1:9911\n"


def test_start_reports_a_systemd_unit_that_fails_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _start_wait(monkeypatch, tmp_path, itertools.repeat(False))
    monkeypatch.setattr(daemon.mcp_daemon, "_systemctl_usable", lambda: True)
    monkeypatch.setattr(daemon, "_start_systemd", lambda python: "unit masked")

    assert daemon.start("/opt/venv/bin/python") == 1
    assert clock.now == 0
    assert capsys.readouterr().err == "serena router failed to start: unit masked\n"
