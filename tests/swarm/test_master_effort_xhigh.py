from dataclasses import replace
from pathlib import Path

import pytest

from scripts.profiles import render
from scripts.swarm import effort_range, master_launch
from scripts.swarm.store import SwarmConfig
from tests.swarm.test_runtime import _launched, _passed, _resuming


@pytest.mark.feature("master-effort-xhigh")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("saved", ["", "low", "medium", "high"])
@pytest.mark.parametrize("configured", [("low", "medium"), ("medium", "xhigh")])
def test_master_defaults_and_saved_lower_efforts_start_at_xhigh(
    harness: str, saved: str, configured: tuple[str, str]
) -> None:
    config = SwarmConfig("sw", "/repo", 0, 0, effort_min=configured[0], effort_max=configured[1])
    assert master_launch.fill({"harness": harness, "effort": saved}, config)["effort"] == "xhigh"


@pytest.mark.feature("master-effort-xhigh")
@pytest.mark.parametrize(
    "harness,expected",
    [
        ("claude", ["--model", "opus", "--effort", "xhigh"]),
        ("codex", ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"']),
    ],
)
@pytest.mark.parametrize("handoff", [False, True])
def test_new_and_handoff_master_launches_pass_xhigh_to_the_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str, expected: list[str], handoff: bool
) -> None:
    task = {"id": "master", "description": "Coordinate the swarm"}
    if handoff:
        task["handoff"] = "Continue the master seat"
    assert _launched(tmp_path, monkeypatch, "master", task, harness=harness)[-len(expected) :] == expected


@pytest.mark.feature("master-effort-xhigh")
@pytest.mark.parametrize(
    "harness,model,expected",
    [
        ("claude", "opus", ["--model", "opus", "--effort", "xhigh"]),
        ("codex", "gpt-6.1-sol", ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"']),
    ],
)
@pytest.mark.parametrize("saved", ["low", "high"])
@pytest.mark.parametrize("configured", [("low", "medium"), ("medium", "xhigh")])
def test_restored_masters_raise_saved_effort_to_xhigh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    model: str,
    expected: list[str],
    saved: str,
    configured: tuple[str, str],
) -> None:
    monkeypatch.setattr(render, "has_codex_rollout", lambda *_: True)
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness=harness)
    config.effort_min, config.effort_max = configured
    master = replace(agent, lane="master", task="master", profile="master", model=model, effort=saved)
    runtime.resume(config, master, "Continue the restored master")
    assert _passed(seen["runs"][0])[-len(expected) :] == expected


@pytest.mark.feature("master-effort-xhigh")
@pytest.mark.parametrize("lane", ["eng", "ci", "plan"])
def test_worker_lanes_keep_the_swarm_effort_range(lane: str) -> None:
    config = SwarmConfig("sw", "/repo", 0, 0, effort_min="low", effort_max="medium")
    assert effort_range.bounds(config, lane) == ("low", "medium")
