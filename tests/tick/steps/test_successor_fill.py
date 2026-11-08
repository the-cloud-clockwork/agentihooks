"""A lane successor spawned from a saved launch with no model or effort takes the lane model and a range effort."""

import json

import pytest

from scripts.swarm import effort_range, profile_choice, store, successor_fill, tick
from tests.swarm.test_tick import FakeRuntime, tasks

pytestmark = pytest.mark.unit

SAVED = {"profile": "engineer", "harness": "claude", "model": "", "effort": "", "account": "a1", "seat": ""}


def swarm(lanes: dict, low: str = "high", high: str = "xhigh") -> store.RedisStore:
    """A fakeredis swarm whose eng lane carries the given settings inside the given effort range."""
    import fakeredis

    saved = store.RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(store.SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, lanes=lanes, effort_min=low, effort_max=high))
    return saved


@pytest.mark.feature("swarm-tick-successor-fill")
def test_a_successor_from_a_saved_launch_with_neither_field_launches_on_the_lane_model_inside_the_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = swarm({"eng": {"model": "sonnet-lane"}})
    held.redis.hset(held.key("sw", "launch-assignments"), "t1", json.dumps(SAVED))
    decision = profile_choice.ProfileDecision("engineer", "classifier", "engineer", "m1", 0.9, True)
    monkeypatch.setattr(profile_choice, "classify", lambda slug, task, environ: decision)
    runtime = FakeRuntime()
    tick.tick("sw", held, tasks(("t1", "eng")), runtime, now_ms=1_000)
    launch = runtime.tasks[0]["launch_assignment"]
    assert launch["model"] == "sonnet-lane"
    assert effort_range.rank("high") <= effort_range.rank(launch["effort"]) <= effort_range.rank("xhigh")
    assert {key: launch[key] for key in ("profile", "harness", "account")} == {
        "profile": "engineer",
        "harness": "claude",
        "account": "a1",
    }


def test_an_automatic_lane_falls_back_to_the_swarm_model_and_the_range_floor() -> None:
    config = swarm({"eng": {"model": "auto"}}).config("sw")
    assert successor_fill.filled(SAVED, config, "eng") == {
        **SAVED,
        "model": config.claude_model,
        "effort": "high",
    }


def test_a_lane_effort_is_clamped_into_the_range_and_a_codex_launch_names_its_own_level() -> None:
    config = swarm({"eng": {"effort": "max"}}, "medium", "max").config("sw")
    assert successor_fill.filled({**SAVED, "harness": "codex"}, config, "eng") == {
        **SAVED,
        "harness": "codex",
        "model": config.codex_model,
        "effort": "xhigh",
    }
    low = swarm({"eng": {"effort": "low"}}, "medium", "high").config("sw")
    assert successor_fill.filled(SAVED, low, "eng")["effort"] == "medium"


def test_present_keys_stay_and_an_unknown_harness_is_left_alone() -> None:
    config = swarm({"eng": {"model": "sonnet-lane", "effort": "xhigh"}}).config("sw")
    assert successor_fill.filled({**SAVED, "model": "opus"}, config, "eng") == {
        **SAVED,
        "model": "opus",
        "effort": "xhigh",
    }
    assert successor_fill.filled({**SAVED, "effort": "high"}, config, "eng") == {
        **SAVED,
        "model": "sonnet-lane",
        "effort": "high",
    }
    whole = {**SAVED, "model": "opus", "effort": "max"}
    assert successor_fill.filled(whole, config, "eng") is whole
    assert successor_fill.filled({**SAVED, "harness": ""}, config, "eng") == {**SAVED, "harness": ""}


def test_a_master_lane_fill_never_starts_below_high() -> None:
    config = swarm({}, "low", "xhigh").config("sw")
    assert successor_fill.filled(SAVED, config, "master")["effort"] == "high"


def test_the_primed_task_fills_both_the_assignment_and_the_handoff_launch() -> None:
    config = swarm({"eng": {"model": "sonnet-lane"}}).config("sw")
    primed = {"id": "t1", "launch_assignment": SAVED, "handoff_envelope": {"reason": "context", "launch": SAVED}}
    filled = successor_fill.task(primed, config, "eng")
    want = {**SAVED, "model": "sonnet-lane", "effort": "high"}
    assert filled == {"id": "t1", "launch_assignment": want, "handoff_envelope": {"reason": "context", "launch": want}}
    bare = {"id": "t2", "handoff_envelope": {"reason": "context"}}
    assert successor_fill.task(bare, config, "eng") == bare
    assert successor_fill.task({"id": "t3"}, config, "eng") == {"id": "t3"}
