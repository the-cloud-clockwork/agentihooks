from __future__ import annotations

import subprocess

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
