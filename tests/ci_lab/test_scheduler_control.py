from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.ci_lab import scheduler
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.units import Priority
from tests.ci_lab.test_scheduler import HEAD_A, HEAD_B, Expander, Inbox, Lab, Run, pull_plan, unit


@pytest.fixture
def lab(tmp_path: Path) -> Callable[..., Run]:
    import fakeredis

    def make(plans: dict[str, scheduler.Plan], quiet_ms: int = 0) -> Run:
        client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
        state = Lab(client, LocalObjects(tmp_path / "objects"), Inbox(), Expander(plans))
        return Run(state, state.build(quiet_ms, Policy()))

    return make


def _pull(run: Run, head: str, lane: str) -> None:
    run.push(head, lane=lane, event="pull_request", action="synchronize")


def _seen(run: Run) -> list[str]:
    return [request.head for request in run.lab.expander.seen]


@pytest.mark.feature("lab-control-routes")
@pytest.mark.parametrize("named", [HEAD_A, "feature"])
def test_a_prioritized_head_or_lane_grades_in_the_hotfix_class_without_waiting(
    lab: Callable[..., Run], named: str
) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})}, quiet_ms=60_000)
    scheduler.RedisControl(run.lab.client).prioritize(frozenset({named}))
    _pull(run, HEAD_A, "feature")
    run.tick(1000)
    assert run.grade(HEAD_A).tier == Priority.HOTFIX


def test_a_head_nobody_prioritized_keeps_its_class(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})}, quiet_ms=0)
    scheduler.RedisControl(run.lab.client).prioritize(frozenset({"other"}))
    _pull(run, HEAD_A, "feature")
    run.tick(1000)
    assert run.grade(HEAD_A).tier == Priority.PULL_REQUEST


def test_a_priority_expires_after_its_window(lab: Callable[..., Run]) -> None:
    run = lab({})
    scheduler.RedisControl(run.lab.client).prioritize(frozenset({"fix-branch"}))
    assert run.lab.client.get(scheduler.PRIORITY + "fix-branch") == "hotfix"
    assert run.lab.client.ttl(scheduler.PRIORITY + "fix-branch") == scheduler.PRIORITY_TTL_S


@pytest.mark.feature("lab-control-routes")
def test_a_paused_class_holds_its_heads_until_resume_except_the_spared_lanes(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)}), HEAD_B: pull_plan({"lint": (unit(2),)})}, quiet_ms=0)
    control = scheduler.RedisControl(run.lab.client)
    control.pause("pull_request", frozenset({"fix-branch"}))
    _pull(run, HEAD_A, "feature")
    _pull(run, HEAD_B, "fix-branch")
    run.tick(1000)
    run.tick(2000)
    assert _seen(run) == [HEAD_B]
    assert control.resume() == ["pull_request"]
    run.tick(3000)
    assert _seen(run) == [HEAD_B, HEAD_A]
    assert control.resume() == []


def test_a_paused_class_spares_a_head_named_by_its_commit(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})}, quiet_ms=0)
    scheduler.RedisControl(run.lab.client).pause("pull_request", frozenset({HEAD_A}))
    _pull(run, HEAD_A, "feature")
    run.tick(1000)
    assert _seen(run) == [HEAD_A]


def test_a_pause_holds_only_its_own_class(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})}, quiet_ms=0)
    scheduler.RedisControl(run.lab.client).pause("queue", frozenset())
    _pull(run, HEAD_A, "feature")
    run.tick(1000)
    assert _seen(run) == [HEAD_A]


def test_the_pause_records_its_spared_heads(lab: Callable[..., Run]) -> None:
    run = lab({})
    control = scheduler.RedisControl(run.lab.client)
    assert control.spared("pull_request") is None
    control.pause("pull_request", frozenset({"b", "a"}))
    assert control.spared("pull_request") == frozenset({"a", "b"})
    assert run.lab.client.get(scheduler.PAUSED + "pull_request") == '["a", "b"]'
