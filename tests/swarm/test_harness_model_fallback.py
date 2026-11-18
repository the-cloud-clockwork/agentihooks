"""A launch handed the other harness's model falls back to the swarm's model for its own harness, never the launcher default."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.templates import DEFAULT_PROFILES
from tests.swarm.profile_fixture import validated
from tests.swarm.test_runtime import _passed, _resuming

SWARM_MODELS = {"claude_model": "claude-opus-5-5", "codex_model": "gpt-6-sol"}
AUTO = {"agent": "auto", "model": "auto", "effort": "auto"}


@pytest.fixture(autouse=True)
def launcher_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the launcher on its built in models, opus and gpt-6.1-sol, which differ from the swarm's."""
    for agent in ("CLAUDE", "CODEX"):
        for key in ("MODEL", "EFFORT"):
            monkeypatch.delenv(f"AGENTIHOOKS_{agent}_{key}", raising=False)


def _spawned(tmp_path: Path, harness: str, task: dict[str, Any], lanes: dict[str, dict[str, str]]) -> list[str]:
    seen: dict[str, list[str]] = {}

    def run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: (harness, "open"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes=lanes or {name: AUTO for name in ("eng", "ci", "plan", "master")},
        autonomy="delegate",
        **SWARM_MODELS,
    )
    runtime.spawn(config, "eng", "agent@a1b2c3-0001", task)
    return _passed(seen["argv"])


@pytest.mark.feature("launch-model-suits-harness")
@pytest.mark.parametrize(
    ("harness", "saved_model", "launch"),
    [
        ("codex", "opus", ["-m", "gpt-6-sol", "-c", 'model_reasoning_effort="medium"']),
        ("claude", "gpt-6.1-sol", ["--model", "claude-opus-5-5", "--effort", "medium"]),
    ],
)
def test_a_successor_saved_model_of_the_other_harness_falls_back_to_the_swarm_harness_model(
    tmp_path: Path, harness: str, saved_model: str, launch: list[str]
) -> None:
    saved = {"profile": DEFAULT_PROFILES["eng"], "harness": harness, "model": saved_model, "effort": "medium"}
    task = {"id": "t1", "title": "Fix typo", "handoff_envelope": {"launch": saved}}
    assert _spawned(tmp_path, harness, task, {}) == launch


@pytest.mark.feature("launch-model-suits-harness")
def test_a_claude_first_life_on_a_codex_pinned_engineer_lane_takes_the_swarm_claude_model(tmp_path: Path) -> None:
    task = {"id": "t1", "title": "Fix typo", "difficulty": "S", "rank": "normal"}
    lanes = {"eng": {"model": "gpt-6.1-sol", "effort": "medium"}}
    assert _spawned(tmp_path, "claude", task, lanes) == ["--model", "claude-opus-5-5", "--effort", "medium"]


@pytest.mark.feature("launch-model-suits-harness")
@pytest.mark.parametrize(
    ("harness", "recorded", "launch"),
    [
        ("codex", "opus", ["-m", "gpt-6-sol", "-c", 'model_reasoning_effort="high"']),
        ("claude", "gpt-6.1-sol", ["--model", "claude-opus-5-5", "--effort", "high"]),
    ],
)
def test_a_resumed_model_of_the_other_harness_falls_back_to_the_swarm_harness_model(
    tmp_path: Path, harness: str, recorded: str, launch: list[str]
) -> None:
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness=harness)
    vars(config).update(SWARM_MODELS)
    runtime.resume(config, replace(agent, model=recorded), "you were restored")
    assert _passed(seen["runs"][0]) == ["--route", "a1", *launch]
