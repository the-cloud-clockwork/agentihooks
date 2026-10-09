from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.swarm import runtime
from scripts.swarm.tick import SpawnError
from tests.swarm.profile_fixture import validated
from tests.swarm.test_runtime import _resuming


@pytest.fixture
def launching(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        out = "status=started\nroute_status=routed\nmodel=saved-model\neffort=medium\naccount=original\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    engine = runtime.HerdrRuntime(
        home=tmp_path, run=run, choose=lambda requested, env: (requested or "claude", "pinned")
    )
    config = SimpleNamespace(
        slug="proof", repo=str(tmp_path), code="a1b2c3", lanes={}, autonomy="delegate", compact_limit=0
    )
    saved = {"profile": "qa", "harness": "codex", "model": "saved-model", "effort": "medium", "account": "original"}
    task = {"id": "task", "title": "Independent proof", "handoff": "continue", "handoff_envelope": {"launch": saved}}
    return engine, config, task, saved, calls


@pytest.mark.parametrize("key", ["profile", "harness", "model", "effort", "launch"])
def test_handoff_missing_original_choices_cannot_spawn(launching, key):
    engine, config, task, saved, calls = launching
    if key == "launch":
        task["handoff_envelope"].pop("launch")
    else:
        saved.pop(key)
    with pytest.raises(
        SpawnError, match="^unsupported handoff: original profile and run options are missing$"
    ) as error:
        engine.spawn(config, "eng", "worker", task)
    assert error.value.status == "unsupported"
    assert not calls


def test_handoff_rejects_unsupported_harness_and_router_substitution(launching):
    engine, config, task, saved, calls = launching
    saved["harness"] = "copilot"
    with pytest.raises(SpawnError, match="^unsupported handoff harness: copilot$") as error:
        engine.spawn(config, "eng", "worker", task)
    assert error.value.status == "unsupported"
    saved["harness"] = "codex"
    engine.choose = lambda *a: ("claude", "fallback")
    with pytest.raises(SpawnError, match="^unsupported handoff: router substituted the original harness$") as error:
        engine.spawn(config, "eng", "worker", task)
    assert error.value.status == "unsupported"
    assert not calls


def test_handoff_cannot_move_a_claude_only_profile_to_codex(launching, monkeypatch):
    engine, config, task, saved, calls = launching
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda *a: True)
    with pytest.raises(
        SpawnError, match="^unsupported handoff: required profile cannot mount on the original harness$"
    ) as error:
        engine.spawn(config, "eng", "worker", task)
    assert error.value.status == "unsupported"
    assert not calls
    saved["harness"] = "claude"
    result = engine.spawn(config, "eng", "worker", task)
    assert result.harness == "claude"


@pytest.mark.parametrize("source,confidence", [(None, None), ("original-classifier", 0.88)])
def test_handoff_preserves_unpinned_profile_and_decision_metadata(launching, source, confidence):
    engine, config, task, saved, calls = launching
    if source is not None:
        saved.update(model_source=source, model_confidence=confidence)
    result = engine.spawn(config, "eng", "worker", task)
    assert result.profile == "qa"
    assert result.model_source == (source or "handoff")
    assert result.model_confidence == confidence
    assert calls[0][calls[0].index("--profile") + 1] == "qa"
    assert result.profile_decision["validation"]["profile"] == "qa"


def test_handoff_of_a_default_lane_profile_is_never_reclassified(launching, monkeypatch):
    engine, config, task, saved, calls = launching
    saved.update(profile="engineer", harness="claude")
    monkeypatch.setattr(runtime.profile_choice, "classify", lambda *a: pytest.fail("handoff reclassified its seat"))
    result = engine.spawn(config, "eng", "worker", task)
    assert calls[-1][calls[-1].index("--profile") + 1] == "engineer"
    decision = result.profile_decision
    assert (result.profile, decision["source"], decision["responsibility"]) == (
        "engineer",
        "handoff",
        "original seat profile",
    )


def test_an_explicit_task_profile_prevails_over_the_saved_seat_profile(launching):
    engine, config, task, saved, calls = launching
    saved.update(profile="engineer", harness="claude")
    task["profile"] = "qa"
    result = engine.spawn(config, "eng", "worker", task)
    assert calls[-1][calls[-1].index("--profile") + 1] == "qa"
    assert (result.profile, result.profile_decision["source"]) == ("qa", "task")


def test_handoff_refuses_to_clamp_saved_effort(launching):
    engine, config, task, saved, calls = launching
    config.effort_min = config.effort_max = "high"
    with pytest.raises(
        SpawnError, match="^unsupported transfer: saved effort is outside the current swarm range$"
    ) as error:
        engine.spawn(config, "eng", "worker", task)
    assert error.value.status == "unsupported"
    assert not calls


def test_a_master_recycle_starts_inside_the_swarm_effort_range(launching):
    engine, config, task, saved, calls = launching
    saved.update(profile="master", harness="claude", effort="max")
    task["handoff_envelope"]["reason"] = "recycle"
    config.effort_min, config.effort_max = "medium", "high"
    engine.spawn(config, "master", "master", task)
    assert calls[-1][calls[-1].index("--effort") + 1] == "high"


@pytest.mark.parametrize(
    ("saved", "lane", "quota", "expected"),
    [
        ({"effort": "max"}, "master", False, False),
        ({"effort": "max"}, "master", True, True),
        ({"effort": "max"}, "eng", False, True),
        ({}, "eng", False, False),
    ],
)
def test_only_a_master_recycle_gives_up_its_saved_effort(saved, lane, quota, expected):
    assert runtime.preserves_effort(saved, lane, quota) is expected


def test_resume_keeps_decision_and_replaces_the_old_binding_evidence(tmp_path):
    engine, config, agent, calls = _resuming(tmp_path, "c0ffee")
    decision = {"profile": "engineer", "source": "task", "validation": {"state": "old", "pid": 42}}
    result = engine.resume(config, replace(agent, profile_decision=decision), "continue")
    assert result.model_source == "recorded"
    assert result.profile_decision["source"] == "task"
    assert result.profile_decision["profile"] == "engineer"
    assert result.profile_decision["validation"]["state"] == "validated"
    assert result.profile_decision["validation"]["pid"] == 123


def test_missing_live_validation_retires_the_named_process(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="status=started\nroute_status=routed\n", stderr="")

    engine = runtime.HerdrRuntime(home=tmp_path, run=run)
    config = SimpleNamespace(slug="proof", repo=str(tmp_path), autonomy="delegate", compact_limit=0)
    with pytest.raises(SpawnError, match="validation is missing"):
        engine._launch(config, "eng", "task", "worker", ["init-agent", "--agent", "codex", "--profile", "engineer"])
    assert calls[-1][1:] == ["terminate-agent", "worker", "--force-shared"]


def test_a_fresh_launch_clamps_a_lane_effort_into_the_swarm_range():
    assert runtime._model_args("claude", {"model": "opus", "effort": "low"}, {}, ("medium", "high")) == [
        "--model",
        "opus",
        "--effort",
        "medium",
    ]
