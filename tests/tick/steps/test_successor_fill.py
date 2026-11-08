"""A lane successor spawned from a saved launch with no model or effort takes the lane model and a range effort."""

import json

import pytest

from scripts.swarm import effort_range, profile_choice, store, successor_fill, tick
from tests.swarm.test_tick import FakeRuntime, tasks

pytestmark = pytest.mark.unit

SAVED = {"profile": "engineer", "harness": "claude", "model": "", "effort": "", "account": "a1"}


def swarm(lanes: dict, low: str = "high", high: str = "xhigh") -> store.RedisStore:
    """A fakeredis swarm whose lanes carry the given settings inside the given effort range."""
    import fakeredis

    saved = store.RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(store.SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, lanes=lanes, effort_min=low, effort_max=high))
    return saved


@pytest.mark.feature("swarm-tick-successor-fill")
def test_a_successor_from_a_saved_launch_with_neither_field_launches_on_the_lane_model_inside_the_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = swarm({"eng": {"model": "sonnet-lane"}})
    held.redis.set(held.key("sw", "handoff-envelope", "t1"), json.dumps({"reason": "context", "launch": SAVED}))
    decision = profile_choice.ProfileDecision("engineer", "classifier", "engineer", "m1", 0.9, True)
    monkeypatch.setattr(profile_choice, "classify", lambda slug, task, environ: decision)
    runtime = FakeRuntime()
    tick.tick("sw", held, tasks(("t1", "eng")), runtime, now_ms=1_000)
    launch = runtime.tasks[0]["handoff_envelope"]["launch"]
    assert launch["model"] == "sonnet-lane"
    assert launch["effort"] in effort_range.EFFORTS["claude"][2:4]
    assert launch["effort"] == "high"
    assert {key: launch[key] for key in ("profile", "harness", "account")} == {
        "profile": "engineer",
        "harness": "claude",
        "account": "a1",
    }
    assert "launch_assignment" not in runtime.tasks[0]


def test_a_filled_saved_effort_passes_the_preserved_effort_check_an_empty_one_fails() -> None:
    from scripts.swarm import runtime

    config = swarm({"eng": {"model": "sonnet-lane"}}).config("sw")
    bounds = effort_range.bounds(config, "eng")
    environ = {"AGENTIHOOKS_CLAUDE_EFFORT": "max"}
    with pytest.raises(tick.SpawnError, match="saved effort is outside the current swarm range"):
        runtime._model_args("claude", {"model": "sonnet-lane", "effort": ""}, environ, bounds, preserve=True)
    launch = successor_fill.filled(SAVED, config, "eng")
    flags = runtime._model_args("claude", launch, environ, bounds, preserve=True)
    assert flags == ["--model", "sonnet-lane", "--effort", "high"]


def test_an_automatic_lane_leaves_the_model_to_the_band_pick_and_takes_the_range_floor() -> None:
    config = swarm({"eng": {"model": "auto"}}).config("sw")
    assert successor_fill.filled(SAVED, config, "eng") == {**SAVED, "effort": "high"}
    assert successor_fill.filled(SAVED, swarm({}).config("sw"), "eng") == {**SAVED, "effort": "high"}


def test_a_lane_effort_is_kept_and_a_codex_launch_names_its_own_level() -> None:
    config = swarm({"eng": {"effort": "max"}}, "medium", "max").config("sw")
    assert successor_fill.filled({**SAVED, "harness": "codex"}, config, "eng") == {
        **SAVED,
        "harness": "codex",
        "effort": "xhigh",
    }
    held = swarm({"eng": {"effort": "medium"}}, "medium", "high").config("sw")
    assert successor_fill.filled(SAVED, held, "eng")["effort"] == "medium"


def test_a_master_bound_lifts_a_lane_effort_below_high() -> None:
    lifted = swarm({"master": {"effort": "medium"}}, "medium", "high").config("sw")
    assert successor_fill.filled(SAVED, lifted, "master")["effort"] == "high"


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


def test_only_the_handoff_launch_is_filled_and_the_launch_assignment_is_left_as_saved() -> None:
    config = swarm({"eng": {"model": "sonnet-lane"}}).config("sw")
    primed = {"id": "t1", "launch_assignment": SAVED, "handoff_envelope": {"reason": "context", "launch": SAVED}}
    want = {**SAVED, "model": "sonnet-lane", "effort": "high"}
    assert successor_fill.task(primed, config, "eng") == {
        "id": "t1",
        "launch_assignment": SAVED,
        "handoff_envelope": {"reason": "context", "launch": want},
    }
    bare = {"id": "t2", "handoff_envelope": {"reason": "context"}}
    assert successor_fill.task(bare, config, "eng") is bare
    assert successor_fill.task({"id": "t3"}, config, "eng") == {"id": "t3"}
