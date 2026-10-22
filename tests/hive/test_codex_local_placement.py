from unittest.mock import Mock

from scripts.swarm import controller
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_local_overflow import HIVE_FULL, fill_anton, hybrid


def test_codex_local_fallback_uses_the_same_admission_path_as_claude(setup, monkeypatch):
    fenced = hybrid(setup, monkeypatch)
    fill_anton(setup)
    refused = "the workstation admission refused the launch"
    overflow = Mock(return_value=refused)
    monkeypatch.setattr(controller.FencedRuntime, "_overflow", overflow)
    task = {"id": "task", "launch_assignment": {"harness": "codex"}}
    assert fenced.placement_refusal("eng", task) == refused
    assert overflow.call_args.args[:2] == ("eng", task)
    assert overflow.call_args.args[2].reason == HIVE_FULL
