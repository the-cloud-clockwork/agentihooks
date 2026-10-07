import os
import subprocess

import pytest

from scripts.swarm import command_runner, commands
from scripts.swarm_ledger import ledger_server

EXE = "/opt/bin/agentihooks"


pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def calls(monkeypatch):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/hive", 0, 0))
    seen = {"run": [], "terminate": [], "store": store}

    def run(argv, **kwargs):
        env = {
            key: kwargs["env"][key]
            for key in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM", "AGENTIHOOKS_CONTROL_SOURCE", "PATH")
        }
        seen["run"].append((argv, env))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(command_runner.shutil, "which", lambda name: EXE if name == "agentihooks" else None)
    monkeypatch.setattr(command_runner.subprocess, "run", run)
    monkeypatch.setattr(ledger_server, "swarm_store", lambda: store)
    monkeypatch.setattr(ledger_server, "swarm_status", lambda slug: {"swarm": slug})
    monkeypatch.setattr(
        ledger_server, "terminate_control", lambda slug, name: seen["terminate"].append((slug, name)) or ({}, "")
    )
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "rig")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1-0001")
    return seen


def test_a_page_control_runs_the_swarm_command_as_the_unpinned_operator(calls):
    assert ledger_server.swarm_control("demo", ["pause"]) == ({"swarm": "demo"}, "")
    assert calls["run"] == []
    command_runner.consume(calls["store"], "demo")
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
    command_runner.consume(calls["store"], "demo")
    assert [argv for argv, _ in calls["run"]] == [[EXE, "doctor", "demo", "terminate", "x"]]


def test_a_missing_agentihooks_names_the_path(calls, monkeypatch):
    monkeypatch.setattr(command_runner.shutil, "which", lambda name: None)
    assert ledger_server.swarm_control("demo", ["pause"]) == ({"swarm": "demo"}, "")
    assert command_runner.consume(calls["store"], "demo") == ["control swarm failed"]
    assert commands.rows(calls["store"], "demo")[0]["error"] == "agentihooks is not on PATH"
    assert calls["run"] == []
