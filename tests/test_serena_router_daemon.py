from __future__ import annotations

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
    assert daemon.release("/nowhere") == 1
