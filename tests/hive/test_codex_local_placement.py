from unittest.mock import Mock

import pytest

from scripts.swarm import controller
from scripts.swarm.tick import SpawnError
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_local_overflow import hybrid, local_agents, workstation


@pytest.mark.parametrize("lane", ["eng", "ci"])
def test_codex_local_fallback_refuses_at_the_eight_agent_cap(setup, monkeypatch, lane):
    fenced = hybrid(setup, monkeypatch)
    workstation(monkeypatch, 40)
    local_agents(setup, 8, slug="other-swarm", harness="codex")
    task = {"id": "task", "seat": f"{lane}-1@fixture", "launch_assignment": {"harness": "codex"}}
    assert "local machine already has 8 of its 8 agents" in fenced.placement_refusal(lane, task)
    with pytest.raises(SpawnError, match="local machine already has 8 of its 8 agents"):
        fenced.spawn(setup.config, lane, "eng name", task)
    setup.herdr.spawn.assert_not_called()
    fenced.runtime.hive_launch.spawn.assert_not_called()


def test_codex_local_fallback_uses_the_same_admission_path_as_claude(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    refused = "the workstation admission refused the launch"
    overflow = Mock(return_value=refused)
    monkeypatch.setattr(controller.FencedRuntime, "_overflow", overflow)
    task = {"id": "task", "launch_assignment": {"harness": "codex"}}
    assert fenced.placement_refusal("eng", task) == refused
    assert overflow.call_args.args[:2] == ("eng", task)
    assert overflow.call_args.args[2].reason == "Codex hive placement is not enabled"
