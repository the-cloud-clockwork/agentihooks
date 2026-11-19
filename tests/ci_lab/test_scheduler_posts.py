from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
import requests

from scripts.ci_lab import scheduler
from scripts.ci_lab.checks import Checks, Output, PostRefused
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.webhooks import HeadEvent
from scripts.github_app import REFUSED, GitAuthRefused
from tests.ci_lab.test_scheduler import ACTOR, BASE, HEAD_A, HEAD_B, SLUG, Poster, Run, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab
HOLD_MS = 60_000
OTHER_REPO = "/the-cloud-clockwork/agentihooks-bundle"
REFUSALS = [
    pytest.param(GitAuthRefused(REFUSED), id="token-refused"),
    pytest.param(PostRefused("POST check-runs answered 422"), id="post-refused"),
    pytest.param(requests.ConnectionError("connection reset by peer"), id="network-failed"),
]


@dataclass
class Refusing(Poster):
    refusals: list[Exception | None] = field(default_factory=list[Exception | None])
    heads: frozenset[str] = frozenset()
    attempts: int = 0

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        self.attempts += 1
        chosen = not self.heads or head in self.heads
        refusal = self.refusals.pop(0) if chosen and self.refusals else None
        if refusal is not None:
            raise refusal
        return super().post(head, gate, status, conclusion, output, shadow)


def refusing(run: Run, *refusals: Exception | None, heads: frozenset[str] = frozenset()) -> Refusing:
    poster = Refusing(refusals=list(refusals), heads=heads)
    run.lab.poster = poster
    return poster


def cached(lab: Callable[..., Run]) -> Run:
    units = (unit(1), unit(2))
    run = lab({HEAD_A: plan({"lint": units})})
    for item in units:
        run.cache(item)
    return run


@pytest.mark.feature("lab-scheduler-post-refusal")
@pytest.mark.parametrize("refusal", REFUSALS)
def test_a_refused_check_run_post_is_recorded_and_the_pass_keeps_grading(
    lab: Callable[..., Run], refusal: Exception
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    poster = refusing(run, refusal)
    run.push(HEAD_A)
    actions = run.tick(1000)
    grade = run.grade(HEAD_A)
    assert f"held lint queued on {HEAD_A} for a later pass: {refusal}" in actions
    assert f"enqueued 1 units of {grade.run}" in actions
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, unit(1))]
    assert (poster.attempts, poster.posts, grade.posted) == (1, [], {})
    assert run.tick(1000 + HOLD_MS - 1) == []
    assert poster.attempts == 1
    assert run.tick(1000 + HOLD_MS) == [
        f"posted lint queued on {HEAD_A}",
        f"posted {scheduler.REQUIRED} queued on {HEAD_A}",
    ]
    assert poster.moves(HEAD_A, "lint") == [("queued", None)]
    assert poster.moves(HEAD_A, scheduler.REQUIRED) == [("queued", None)]


@pytest.mark.feature("lab-scheduler-post-refusal")
def test_a_grade_whose_completed_post_is_refused_stays_live_until_a_later_pass_posts_it(
    lab: Callable[..., Run],
) -> None:
    run = cached(lab)
    poster = refusing(run, GitAuthRefused(REFUSED))
    run.push(HEAD_A)
    actions = run.tick(1000)
    grade = run.grade(HEAD_A)
    assert f"held lint completed on {HEAD_A} for a later pass: {REFUSED}" in actions
    assert f"completed {grade.run} success" not in actions
    assert run.lab.client.smembers(scheduler.LIVE) == {grade.run}
    assert (grade.conclusion, grade.posted, poster.posts) == ("", {}, [])
    assert run.tick(1000 + HOLD_MS) == [
        f"posted lint completed on {HEAD_A}",
        f"posted {scheduler.REQUIRED} completed on {HEAD_A}",
        f"completed {grade.run} success",
    ]
    assert poster.moves(HEAD_A, scheduler.REQUIRED) == [("completed", "success")]
    assert run.lab.client.smembers(scheduler.LIVE) == set()
    assert run.grade(HEAD_A).conclusion == "success"


@pytest.mark.feature("lab-scheduler-post-refusal")
def test_a_refused_required_post_keeps_the_posted_gate_and_closes_the_grade_on_the_next_post(
    lab: Callable[..., Run],
) -> None:
    run = cached(lab)
    poster = refusing(run, None, GitAuthRefused(REFUSED))
    run.push(HEAD_A)
    actions = run.tick(1000)
    grade = run.grade(HEAD_A)
    assert f"posted lint completed on {HEAD_A}" in actions
    assert f"held {scheduler.REQUIRED} completed on {HEAD_A} for a later pass: {REFUSED}" in actions
    assert run.lab.client.smembers(scheduler.LIVE) == {grade.run}
    assert grade.posted == {"lint": ["completed", "success"]}
    assert grade.runs == {"lint": 1}
    assert run.tick(1000 + HOLD_MS) == [
        f"posted {scheduler.REQUIRED} completed on {HEAD_A}",
        f"completed {grade.run} success",
    ]
    assert poster.attempts == 3
    assert poster.moves(HEAD_A, "lint") == [("completed", "success")]
    assert poster.moves(HEAD_A, scheduler.REQUIRED) == [("completed", "success")]
    assert run.lab.client.smembers(scheduler.LIVE) == set()


@pytest.mark.feature("lab-scheduler-post-refusal")
def test_a_refused_post_holds_the_posts_of_every_grade_of_the_repository_until_the_hold_ends(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    poster = refusing(run, GitAuthRefused(REFUSED))
    run.push(HEAD_A, lane="first")
    run.push(HEAD_B, lane="second")
    actions = run.tick(1000)
    assert len([action for action in actions if action.startswith("held ")]) == 1
    assert len([action for action in actions if action.startswith("enqueued 1 units of ")]) == 2
    assert poster.attempts == 1
    assert run.tick(1000 + HOLD_MS - 1) == []
    assert poster.attempts == 1
    assert len(run.tick(1000 + HOLD_MS)) == 4
    expected = {(head, gate, "queued") for head in (HEAD_A, HEAD_B) for gate in ("lint", scheduler.REQUIRED)}
    assert {(post[0], post[1], post[2]) for post in poster.posts} == expected
    assert poster.attempts == 5


@pytest.mark.feature("lab-scheduler-post-refusal")
def test_a_refused_post_holds_only_its_own_repository_and_another_repository_keeps_posting(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    poster = refusing(run, GitAuthRefused(REFUSED), heads=frozenset({HEAD_A}))
    run.push(HEAD_A, lane="first")
    other = HeadEvent("push", OTHER_REPO, HEAD_B, BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(other, "second", ""))
    actions = run.tick(1000)
    assert f"held lint queued on {HEAD_A} for a later pass: {REFUSED}" in actions
    assert run.grade(HEAD_B).repo == OTHER_REPO
    assert poster.moves(HEAD_A, "lint") == []
    assert poster.moves(HEAD_B, "lint") == [("queued", None)]
    assert poster.moves(HEAD_B, scheduler.REQUIRED) == [("queued", None)]


@pytest.mark.feature("lab-scheduler-post-refusal")
def test_a_post_github_refuses_holds_only_its_grade_across_a_restart_while_other_grades_keep_posting(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    refusal = PostRefused("POST check-runs answered 422")
    poster = refusing(run, refusal, heads=frozenset({HEAD_A}))
    run.push(HEAD_A, lane="first")
    run.push(HEAD_B, lane="second")
    assert f"held lint queued on {HEAD_A} for a later pass: {refusal}" in run.tick(1000)
    assert run.grade(HEAD_A).hold == 1000 + HOLD_MS
    grant = next(leased for leased in run.lease(slots=2, ttl_ms=10 * HOLD_MS) if leased.job.startswith(HEAD_B))
    run.finish(grant, "passed", {})
    restarted = Run(run.lab, run.lab.build(0, Policy(), None))
    actions = restarted.tick(1000 + HOLD_MS - 1)
    assert f"completed {restarted.grade(HEAD_B).run} success" in actions
    assert poster.moves(HEAD_A, "lint") == []
    assert restarted.tick(1000 + HOLD_MS) == [
        f"posted lint in_progress on {HEAD_A}",
        f"posted {scheduler.REQUIRED} in_progress on {HEAD_A}",
    ]


class RefusedToken:
    def get_token(self) -> str:
        raise GitAuthRefused(REFUSED)


@pytest.mark.feature("lab-scheduler-post-refusal")
def test_the_check_poster_s_refused_app_token_is_held_by_the_pass_and_tried_again_after_the_hold(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    poster = Checks(SLUG, RefusedToken())
    ports = scheduler.Ports(run.lab.inbox, run.lab.expander, lambda _: poster, run.lab.hotfixes, run.lab.kube)
    engine = scheduler.Scheduler(run.lab.client, run.lab.objects, ports, run.engine.settings)
    run.push(HEAD_A)
    held = f"held lint queued on {HEAD_A} for a later pass: {REFUSED}"
    assert held in engine.tick(1000)
    assert engine.tick(1000 + HOLD_MS - 1) == []
    assert held in engine.tick(1000 + HOLD_MS)
    assert (poster.runs, run.grade(HEAD_A).posted) == ({}, {})


@pytest.mark.feature("lab-scheduler-post-refusal")
@pytest.mark.parametrize("refusal", REFUSALS)
def test_a_refused_failed_check_of_a_head_that_cannot_expand_is_recorded_and_the_pass_goes_on(
    lab: Callable[..., Run], refusal: Exception
) -> None:
    run = lab({HEAD_B: plan({"lint": (unit(2),)})})
    poster = refusing(run, refusal, heads=frozenset({HEAD_A}))
    run.push(HEAD_A, lane="first")
    run.push(HEAD_B, lane="second")
    actions = run.tick(1000)
    assert f"cannot post the failed {scheduler.REQUIRED} check on {HEAD_A}: {refusal}" in actions
    assert poster.moves(HEAD_A, scheduler.REQUIRED) == []
    assert poster.moves(HEAD_B, "lint") == [("queued", None)]
