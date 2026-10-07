import os
import subprocess

import pytest

from scripts.swarm_ledger import ledger_server

EXE = "/opt/bin/agentihooks"


@pytest.fixture
def calls(monkeypatch):
    seen = {"run": [], "terminate": []}

    def run(argv, **kwargs):
        seen["run"].append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ledger_server.shutil, "which", lambda name: EXE if name == "agentihooks" else None)
    monkeypatch.setattr(ledger_server.subprocess, "run", run)
    monkeypatch.setattr(ledger_server, "swarm_status", lambda slug: {"swarm": slug})
    monkeypatch.setattr(
        ledger_server, "terminate_control", lambda slug, name: seen["terminate"].append((slug, name)) or ({}, "")
    )
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "rig")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1-0001")
    return seen


def test_a_page_control_runs_the_swarm_command_as_the_unpinned_operator(calls):
    assert ledger_server.swarm_control("demo", ["pause"]) == ({"swarm": "demo"}, "")
    [(argv, env)] = calls["run"]
    assert argv == [EXE, "swarm", "demo", "pause"]
    assert (env["AGENTIHOOKS_AGENT_NAME"], env["AGENTIHOOKS_SWARM"], env["AGENTIHOOKS_CONTROL_SOURCE"]) == (
        "operator",
        "",
        "page",
    )
    assert env["PATH"] == os.environ["PATH"]
    assert calls["terminate"] == []


def test_only_a_swarm_terminate_goes_to_the_terminate_control(calls):
    assert ledger_server.swarm_control("demo", ["terminate", "engineer@a1-0002"]) == ({}, "")
    assert ledger_server.swarm_control("demo", ["terminate", "x"], command="doctor") == ({"swarm": "demo"}, "")
    assert calls["terminate"] == [("demo", "engineer@a1-0002")]
    assert [argv for argv, _ in calls["run"]] == [[EXE, "doctor", "demo", "terminate", "x"]]


def test_a_missing_agentihooks_names_the_path(calls, monkeypatch):
    monkeypatch.setattr(ledger_server.shutil, "which", lambda name: None)
    assert ledger_server.swarm_control("demo", ["pause"]) == (None, "agentihooks is not on PATH")
    assert calls["run"] == []
