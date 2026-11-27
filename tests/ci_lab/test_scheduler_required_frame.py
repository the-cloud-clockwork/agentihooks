import json
from collections import Counter, defaultdict
from collections.abc import Callable

import pytest

from scripts.ci_lab import compact, relay, scheduler
from tests.ci_lab.test_scheduler import BASE, HEAD_A, HEAD_B, REPO, TREE, Run, pull_plan, unit
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler_posts import refusing


def _release(run: Run, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "PULL_REQUIRED", (*scheduler.PULL_REQUIRED, "release-job"))
    run.lab.expander.plans[HEAD_A] = pull_plan({"lint": (unit(1),), "release-job": (unit(2),)})


@pytest.mark.feature("lab-grade-required-list-regrade")
@pytest.mark.parametrize(
    ("tier", "plant", "scope"),
    [
        (scheduler.Priority.HOTFIX, False, "none"),
        (scheduler.Priority.PULL_REQUEST, False, "none"),
        (scheduler.Priority.PULL_REQUEST, True, "full"),
        (scheduler.Priority.QUEUE, False, "none"),
        (scheduler.Priority.PROMOTION, False, "none"),
        (scheduler.Priority.BACKFILL, False, "none"),
    ],
)
def test_the_plan_keeps_only_the_jobs_its_tier_requires(tier: scheduler.Priority, plant: bool, scope: str) -> None:
    plan = scheduler.frame(tier, TREE, BASE, {}, plant=plant)
    expected = ()
    if tier == scheduler.Priority.QUEUE:
        expected = scheduler.QUEUE_REQUIRED
    elif tier < scheduler.Priority.PROMOTION:
        expected = (*scheduler.PULL_REQUIRED, *scheduler.SCOPED.get(scope, ()))
    assert getattr(plan, "required", None) == expected


@pytest.mark.feature("lab-grade-required-list-regrade")
def test_a_started_grade_stores_its_planned_required_list(lab: Callable[..., Run]) -> None:
    plan = pull_plan({"lint": (unit(1),)})
    run = lab({HEAD_A: plan})
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    grade = run.grade(HEAD_A)
    expected = list(scheduler.PULL_REQUIRED)
    assert getattr(grade, "required", None) == expected
    assert json.loads(run.lab.client.get(scheduler.GRADES + grade.run))["required"] == expected


@pytest.mark.feature("lab-grade-required-list-regrade")
@pytest.mark.parametrize("quiet", [False, True])
def test_a_release_regrades_a_live_frame_without_posting_failed_required(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, quiet: bool
) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    old = run.grade(HEAD_A)
    grant = run.lease(ttl_ms=10000)[0]
    run.tick(1100)
    if not quiet:
        run.finish(grant, "passed", {})
    _release(run, monkeypatch)
    actions = run.tick(2000)
    assert any(action.startswith("grading ") for action in actions)
    grades = run.grades()
    assert len(grades) == 2
    replacement = next(grade for grade in grades if grade.run != old.run)
    assert "release-job" in replacement.gates
    assert getattr(replacement, "required", None) == list(scheduler.PULL_REQUIRED)
    retired = run.engine.grade(old.run)
    assert retired is not None and retired.superseded
    assert not run.lab.client.sismember(scheduler.LIVE, old.run)
    assert ("completed", "failure") not in run.lab.poster.moves(HEAD_A, scheduler.REQUIRED)
    original_ref = old.gates["lint"][0]
    if quiet:
        assert replacement.units[original_ref].job == grant.job
        run.finish(grant, "passed", {})
    else:
        assert replacement.units[original_ref].outcome == "passed"
        assert replacement.units[original_ref].job == ""
    run.tick(2100)
    advanced = run.engine.grade(replacement.run)
    assert advanced is not None and advanced.units[original_ref].outcome == "passed"


@pytest.mark.feature("lab-grade-required-list-regrade")
def test_a_release_reuses_queued_work(lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    old = run.grade(HEAD_A)
    original_ref = old.gates["lint"][0]
    _release(run, monkeypatch)
    assert any(action.startswith("grading ") for action in run.tick(2000))
    replacement = next(grade for grade in run.grades() if grade.run != old.run)
    assert replacement.units[original_ref].job == old.units[original_ref].job
    grant = run.lease()[0]
    assert grant.job == old.units[original_ref].job
    run.finish(grant, "passed", {})
    run.tick(2100)
    advanced = run.engine.grade(replacement.run)
    assert advanced is not None and advanced.units[original_ref].outcome == "passed"


@pytest.mark.feature("lab-grade-required-list-regrade")
def test_a_stale_superseded_grade_closes_after_its_first_posts_were_held(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    refusing(run, scheduler.PostRefused("held"), heads=frozenset({HEAD_A}))
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    old = run.grade(HEAD_A)
    assert old.posted == {}
    _release(run, monkeypatch)
    run.lab.expander.plans[HEAD_B] = pull_plan({"lint": (unit(2),)})
    run.push(HEAD_B, event="pull_request", action="synchronize")
    run.tick(2000)
    assert run.grade(HEAD_B).posted[scheduler.REQUIRED] == ["queued", None]
    run.tick(1000 + scheduler.POST_HOLD_MS)
    retired = run.engine.grade(old.run)
    assert retired is not None and retired.conclusion == "cancelled"
    grant = run.lease()[0]
    run.finish(grant, "passed", {})
    run.tick(1100 + scheduler.POST_HOLD_MS)
    assert run.grade(HEAD_B).conclusion == "success"


@pytest.mark.feature("lab-grade-required-list-regrade")
def test_stale_intake_regrades_once_and_reuses_the_matching_replacement(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    run.cache(unit(1))
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    old = run.grade(HEAD_A)
    assert old.conclusion == "success"
    _release(run, monkeypatch)
    request = run.lab.expander.seen[0]
    action = run.engine._start(request, "feature", "", 2000)
    assert action.startswith("grading ")
    replacement = next(grade for grade in run.grades() if grade.run != old.run)
    assert replacement.units[old.gates["lint"][0]].outcome == "passed"
    assert run.engine._start(request, "feature", "", 3000) == f"already graded {replacement.run}"
    assert len(run.grades()) == 2


@pytest.mark.feature("lab-grade-required-list-regrade")
@pytest.mark.parametrize("tier", [scheduler.Priority.PULL_REQUEST, scheduler.Priority.QUEUE])
def test_a_legacy_grade_derives_required_from_its_stored_gates(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch, tier: scheduler.Priority
) -> None:
    plan = scheduler.frame(tier, TREE, BASE, {})
    run = lab({HEAD_A: plan})
    request = scheduler.GradeRequest(REPO, HEAD_A, BASE, tier, "trusted", False)
    run.engine._start(request, "feature", "", 1000)
    old = run.grade(HEAD_A)
    document = json.loads(run.lab.client.get(scheduler.GRADES + old.run))
    document.pop("required", None)
    expected = [gate for gate in document["gates"] if gate not in (scheduler.FAIL_FAST, *scheduler.UNIT_JOBS)]
    monkeypatch.setattr(scheduler, "PULL_REQUIRED", (*scheduler.PULL_REQUIRED, "later-release-job"))
    parsed = scheduler._parse(json.dumps(document))
    assert getattr(parsed, "required", None) == expected


@pytest.mark.feature("lab-grade-required-list-regrade")
def test_a_matching_frame_keeps_existing_grade_reuse_and_success(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    run.cache(unit(1))
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    old = run.grade(HEAD_A)
    assert old.conclusion == "success"
    request = run.lab.expander.seen[0]
    assert run.engine._start(request, "feature", "", 2000) == f"already graded {old.run}"
    assert len(run.grades()) == 1
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED)[-1] == ("completed", "success")


@pytest.mark.feature("lab-grade-required-list-regrade")
def test_compaction_round_trips_the_stored_required_frame(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    grade = run.grade(HEAD_A)
    key = scheduler.GRADES + grade.run
    document = json.loads(run.lab.client.get(key))
    document["required"] = list(scheduler.PULL_REQUIRED)
    next(iter(document["units"].values()))["manifest"] = json.dumps({"example": "kept"})
    run.lab.client.set(key, json.dumps(document))
    counts: Counter[str] = Counter()
    compact._grade(run.lab.client, run.lab.objects, key, set(), defaultdict(dict), counts)
    assert counts["grades stripped"] == 1
    compacted = json.loads(run.lab.client.get(key))
    assert compacted["required"] == document["required"]
    assert all("manifest" not in slot for slot in compacted["units"].values())
    assert getattr(scheduler._parse(json.dumps(compacted)), "required", None) == document["required"]


@pytest.mark.feature("lab-grade-required-list-regrade")
def test_relay_parses_the_frame_and_ignores_a_superseded_grade(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})})
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    grade = run.grade(HEAD_A)
    document = json.loads(run.lab.client.get(scheduler.GRADES + grade.run))
    document["required"] = list(scheduler.PULL_REQUIRED)
    document["superseded"] = True
    parsed = scheduler._parse(json.dumps(document))
    instance = object.__new__(relay.Relay)
    item = relay.Pending(REPO, HEAD_A, "refs/heads/grade", "feature")
    assert instance._relay(item, [parsed]) == [f"dropped {HEAD_A} for feature: every grade superseded"]
