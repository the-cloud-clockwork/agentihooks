import json
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import budget, queue, scheduler
from scripts.ci_lab.checks import Output, PostRefused
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.webhooks import HeadEvent
from scripts.github_app import REFUSED, GitAuthRefused
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    DAY_MS,
    HEAD_A,
    HEAD_B,
    LOW_BUDGET,
    REPO,
    Expander,
    Inbox,
    Lab,
    Poster,
    Run,
    _spend,
    fake_redis,
    plan,
    unit,
)
from tests.ci_lab.test_scheduler import lab as _lab
from tests.ci_lab.test_scheduler_posts import HOLD_MS, OTHER_REPO

lab = _lab
HEAD_C, HEAD_D, HEAD_E = "1" * 40, "2" * 40, "3" * 40
RETRIES = Policy(failed=1)
Sent = list[tuple[str, str, bool]]


@dataclass
class Holding(Poster):
    refusals: dict[tuple[str, str], Exception] = field(default_factory=dict[tuple[str, str], Exception])

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        refusal = self.refusals.pop((head, status), None)
        if refusal is not None:
            raise refusal
        return super().post(head, gate, status, conclusion, output, shadow)


def _record(run: Run, monkeypatch: pytest.MonkeyPatch) -> Sent:
    sent: Sent = []
    building: list[bool] = []
    client = run.lab.client
    command, pipeline, demand = client.execute_command, client.pipeline, run.engine._demand

    def note(args: tuple[Any, ...]) -> None:
        if len(args) > 1 and str(args[1]).startswith(scheduler.GRADES):
            sent.append((str(args[0]).upper(), str(args[1]).removeprefix(scheduler.GRADES), bool(building)))

    def direct(*args: Any, **options: Any) -> Any:
        note(args)
        return command(*args, **options)

    def piped(*args: Any, **options: Any) -> Any:
        pipe = pipeline(*args, **options)
        queued = pipe.execute_command

        def recorded(*inner: Any, **more: Any) -> Any:
            note(inner)
            return queued(*inner, **more)

        pipe.execute_command = recorded
        return pipe

    def demanded() -> Any:
        building.append(True)
        try:
            return demand()
        finally:
            building.pop()

    monkeypatch.setattr(client, "execute_command", direct)
    monkeypatch.setattr(client, "pipeline", piped)
    monkeypatch.setattr(run.engine, "_demand", demanded)
    return sent


def _moved(sent: Sent) -> list[tuple[str, str]]:
    return [(name, grade) for name, grade, building in sent if name in {"GET", "SET"} and not building]


def _documents(run: Run) -> dict[str, Any]:
    client = run.lab.client
    return {name: json.loads(client.get(name)) for name in sorted(client.keys(scheduler.GRADES + "*"))}


def _started(run: Run, *heads: str) -> int:
    return sum(slot.started for head in heads for slot in run.grade(head).units.values())


@pytest.mark.feature("lab-scheduler-pass-moves-changed-grades")
def test_a_quiet_pass_reads_and_writes_no_live_grade_whatever_holds_it(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = (unit(9), unit(10))
    plans = {
        HEAD_A: plan({"lint": (unit(1), unit(2))}),
        HEAD_B: plan({"lint": (unit(3),)}),
        HEAD_C: plan({"lint": (unit(4),)}),
        HEAD_D: plan({"lint": (unit(5),)}),
        HEAD_E: plan({"lint": cached}),
    }
    run = lab(plans, spend=LOW_BUDGET)
    _spend(run)
    for item in cached:
        run.cache(item)
    refusal = PostRefused("POST check-runs answered 422")
    refusals = {
        (HEAD_C, "queued"): refusal,
        (HEAD_D, "queued"): GitAuthRefused(REFUSED),
        (HEAD_E, "completed"): refusal,
    }
    run.lab.poster = Holding(refusals=refusals)
    run.push(HEAD_A, lane="pr-1", event="pull_request", action="synchronize")
    run.push(HEAD_B, lane="sweep", event="backfill")
    run.push(HEAD_C, lane="third")
    other = HeadEvent("push", OTHER_REPO, HEAD_D, BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(other, "fourth", ""))
    run.push(HEAD_E, lane="fifth")
    run.tick(1000)
    assert len(run.lease(ttl_ms=10 * HOLD_MS)) == 1
    run.tick(1100)
    run.tick(1200)
    documents = _documents(run)
    sent = _record(run, monkeypatch)
    assert run.tick(1300) == []
    moved = _moved(sent)
    assert len(run.lab.client.smembers(scheduler.LIVE)) == 5
    assert _started(run, HEAD_A, HEAD_C, HEAD_D) == 1
    assert [slot.held for slot in run.grade(HEAD_B).units.values()] == [True]
    assert (run.grade(HEAD_C).hold, run.grade(HEAD_E).hold) == (1000 + HOLD_MS, 1000 + HOLD_MS)
    assert run.engine.held[OTHER_REPO] == 1000 + HOLD_MS
    assert [slot.outcome for slot in run.grade(HEAD_E).units.values()] == ["passed", "passed"]
    assert moved == []
    assert _documents(run) == documents


@pytest.mark.feature("lab-scheduler-pass-moves-changed-grades")
def test_a_pass_after_one_job_finishes_reads_and_writes_only_the_grade_holding_it(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1), unit(2))}), HEAD_B: plan({"lint": (unit(3), unit(4))})})
    run.push(HEAD_A, lane="first")
    run.push(HEAD_B, lane="second")
    run.tick(1000)
    run.tick(1100)
    grant = run.lease()[0]
    head = grant.job.split(":")[0]
    run.finish(grant, "passed", {"seconds": 1.0})
    sent = _record(run, monkeypatch)
    actions = run.tick(1200)
    moved = _moved(sent)
    graded = run.grade(head)
    assert actions == [f"posted lint in_progress on {head}", f"posted {scheduler.REQUIRED} in_progress on {head}"]
    assert [slot.outcome for slot in graded.units.values()] == ["passed", ""]
    assert sorted(set(moved)) == [("GET", graded.run), ("SET", graded.run)]


@pytest.mark.feature("lab-scheduler-pass-moves-changed-grades")
def test_a_unit_whose_queued_job_vanished_is_dropped_on_the_next_pass(lab: Callable[..., Run]) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first, second)})})
    run.push(HEAD_A)
    run.tick(1000)
    run.tick(1100)
    run.lab.client.delete(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, first)}")
    run.tick(1200)
    grade = run.grade(HEAD_A)
    assert [(slot.key, slot.outcome) for slot in grade.units.values()] == [(first.key, "dropped"), (second.key, "")]
    assert grade.run in run.lab.client.smembers(scheduler.LIVE)


@pytest.mark.feature("lab-scheduler-pass-moves-changed-grades")
def test_a_finished_job_its_retry_policy_reruns_goes_back_to_pending_and_leaves_its_grade_unwritten(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1), unit(2))})}, retries=RETRIES)
    run.push(HEAD_A)
    run.tick(1000)
    grant = run.lease()[0]
    run.finish(grant, "failed", {"seconds": 1.0})
    documents = _documents(run)
    sent = _record(run, monkeypatch)
    assert run.tick(1100) == []
    writes = [entry for entry in sent if entry[0] == "SET"]
    assert grant.job in run.engine.queue.pending()
    assert writes == []
    assert _documents(run) == documents


@pytest.mark.feature("lab-scheduler-pass-moves-changed-grades")
def test_a_live_grade_saved_with_every_slot_final_and_every_check_posted_closes_on_the_next_pass(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    run.push(HEAD_A)
    run.tick(1000)
    state = run.grade(HEAD_A)
    for slot in state.units.values():
        slot.outcome = "passed"
    state.posted = {check: [status, conclusion] for check, (status, conclusion) in scheduler._checks(state).items()}
    run.engine.save(state)
    assert run.tick(1100) == [f"completed {state.run} success"]
    assert run.lab.client.smembers(scheduler.LIVE) == set()


@pytest.mark.feature("lab-scheduler-pass-moves-changed-grades")
def test_a_grade_ref_head_records_its_moves_for_the_relay_while_its_repository_posts_are_held(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    run.lab.poster = Holding(refusals={(HEAD_B, "queued"): GitAuthRefused(REFUSED)})
    run.push(HEAD_B, lane="second")
    run.tick(1000)
    assert run.engine.held[REPO] == 1000 + HOLD_MS
    run.push(HEAD_A, lane="first", ref="refs/grade/t1")
    actions = run.tick(1100)
    assert f"left lint queued on {HEAD_A} to the relay" in actions
    assert run.grade(HEAD_A).posted == {"lint": ["queued", None], scheduler.REQUIRED: ["queued", None]}
    assert run.lab.poster.moves(HEAD_B, "lint") == []


@pytest.mark.feature("lab-scheduler-pass-moves-changed-grades")
def test_a_grade_another_scheduler_saves_is_read_again_and_advanced_on_the_next_pass(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    run.push(HEAD_A)
    run.tick(1000)
    run.tick(1100)
    state = run.grade(HEAD_A)
    state.superseded = True
    run.lab.build(0, Policy(), None).save(state)
    assert f"posted lint completed on {HEAD_A}" in run.tick(1200)
    assert run.lab.poster.moves(HEAD_A, "lint")[-1] == ("completed", "cancelled")


def _pair(tmp_path: Path, plans: dict[str, scheduler.Plan]) -> tuple[Run, Run]:
    runs: list[Run] = []
    for name in ("warm", "rebuilt"):
        state = Lab(fake_redis(), LocalObjects(tmp_path / name), Inbox(), Expander(plans))
        spending = budget.Budget(state.client, partial(getattr, state, "now"), LOW_BUDGET)
        run = Run(state, state.build(0, RETRIES, spending))
        _spend(run)
        run.engine.queue.offer("worker", 8, 1000)
        state.poster = Holding(refusals={(HEAD_A, "in_progress"): PostRefused("POST check-runs answered 422")})
        runs.append(run)
    return runs[0], runs[1]


def _rebuild(run: Run) -> None:
    engine = run.lab.build(0, RETRIES, run.engine.spending)
    engine.held, engine.shed, engine.registry = run.engine.held, run.engine.shed, run.engine.registry
    run.engine = engine


def _snapshot(run: Run) -> dict[str, Any]:
    return {
        "documents": _documents(run),
        "live": sorted(run.lab.client.smembers(scheduler.LIVE)),
        "pending": run.engine.queue.pending(),
        "leased": sorted(run.engine.queue.leased()),
        "posts": list(run.lab.poster.posts),
        "held": {repo: until for repo, until in run.engine.held.items() if until},
    }


def _tick(warm: Run, rebuilt: Run, at: int) -> list[str]:
    _rebuild(rebuilt)
    actions = warm.tick(at)
    assert rebuilt.tick(at) == actions
    assert _snapshot(warm) == _snapshot(rebuilt)
    return actions


@pytest.mark.feature("lab-scheduler-pass-moves-changed-grades")
def test_a_warm_scheduler_grades_every_pass_like_one_rebuilt_from_redis(tmp_path: Path) -> None:
    first, second, third, fourth, fifth = (unit(number) for number in range(1, 6))
    plans = {HEAD_A: plan({"lint": (first, second, third)}), HEAD_B: plan({"lint": (fourth, fifth)})}
    warm, rebuilt = _pair(tmp_path, plans)
    both = (warm, rebuilt)
    for run in both:
        run.push(HEAD_A, lane="pr-1", event="pull_request", action="synchronize")
        run.push(HEAD_B, lane="sweep", event="backfill")
    assert sum(action.startswith("enqueued 3 units of ") for action in _tick(warm, rebuilt, 1000)) == 1
    assert _tick(warm, rebuilt, 1100) == []
    leased = {id(run): run.lease(slots=2, ttl_ms=10 * DAY_MS) for run in both}
    for run in both:
        run.finish(leased[id(run)][0], "passed", {"seconds": 1.0})
    assert f"held lint in_progress on {HEAD_A} for a later pass: " in "\n".join(_tick(warm, rebuilt, 1200))
    assert _tick(warm, rebuilt, 1300) == []
    for run in both:
        run.finish(leased[id(run)][1], "failed", {"seconds": 1.0})
    _tick(warm, rebuilt, 1400)
    assert scheduler.job_id(HEAD_A, second) in warm.engine.queue.pending()
    vanished = scheduler.job_id(HEAD_A, third)
    for run in both:
        run.lab.client.delete(f"{queue.PREFIX}job:{vanished}")
        run.lab.client.zrem(f"{queue.PREFIX}pending", vanished)
    _tick(warm, rebuilt, 1500)
    for run in both:
        run.finish(run.lease(ttl_ms=10 * DAY_MS)[0], "passed", {"seconds": 1.0})
    assert _tick(warm, rebuilt, 1600) == []
    assert [slot.outcome for slot in warm.grade(HEAD_A).units.values()] == ["passed", "passed", "dropped"]
    assert f"completed {warm.grade(HEAD_A).run} failure" in _tick(warm, rebuilt, 1200 + HOLD_MS)
    for run in both:
        run.engine.queue.offer("worker", 8, DAY_MS)
    assert f"enqueued 2 units of {warm.grade(HEAD_B).run}" in _tick(warm, rebuilt, DAY_MS)
    for run in both:
        for grant in run.lease(slots=2, ttl_ms=10 * DAY_MS):
            run.finish(grant, "passed", {"seconds": 1.0})
    assert f"completed {warm.grade(HEAD_B).run} success" in _tick(warm, rebuilt, DAY_MS + 100)
    assert warm.lab.client.smembers(scheduler.LIVE) == set()
