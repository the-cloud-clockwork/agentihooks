from __future__ import annotations

from scripts import serena_router_daemon as daemon


def test_unit_renders_python_port_and_checkout(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SERENA_ROUTER_PORT", "9911")
    unit = daemon.render_unit("/opt/py/bin/python3")
    assert "ExecStart=/opt/py/bin/python3 -m hooks.serena_router --host 127.0.0.1 --port 9911" in unit
    assert f"WorkingDirectory={daemon.REPO_ROOT}" in unit
    assert "Environment=SERENA_USAGE_REPORTING=false" in unit
    assert "__" not in unit


def test_release_reports_failure_when_router_is_down(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SERENA_ROUTER_PORT", "1")
    assert daemon.release("/nowhere") == 1
