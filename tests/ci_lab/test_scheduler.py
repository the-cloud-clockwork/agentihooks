import copy
import io
import json
import logging
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from scripts.ci_lab import __main__ as cli
from scripts.ci_lab import budget, control, delivery, evidence, expand_tests, lease_api, queue, scheduler
from scripts.ci_lab.capacity import Load
from scripts.ci_lab.checks import Output
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.digests import GradeInputs, StaticInputs, Toolchain, grade_key, static_key
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.leases import Grant, Leases
from scripts.ci_lab.objects import LocalObjects, manifest_folder, unit_path, upload_path
from scripts.ci_lab.outcomes import CRASH
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.static_gates import StaticGate
from scripts.ci_lab.store import Store, Verdict
from scripts.ci_lab.units import Priority, Unit
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.events import bus
from scripts.events.envelope import Envelope
from tests import dep_manifest
from tests.ci_lab.gate_required_cases import load, materialize
from tests.dep_manifest import _git

REPO = "/the-cloud-clockwork/agentihooks"
SLUG = "the-cloud-clockwork/agentihooks"
ACTOR = "nestorcolt"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
BASE = "c" * 40
TREE = "d" * 40
OTHER_TREE = "e" * 40

Post = tuple[str, str, str, str | None, Output, bool]


def fake_redis() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def key(kind: str, number: int) -> str:
    return f"{kind}-v1-{number:064x}"


def unit(number: int, kind: str = "static") -> Unit:
    return Unit(kind, key(kind, number), (f"gate-{number}",))


def ref(item: Unit) -> str:
    return f"{item.kind}:{item.key}"


def plan(gates: dict[str, tuple[Unit, ...]], tree: str = TREE) -> scheduler.Plan:
    return scheduler.Plan(tree, BASE, gates)


def pull_plan(gates: dict[str, tuple[Unit, ...]]) -> scheduler.Plan:
    return scheduler.frame(Priority.PULL_REQUEST, TREE, BASE, gates)


def test_a_tier_frame_requires_the_jobs_its_rules_name_and_keeps_only_the_lab_gates_they_grade() -> None:
    lint, docs, other = (unit(1),), (unit(2),), (unit(3),)
    gates = {"lint": lint, "docs": docs, "other": other}
    pulled = scheduler.frame(Priority.PULL_REQUEST, TREE, BASE, gates)
    assert pulled == scheduler.frame(Priority.HOTFIX, TREE, BASE, gates)
    assert pulled.gates == {job: {"lint": lint, "docs": docs}.get(job, ()) for job in scheduler.PULL_REQUIRED}
    assert (pulled.tree, pulled.grader, pulled.skipped, pulled.outputs, pulled.flags) == (
        TREE,
        BASE,
        frozenset(),
        {"select": {"scope": "none"}},
        scheduler.Flags(),
    )
    queued = scheduler.frame(Priority.QUEUE, TREE, BASE, {**gates, "unit": other})
    required = ("select", "split", "shard-check", "test-count", "queue-fail-fast", "unit")
    assert queued.gates == {job: other if job == "unit" else () for job in required}
    assert (queued.tree, queued.grader, queued.skipped, queued.outputs) == (
        TREE,
        BASE,
        frozenset({"unit-lost", "unit-rerun"}),
        {},
    )
    for tier in (Priority.PROMOTION, Priority.BACKFILL):
        assert scheduler.frame(tier, TREE, BASE, gates) == scheduler.Plan(TREE, BASE, gates)


@dataclass
class Inbox:
    pending: list[scheduler.Head] = field(default_factory=list[scheduler.Head])

    def heads(self) -> list[scheduler.Head]:
        heads, self.pending = self.pending, []
        return heads


@dataclass
class Expander:
    plans: dict[str, scheduler.Plan]
    seen: list[GradeRequest] = field(default_factory=list[GradeRequest])

    def expand(self, request: GradeRequest) -> scheduler.Plan:
        self.seen.append(request)
        return self.plans[request.head]


@dataclass
class Poster:
    posts: list[Post] = field(default_factory=list[Post])

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        self.posts.append((head, gate, status, conclusion, output, shadow))
        return len(self.posts)

    def moves(self, head: str, gate: str) -> list[tuple[str, str | None]]:
        return [(post[2], post[3]) for post in self.posts if post[0] == head and post[1] == gate]


@dataclass
class Lab:
    client: Any
    objects: LocalObjects
    inbox: Inbox
    expander: Expander
    poster: Poster = field(default_factory=Poster)
    fixes: set[str] = field(default_factory=set[str])
    asked: list[int] = field(default_factory=list[int])
    repos: list[str] = field(default_factory=list[str])
    now: int = 0

    def checks(self, repo: str) -> Poster:
        self.repos.append(repo)
        return self.poster

    def hotfixes(self) -> frozenset[str]:
        self.asked.append(self.now)
        return frozenset(self.fixes)

    def build(self, quiet_ms: int, retries: Policy) -> scheduler.Scheduler:
        ports = scheduler.Ports(self.inbox, self.expander, self.checks, self.hotfixes)
        settings = scheduler.Settings(frozenset({ACTOR}), quiet_ms, retries)
        return scheduler.Scheduler(self.client, self.objects, ports, settings)


@dataclass
class Run:
    lab: Lab
    engine: scheduler.Scheduler

    @property
    def store(self) -> Store:
        return self.engine.store

    def push(
        self, head: str, lane: str = "feature", event: str = "push", action: str = "pushed", actor: str = ACTOR
    ) -> None:
        self.lab.inbox.pending.append(scheduler.Head(HeadEvent(event, REPO, head, BASE, actor, action), lane))

    def tick(self, at: int) -> list[str]:
        self.lab.now = at
        return self.engine.tick(at)

    def leases(self, ttl_ms: int = 1000) -> Leases:
        return Leases(self.engine.queue, ttl_ms, lambda: self.lab.now, lambda _: None, 1)

    def lease(self, slots: int = 1, ttl_ms: int = 1000) -> list[Grant]:
        return self.leases(ttl_ms).lease("worker", slots, frozenset(), 0)

    def finish(self, grant: Grant, outcome: str, report: dict[str, Any] | None = None, log: bool = True) -> None:
        if report is not None:
            self.lab.objects.write_once(upload_path(grant.job, "report"), json.dumps(report).encode())
            if log:
                self.lab.objects.write_once(upload_path(grant.job, "log"), b"unit log")
        assert self.leases().complete(grant.job, grant.token, outcome)

    def grades(self) -> list[scheduler.State]:
        runs = [name.removeprefix(scheduler.GRADES) for name in self.lab.client.keys(scheduler.GRADES + "*")]
        return [grade for run in runs if (grade := self.engine.grade(run)) is not None]

    def grade(self, head: str) -> scheduler.State:
        return next(grade for grade in self.grades() if grade.head == head)

    def cache(self, item: Unit, passed: bool = True) -> None:
        self.store.put(Verdict(item.kind, item.key, passed, False, 1.0), {}, b"")


@pytest.fixture
def lab(tmp_path: Path) -> Callable[..., Run]:
    def make(plans: dict[str, scheduler.Plan], quiet_ms: int = 0, retries: Policy = Policy()) -> Run:
        client = fake_redis()
        state = Lab(client, LocalObjects(tmp_path / "objects"), Inbox(), Expander(plans))
        return Run(state, state.build(quiet_ms, retries))

    return make


@pytest.mark.feature("lab-scheduler-grade")
def test_a_fully_cached_head_enqueues_nothing_and_completes_at_once(lab: Callable[..., Run]) -> None:
    units = (unit(1), unit(2))
    run = lab({HEAD_A: plan({"lint": units})})
    for item in units:
        run.cache(item)
    run.push(HEAD_A)
    actions = run.tick(1000)
    grade = run.grade(HEAD_A)
    assert grade.run == grade_key(GradeInputs(REPO, HEAD_A, BASE, CLASSES[Priority.PROMOTION], BASE))
    assert grade.lane == "feature"
    assert run.engine.queue.pending() == []
    assert grade.conclusion == "success"
    assert run.lab.poster.moves(HEAD_A, "lint") == [("completed", "success")]
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED) == [("completed", "success")]
    assert run.lab.client.smembers(scheduler.LIVE) == set()
    assert f"grading {grade.run} with 0 of 2 units enqueued" in actions
    assert f"completed {grade.run} success" in actions
    assert run.lab.repos == [SLUG]


@pytest.mark.feature("lab-scheduler-evidence")
def test_a_folded_grade_is_recorded_so_show_finds_it_by_run_and_head(lab: Callable[..., Run]) -> None:
    units = (unit(1), unit(2))
    run = lab({HEAD_A: plan({"lint": units})})
    for item in units:
        run.cache(item)
    run.push(HEAD_A)
    run.tick(1000)
    grade = run.grade(HEAD_A)
    text, missing = evidence.show(run.lab.objects, [grade.run])
    paths = [unit_path(item.kind, item.key, name) for item in units for name in ("report", "log")]
    assert missing == []
    assert text == "\n".join(
        [
            f"grade {grade.run}",
            f"repository {grade.repo}",
            f"head {HEAD_A}",
            f"base {BASE}",
            f"tier {CLASSES[grade.tier]}",
            f"grader {BASE}",
            "kinds static:2",
            f"unit static {units[0].key} run {grade.run}",
            f"  report {paths[0]}",
            f"  log {paths[1]}",
            f"unit static {units[1].key} run {grade.run}",
            f"  report {paths[2]}",
            f"  log {paths[3]}",
            "",
        ]
    )
    assert run.lab.objects.keys(evidence.head_prefix(HEAD_A)) == [evidence.head_prefix(HEAD_A) + grade.run]


@pytest.mark.feature("lab-scheduler-evidence")
def test_a_grade_is_recorded_only_when_its_last_unit_is_final(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})})
    run.push(HEAD_A)
    run.tick(1000)
    grade = run.grade(HEAD_A)
    assert evidence.show(run.lab.objects, [grade.run]) == ("", [evidence.grade_path(grade.run)])
    assert run.lab.objects.keys(evidence.head_prefix(HEAD_A)) == []
    run.finish(run.lease()[0], "passed", {"seconds": 2.0})
    run.tick(1100)
    text, missing = evidence.show(run.lab.objects, [grade.run])
    assert missing == []
    assert f"\nunit static {item.key} run {grade.run}\n" in text
    assert run.lab.objects.keys(evidence.head_prefix(HEAD_A)) == [evidence.head_prefix(HEAD_A) + grade.run]


@pytest.mark.feature("lab-scheduler-evidence")
def test_a_grade_with_a_lost_unit_is_recorded_and_show_names_its_missing_objects(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})}, retries=Policy(lost=0))
    run.push(HEAD_A)
    run.tick(1000)
    run.lease(ttl_ms=100)
    run.tick(1100)
    grade = run.grade(HEAD_A)
    text, missing = evidence.show(run.lab.objects, [grade.run])
    assert grade.conclusion == "failure"
    assert missing == [unit_path(item.kind, item.key, "report"), unit_path(item.kind, item.key, "log")]
    assert f"\nunit static {item.key} run {grade.run}\n" in text
    assert run.lab.objects.keys(evidence.head_prefix(HEAD_A)) == [evidence.head_prefix(HEAD_A) + grade.run]


@pytest.mark.feature("lab-scheduler-grade")
def test_a_newer_head_drops_pending_units_and_reuses_carried_verdicts(lab: Callable[..., Run]) -> None:
    running, finished, waiting, fresh = unit(1), unit(2), unit(3), unit(4)
    run = lab(
        {
            HEAD_A: plan({"semgrep": (finished,), "lint": (running, waiting)}),
            HEAD_B: plan({"semgrep": (finished,), "lint": (running, waiting, fresh)}),
        }
    )
    run.push(HEAD_A)
    run.tick(1000)
    grants = run.lease(slots=2)
    assert [grant.unit for grant in grants] == [finished, running]
    run.finish(grants[0], "passed", {"seconds": 2.0})
    run.tick(1100)
    run.push(HEAD_B)
    run.tick(1200)
    old, new = run.grade(HEAD_A), run.grade(HEAD_B)
    assert old.superseded
    assert run.engine.queue.job(scheduler.job_id(HEAD_A, waiting)) is None
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, waiting), scheduler.job_id(HEAD_B, fresh)]
    assert new.units[ref(running)].job == grants[1].job
    assert new.units[ref(finished)].outcome == "passed"
    assert new.units[ref(finished)].job == ""
    assert run.lab.poster.moves(HEAD_A, "semgrep")[-1] == ("completed", "success")
    assert run.lab.poster.moves(HEAD_A, "lint")[-1] == ("completed", "cancelled")
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED)[-1] == ("completed", "cancelled")
    run.finish(grants[1], "passed", {})
    for grant in run.lease(slots=2):
        run.finish(grant, "passed", {})
    actions = run.tick(1300)
    assert run.grade(HEAD_B).conclusion == "success"
    assert run.grade(HEAD_A).conclusion == "cancelled"
    assert f"completed {old.run} cancelled" in actions
    assert run.lab.client.smembers(scheduler.LIVE) == set()


@pytest.mark.feature("lab-scheduler-grade")
def test_a_lost_worker_requeues_once_then_its_unit_makes_the_grade_red(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})})
    run.push(HEAD_A)
    run.tick(1000)
    first = run.lease(ttl_ms=100)[0]
    assert run.tick(1099) == [f"posted lint in_progress on {HEAD_A}", f"posted required in_progress on {HEAD_A}"]
    assert run.tick(1100) == [f"reaped {first.job} pending"]
    job = run.engine.queue.job(first.job)
    assert job is not None and (job.state, job.attempts) == ("pending", 1)
    second = run.lease(ttl_ms=100)[0]
    assert second.job == first.job
    assert f"reaped {second.job} lost" in run.tick(1300)
    assert run.grade(HEAD_A).conclusion == "failure"
    assert run.lab.poster.moves(HEAD_A, "lint")[-1] == ("completed", "failure")
    assert run.store.get(item.kind, item.key) is None


@pytest.mark.feature("lab-worker-executors")
def test_each_enqueued_job_carries_the_grades_base_and_its_plans_grader(lab: Callable[..., Run]) -> None:
    grader = "e" * 40
    run = lab({HEAD_A: scheduler.Plan(TREE, grader, {"lint": (unit(1),)})})
    run.push(HEAD_A)
    run.tick(1000)

    [grant] = run.lease()

    assert (grant.head, grant.base, grant.grader) == (HEAD_A, BASE, grader)


def test_the_lost_retries_come_from_the_settings(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, retries=Policy(lost=0))
    run.push(HEAD_A)
    run.tick(1000)
    grant = run.lease(ttl_ms=100)[0]
    assert f"reaped {grant.job} lost" in run.tick(1100)
    assert run.grade(HEAD_A).conclusion == "failure"


@pytest.mark.feature("lab-scheduler-grade")
def test_a_unit_that_cannot_run_makes_the_grade_red_and_stores_no_verdict(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})})
    run.push(HEAD_A)
    run.tick(1000)
    run.finish(run.lease()[0], scheduler.CANNOT_RUN, {"error": "bwrap is not installed"})
    run.tick(1100)
    assert run.grade(HEAD_A).conclusion == "failure"
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED)[-1] == ("completed", "failure")
    assert run.store.get(item.kind, item.key) is None


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.parametrize(("retries", "runs"), [(Policy(), 2), (Policy(lost=0), 1), (Policy(lost=2), 3)])
def test_a_crashed_unit_reruns_while_the_lost_retries_allow_then_makes_the_grade_red(
    lab: Callable[..., Run], retries: Policy, runs: int
) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})}, retries=retries)
    run.push(HEAD_A)
    run.tick(1000)
    job = scheduler.job_id(HEAD_A, item)
    for attempt in range(runs):
        grants = run.lease()
        assert [grant.job for grant in grants] == [job]
        run.finish(grants[0], CRASH)
        run.tick(1100 + attempt)
        record = run.engine.queue.job(job)
        assert record is not None
        final = attempt == runs - 1
        assert (record.state, record.attempts) == ("done" if final else "pending", attempt if final else attempt + 1)
        assert run.grade(HEAD_A).units[ref(item)].outcome == (CRASH if final else "")
    assert run.lease() == []
    assert run.grade(HEAD_A).conclusion == "failure"
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED)[-1] == ("completed", "failure")
    assert run.store.get(item.kind, item.key) is None


@pytest.mark.feature("lab-scheduler-test-units")
def test_a_failed_unit_reruns_while_the_failed_retries_allow_and_its_pass_is_stored(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})}, retries=Policy(failed=1))
    run.push(HEAD_A)
    run.tick(1000)
    first = run.lease()[0]
    run.finish(first, scheduler.FAILED, {"seconds": 3.0})
    run.tick(1100)
    assert run.store.get(item.kind, item.key) is None
    assert run.grade(HEAD_A).units[ref(item)].outcome == ""
    second = run.lease()[0]
    assert second.job == first.job
    run.finish(second, scheduler.PASSED, {"seconds": 3.0})
    run.tick(1200)
    assert run.grade(HEAD_A).conclusion == "success"
    assert run.store.get(item.kind, item.key) == Verdict(item.kind, item.key, True, False, 3.0)


@pytest.mark.feature("lab-unit-outcomes")
def test_an_unknown_outcome_is_refused_on_completion_and_stores_no_verdict(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})})
    run.push(HEAD_A)
    run.tick(1000)
    grant = run.lease()[0]
    run.lab.objects.write_once(upload_path(grant.job, "report"), json.dumps({"seconds": 1.0}).encode())
    run.lab.objects.write_once(upload_path(grant.job, "log"), b"unit log")

    assert run.leases().complete(grant.job, grant.token, "taken") is False
    run.tick(1100)

    job = run.engine.queue.job(grant.job)
    assert job is not None and (job.state, job.outcome) == ("leased", "")
    assert run.grade(HEAD_A).units[ref(item)].outcome == ""
    assert run.grade(HEAD_A).conclusion == ""
    assert run.store.get(item.kind, item.key) is None


@pytest.mark.feature("lab-scheduler-grade")
def test_the_fold_reproduces_gate_required_on_recorded_fixtures() -> None:
    fixture = load()
    wrong: list[str] = []
    for case in fixture["cases"]:
        event, needs, flags = materialize(fixture, case)
        if scheduler.fold(event, needs, scheduler.Flags(**flags)) != case["passes"]:
            wrong.append(case["name"])
    assert wrong == []


def test_the_fold_reads_a_grade_as_needs_of_its_tier(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)}), HEAD_B: pull_plan({"lint": (unit(2),)})})
    run.cache(unit(1))
    run.cache(unit(2), passed=False)
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.push(HEAD_B, lane="other", event="pull_request", action="synchronize")
    run.tick(1000)
    assert run.grade(HEAD_A).conclusion == "success"
    assert run.grade(HEAD_B).conclusion == "failure"
    assert run.lab.poster.moves(HEAD_A, "lint") == [("completed", "success")]
    assert run.lab.poster.moves(HEAD_A, "docs") == []


def test_framed_grades_fold_green_unless_a_lab_gate_fails(lab: Callable[..., Run]) -> None:
    good, bad = unit(1), unit(2)
    run = lab(
        {
            HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (good,)}),
            HEAD_B: scheduler.frame(Priority.QUEUE, OTHER_TREE, BASE, {"unit": (bad,)}),
        }
    )
    run.cache(good)
    run.cache(bad, passed=False)
    run.push(HEAD_A, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.push(HEAD_B, lane="gh-readonly-queue/dev/pr-2", event="merge_group", action="checks_requested")
    run.tick(1000)
    assert run.grade(HEAD_A).conclusion == "success"
    assert run.grade(HEAD_B).conclusion == "failure"
    assert run.lab.client.get(f"{scheduler.TREES}{REPO}|{TREE}") == run.grade(HEAD_A).run
    assert run.lab.client.get(f"{scheduler.TREES}{REPO}|{OTHER_TREE}") is None


def test_a_head_waits_out_its_lanes_quiet_window_and_a_newer_head_restarts_it(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})}, quiet_ms=500)
    run.push(HEAD_A)
    assert run.tick(1000) == [f"received {HEAD_A} on feature"]
    run.push(HEAD_A)
    assert run.tick(1300) == [f"holding {HEAD_A}"]
    run.push(HEAD_B)
    assert run.tick(1400) == [f"received {HEAD_B} on feature"]
    assert run.tick(1899) == []
    assert run.lab.expander.seen == []
    run.tick(1900)
    assert [request.head for request in run.lab.expander.seen] == [HEAD_B]
    assert run.lab.client.hgetall(scheduler.WAITING) == {}


def test_a_held_head_is_released_at_its_quiet_window_to_the_millisecond(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=500)
    run.push(HEAD_A)
    run.tick(1000)
    run.push(HEAD_A)
    run.tick(1300)
    assert run.tick(1499) == []
    assert run.tick(1500)[0].startswith("grading ")


def test_a_lane_still_quiet_holds_back_no_other_lane(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})}, quiet_ms=500)
    run.push(HEAD_B, lane="b")
    run.tick(1000)
    run.push(HEAD_A, lane="a")
    run.tick(1400)
    run.tick(1500)
    assert run.lab.expander.seen == [GradeRequest(REPO, HEAD_B, BASE, Priority.PROMOTION, ACTOR, False)]
    assert list(run.lab.client.hgetall(scheduler.WAITING)) == [f"{REPO}|a|{int(Priority.PROMOTION)}"]


def test_a_head_on_a_panic_fix_branch_grades_in_the_hotfix_class_without_waiting(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})}, quiet_ms=60_000)
    run.lab.fixes.add("fix-branch")
    run.tick(500)
    assert run.lab.asked == []
    run.push(HEAD_A, lane="fix-branch", event="pull_request", action="synchronize")
    run.tick(1000)
    assert run.lab.asked == [1000]
    assert [request.tier for request in run.lab.expander.seen] == [Priority.HOTFIX]
    assert run.grade(HEAD_A).tier == Priority.HOTFIX
    assert run.lab.poster.posts[0][4].title == "hotfix grade queued"


def test_a_head_off_the_fix_branches_keeps_its_class(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})}, quiet_ms=0)
    run.lab.fixes.add("fix-branch")
    run.push(HEAD_A, lane="feature", event="pull_request", action="synchronize")
    run.tick(1000)
    assert run.grade(HEAD_A).tier == Priority.PULL_REQUEST


@pytest.mark.feature("lab-control-routes")
def test_a_head_marked_hotfix_by_its_commit_grades_in_the_hotfix_class(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})}, quiet_ms=60_000)
    run.lab.fixes.add(HEAD_A)
    run.push(HEAD_A, lane="feature", event="pull_request", action="synchronize")
    run.tick(1000)
    assert run.grade(HEAD_A).tier == Priority.HOTFIX


@pytest.mark.feature("lab-control-routes")
def test_a_paused_class_holds_its_heads_but_the_excepted_ones_until_resume(lab: Callable[..., Run]) -> None:
    held, by_lane, by_commit = HEAD_A, HEAD_B, "c" * 40
    run = lab({head: pull_plan({"lint": (unit(1),)}) for head in (held, by_lane, by_commit)}, quiet_ms=0)
    control.pause(run.lab.client, "pull_request", ["fix-branch", by_commit])
    run.push(held, lane="feature", event="pull_request", action="synchronize")
    run.push(by_lane, lane="fix-branch", event="pull_request", action="synchronize")
    run.push(by_commit, lane="other", event="pull_request", action="synchronize")
    run.tick(1000)
    run.tick(2000)
    assert sorted(grade.head for grade in run.grades()) == sorted([by_lane, by_commit])
    assert run.lab.client.hlen(scheduler.WAITING) == 1
    control.resume(run.lab.client)
    run.tick(3000)
    assert run.grade(held).tier == Priority.PULL_REQUEST
    assert run.lab.client.hlen(scheduler.WAITING) == 0


@pytest.mark.feature("lab-control-routes")
def test_a_pause_of_another_class_leaves_pull_request_grading_alone(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: pull_plan({"lint": (unit(1),)})}, quiet_ms=0)
    control.pause(run.lab.client, "queue", [])
    run.push(HEAD_A, lane="feature", event="pull_request", action="synchronize")
    run.tick(1000)
    assert run.grade(HEAD_A).tier == Priority.PULL_REQUEST


def test_an_untrusted_head_is_never_held_or_graded(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_B: plan({"lint": (unit(1),)})}, quiet_ms=500)
    run.push(HEAD_A, actor="stranger")
    run.push(HEAD_B, lane="other")
    assert run.tick(1000) == [f"untrusted stranger head {HEAD_A}", f"received {HEAD_B} on other"]
    run.tick(1500)
    assert [request.head for request in run.lab.expander.seen] == [HEAD_B]
    assert run.lab.client.hgetall(scheduler.WAITING) == {}


def test_check_runs_move_queued_on_receipt_in_progress_on_the_first_unit_and_completed_on_the_last(
    lab: Callable[..., Run],
) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first, second)})})
    run.push(HEAD_A)
    run.tick(1000)
    grant = run.lease()[0]
    run.tick(1100)
    run.finish(grant, "passed", {})
    assert run.tick(1200) == []
    run.finish(run.lease()[0], "passed", {})
    run.tick(1300)
    moves = [("queued", None), ("in_progress", None), ("completed", "success")]
    assert run.lab.poster.moves(HEAD_A, "lint") == moves
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED) == moves
    grade = run.grade(HEAD_A)
    for _, gate, _, _, output, shadow in run.lab.poster.posts:
        assert shadow is True
        assert (output.run, output.grader, output.details) == (grade.run, BASE, f"{REPO}/commit/{HEAD_A}")
        assert output.units == (first, second)
        assert output.summary == f"2 units of {gate} on {HEAD_A} against {BASE}"
    assert run.lab.poster.posts[-1][4].title == "promotion grade completed"


def test_a_collected_unit_keeps_its_outcome_once_its_job_is_gone(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1), unit(2))})})
    run.push(HEAD_A)
    run.tick(1000)
    grant = run.lease()[0]
    run.finish(grant, "passed", {})
    run.tick(1100)
    run.lab.client.delete(f"{queue.PREFIX}job:{grant.job}")
    run.tick(1200)
    assert run.grade(HEAD_A).units[ref(grant.unit)].outcome == "passed"


def test_a_grade_concludes_by_its_tiers_rules_over_its_gates(lab: Callable[..., Run]) -> None:
    item = unit(1)
    skipping = scheduler.Plan(TREE, BASE, {"unit": (item,)}, frozenset({"lint"}))
    reused = scheduler.Plan(TREE, BASE, {"unit": (item,)}, flags=scheduler.Flags(reused="true"))
    run = lab({HEAD_A: skipping, HEAD_B: reused})
    run.cache(item)
    run.push(HEAD_A)
    run.push(HEAD_B, lane="other")
    run.tick(1000)
    assert run.grade(HEAD_A).conclusion == "success"
    assert run.grade(HEAD_B).conclusion == "failure"
    assert run.lab.poster.moves(HEAD_B, "unit") == [("completed", "success")]


def test_a_dev_push_grade_reuses_the_merge_group_grade_of_the_same_tree(lab: Callable[..., Run]) -> None:
    queued, pushed, other = unit(1), unit(2), unit(3)
    run = lab(
        {
            HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (queued,)}),
            HEAD_B: plan({"unit": (pushed,)}),
            BASE: plan({"unit": (other,)}, tree=OTHER_TREE),
        }
    )
    run.push(HEAD_A, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.tick(1000)
    run.finish(run.lease()[0], "passed", {})
    run.tick(1100)
    queue = run.grade(HEAD_A)
    assert queue.conclusion == "success"
    assert run.lab.client.get(f"{scheduler.TREES}{REPO}|{TREE}") == queue.run
    run.push(HEAD_B, lane="dev")
    run.tick(1200)
    pushed_grade = run.grade(HEAD_B)
    assert run.engine.queue.pending() == []
    assert pushed_grade.gates == {"unit": [ref(queued)]}
    assert pushed_grade.conclusion == "success"
    assert run.lab.client.get(f"{scheduler.TREES}{REPO}|{TREE}") == queue.run
    run.push(BASE, lane="dev")
    run.tick(1300)
    assert run.engine.queue.pending() == [scheduler.job_id(BASE, other)]


def test_a_framed_push_grade_carries_the_unit_suite_its_merge_group_grade_graded(lab: Callable[..., Run]) -> None:
    suite, pushed_suite, lint = unit(1, "test"), unit(2, "test"), unit(3)
    run = lab(
        {
            HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (suite,), "lint": (lint,)}),
            HEAD_B: scheduler.frame(Priority.PROMOTION, TREE, BASE, {"unit": (pushed_suite,), "lint": (lint,)}),
        }
    )
    run.push(HEAD_A, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.tick(1000)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, suite)]
    run.finish(run.lease()[0], "passed", {})
    run.tick(1100)
    assert run.grade(HEAD_A).conclusion == "success"
    run.push(HEAD_B, lane="dev")
    run.tick(1200)
    assert run.grade(HEAD_B).gates["unit"] == [ref(suite)]
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, lint)]


def test_a_merge_group_grade_of_a_tree_serves_no_other_tier(lab: Callable[..., Run]) -> None:
    queued, pulled = unit(1), unit(2)
    run = lab(
        {HEAD_A: scheduler.frame(Priority.QUEUE, TREE, BASE, {"unit": (queued,)}), HEAD_B: plan({"unit": (pulled,)})}
    )
    run.push(HEAD_A, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.tick(1000)
    run.finish(run.lease()[0], "passed", {})
    run.tick(1100)
    assert run.lab.client.get(f"{scheduler.TREES}{REPO}|{TREE}") == run.grade(HEAD_A).run
    run.push(HEAD_B, event="pull_request", action="synchronize")
    run.tick(1200)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, pulled)]


def test_the_same_head_is_graded_once_unless_a_rerun_bypasses_negative_verdicts(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})})
    run.cache(item, passed=False)
    run.push(HEAD_A, event="pull_request", action="synchronize")
    run.tick(1000)
    first = run.grade(HEAD_A)
    assert first.units[ref(item)].outcome == "failed"
    assert run.engine.queue.pending() == []
    run.push(HEAD_A, event="pull_request", action="synchronize")
    assert run.tick(2000) == [f"received {HEAD_A} on feature", f"already graded {first.run}"]
    run.push(HEAD_A, event="check_run", action="rerequested")
    run.tick(3000)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item)]
    assert run.engine.grade(f"{first.run}-3000") is not None


def test_a_graded_job_stores_its_verdict_from_the_uploaded_report(lab: Callable[..., Run]) -> None:
    flaky, failed, silent, unlogged = unit(1), unit(2), unit(3), unit(4)
    run = lab({HEAD_A: plan({"lint": (flaky, failed, silent, unlogged)})})
    run.push(HEAD_A)
    run.tick(1000)
    grants = {grant.unit: grant for grant in run.lease(slots=4)}
    run.finish(grants[flaky], "passed", {"flaky": True, "seconds": 3.5})
    run.finish(grants[failed], "failed", {"flaky": True, "seconds": 1.0})
    run.finish(grants[silent], "passed")
    run.finish(grants[unlogged], "passed", {}, log=False)
    run.tick(1100)
    assert run.store.get(flaky.kind, flaky.key) == Verdict(flaky.kind, flaky.key, True, True, 3.5)
    assert run.store.get(failed.kind, failed.key) == Verdict(failed.kind, failed.key, False, False, 1.0)
    assert run.store.get(silent.kind, silent.key) is None
    assert run.store.get(unlogged.kind, unlogged.key) == Verdict(unlogged.kind, unlogged.key, True, False, 0.0)
    assert run.lab.objects.read(unit_path(flaky.kind, flaky.key, "log")) == b"unit log"
    assert run.lab.objects.read(unit_path(unlogged.kind, unlogged.key, "log")) == b""
    assert run.grade(HEAD_A).conclusion == "failure"


def test_a_head_whose_expansion_fails_is_dropped_with_its_reason(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({})
    run.push(HEAD_A)
    actions = run.tick(1000)
    assert actions[-1].startswith(f"cannot expand {HEAD_A}: ")
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []

    def malformed(request: GradeRequest) -> scheduler.Plan:
        raise yaml.YAMLError(f"malformed workflow at {request.base}")

    monkeypatch.setattr(run.lab.expander, "expand", malformed)
    run.push(HEAD_B)
    assert run.tick(2000)[-1] == f"cannot expand {HEAD_B}: malformed workflow at {BASE}"
    assert run.grades() == []


@pytest.mark.feature("lab-scheduler-test-units")
def test_a_head_whose_grader_delivery_breaks_its_schema_is_dropped_with_its_reason(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({})

    def invalid(request: GradeRequest) -> scheduler.Plan:
        raise jsonschema.ValidationError(f"lab.queue at {request.base} is not of type 'object'")

    monkeypatch.setattr(run.lab.expander, "expand", invalid)
    run.push(HEAD_A)
    assert run.tick(1000)[-1] == f"cannot expand {HEAD_A}: lab.queue at {BASE} is not of type 'object'"
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []


def test_a_live_run_without_a_grade_is_forgotten(lab: Callable[..., Run]) -> None:
    run = lab({})
    run.lab.client.sadd(scheduler.LIVE, "grade-v1-gone")
    assert run.tick(1000) == ["forgot grade-v1-gone"]
    assert run.lab.client.smembers(scheduler.LIVE) == set()


@pytest.mark.feature("lab-scheduler-service")
def test_the_load_counts_each_job_once_in_the_most_urgent_class_of_the_live_grades_holding_it(
    lab: Callable[..., Run],
) -> None:
    cached, stored, shared, joined, solo, pulled, lost = (unit(number) for number in range(1, 8))
    run = lab({HEAD_A: plan({"lint": (cached, stored, shared, joined, solo)}), HEAD_B: plan({"lint": (pulled, lost)})})
    run.cache(cached)
    run.cache(stored)
    run.push(HEAD_A)
    run.tick(1000)
    run.lab.expander.plans[HEAD_A] = plan({"lint": (shared, joined)})
    run.push(HEAD_A, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.push(HEAD_B, lane="pr-2", event="pull_request", action="synchronize")
    run.tick(1100)
    assert [grant.unit for grant in run.lease(slots=2)] == [shared, joined]
    run.lab.client.hset(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, solo)}", "slots", 2)
    run.lab.client.delete(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_B, lost)}")
    run.lab.client.sadd(scheduler.LIVE, "grade-v1-gone")
    estimate = scheduler.UNIT_ESTIMATE_S
    assert run.engine.load(1100) == Load(
        pending={"promotion": [2 * estimate], "pull_request": [estimate]},
        running={"queue": 2},
        slots_total=2,
        slots_busy=2,
        cache_hits={"promotion": 2},
        cache_misses={"promotion": 3, "queue": 2, "pull_request": 2},
        frees_in=(estimate, estimate),
    )


@pytest.mark.feature("lab-scheduler-service")
def test_the_load_counts_leased_slots_and_the_free_slots_workers_offered_within_the_ready_window(
    lab: Callable[..., Run],
) -> None:
    run = lab({})
    big, small = unit(8), unit(9)
    run.engine.queue.enqueue([big, small], HEAD_B, {big.key: queue.Requirement(slots=2)})
    run.lab.now = 1000
    assert [grant.slots for grant in run.leases().lease("busy", 2, frozenset(), 0)] == [2]
    run.engine.queue.offer("idle", 4, 1500)
    run.engine.queue.offer("stale", 8, 999)
    load = run.engine.load(1000 + scheduler.READY_MS)
    assert load == Load(
        pending={},
        running={},
        slots_total=2 + 0 + 4,
        slots_busy=2,
        cache_hits={},
        cache_misses={},
        frees_in=(scheduler.UNIT_ESTIMATE_S,) * 2,
    )
    run.engine.queue.offer("busy", 1, 1000)
    assert run.engine.load(1000 + scheduler.READY_MS).slots_total == 2 + 1 + 4
    assert run.engine.load(1001 + scheduler.READY_MS).slots_total == 2 + 4


@pytest.mark.feature("lab-scheduler-service")
def test_the_grades_of_a_commit_are_its_live_grades_and_the_closed_ones_its_evidence_names(
    lab: Callable[..., Run],
) -> None:
    cached, waiting, other = unit(1), unit(2), unit(3)
    run = lab({HEAD_A: plan({"lint": (cached,)}), HEAD_B: plan({"lint": (other,)})})
    run.cache(cached)
    run.push(HEAD_A)
    run.push(HEAD_B, lane="other")
    run.tick(1000)
    closed = run.grade(HEAD_A)
    run.lab.expander.plans[HEAD_A] = plan({"lint": (waiting,)})
    run.push(HEAD_A, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.tick(2000)
    live = next(grade for grade in run.grades() if grade.head == HEAD_A and grade.run != closed.run)
    done = {"status": "completed", "conclusion": "success"}
    assert scheduler.summary(closed) == {
        "run": closed.run,
        "repo": REPO,
        "head": HEAD_A,
        "base": BASE,
        "class": "promotion",
        "lane": "feature",
        "tree": TREE,
        "grader": BASE,
        "conclusion": "success",
        "superseded": False,
        "units": {"total": 1, "final": 1},
        "checks": {"lint": done, scheduler.REQUIRED: done},
    }
    queued = {"status": "queued", "conclusion": None}
    assert scheduler.summary(live) == {
        **scheduler.summary(live),
        "class": "queue",
        "lane": "gh-readonly-queue/dev/pr-1",
        "conclusion": None,
        "units": {"total": 1, "final": 0},
        "checks": {"lint": queued, scheduler.REQUIRED: queued},
    }
    both = sorted([scheduler.summary(closed), scheduler.summary(live)], key=lambda grade: str(grade["run"]))
    assert run.engine.grades_of(HEAD_A) == both
    assert [grade["head"] for grade in run.engine.grades_of(HEAD_B)] == [HEAD_B]
    assert run.engine.grades_of("f" * 40) == []
    run.lab.client.delete(scheduler.GRADES + closed.run)
    assert run.engine.grades_of(HEAD_A) == [scheduler.summary(live)]


def _read(url: str, data: bytes | None = None) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5) as reply:
        return reply.read()


@pytest.mark.feature("lab-scheduler-service")
def test_listen_serves_leases_capacity_grades_and_metrics_from_one_budgeted_load(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})})
    run.push(HEAD_A)
    run.tick(1000)
    spending = budget.Budget(run.lab.client, lambda: 0, budget.Policy(100.0, {"backfill": 0.5}))
    spending.charge(1, 3600.0)
    routes = lease_api.ControlRoutes(run.lab.client, "tok", lambda: 1000)
    servers = scheduler.listen(run.engine, run.leases(), routes, spending, lambda: 1000, (0, 0))
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    api, exposition = (f"http://127.0.0.1:{server.server_address[1]}" for server in servers)
    try:
        capacity = json.loads(_read(f"{api}/capacity"))
        assert capacity["classes"]["promotion"]["pending"] == 1
        assert capacity["budget_fraction"] == pytest.approx(0.4)
        assert capacity["shed"] == ["backfill"]
        grades = json.loads(_read(f"{api}/grades/{HEAD_A}"))
        assert [grade["run"] for grade in grades["grades"]] == [run.grade(HEAD_A).run]
        body = json.dumps({"worker": "w", "free_slots": 1, "capabilities": [], "wait_ms": 0}).encode()
        leased = json.loads(_read(f"{api}/lease", body))["leases"]
        assert [grant["job"] for grant in leased] == [scheduler.job_id(HEAD_A, item)]
        job, token = leased[0]["job"], leased[0]["token"]
        signed = json.loads(_read(f"{api}/upload-url?job={job}&token={token}&name=report"))
        assert signed == {"url": run.lab.objects.upload_url(upload_path(job, "report"), 0)}
        resume = urllib.request.Request(f"{api}/resume", data=b"", headers={"Authorization": "Bearer tok"})
        with urllib.request.urlopen(resume, timeout=5) as reply:
            assert json.loads(reply.read()) == {"resumed": []}
        text = _read(f"{exposition}/metrics").decode()
        assert 'ci_lab_units_running{class="promotion"} 1.0' in text
        assert 'ci_lab_class_shed{class="backfill"} 1.0' in text
        assert "ci_lab_slots_busy 1.0" in text
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


FLUSHED = "<flushed>"


@dataclass
class Stdout:
    events: list[str] = field(default_factory=list[str])

    def write(self, text: str) -> int:
        self.events.append(text)
        return len(text)

    def flush(self) -> None:
        self.events.append(FLUSHED)


def test_serve_runs_the_given_passes_and_prints_each_action(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=60_000)
    run.push(HEAD_A)
    out = Stdout()
    monkeypatch.setattr(sys, "stdout", out)
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        assert len(slept) <= 2

    assert scheduler.serve(run.engine, 0.25, 2, lambda: 2.5, sleep) == 0
    assert slept == [0.25, 0.25]
    assert out.events == [f"received {HEAD_A} on feature", "\n", FLUSHED]
    assert json.loads(run.lab.client.hget(scheduler.WAITING, f"{REPO}|feature|3"))["since"] == 2500


def test_serve_without_a_pass_count_runs_until_stopped(lab: Callable[..., Run]) -> None:
    run = lab({})
    passes: list[float] = []

    def sleep(seconds: float) -> None:
        passes.append(seconds)
        if len(passes) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        scheduler.serve(run.engine, 1.0, 0, lambda: 1.0, sleep)
    assert passes == [1.0, 1.0, 1.0]


@dataclass
class Delivery:
    event: Envelope
    attempt: int = 1
    acked: bool = False
    termed: bool = False

    def ack(self) -> None:
        self.acked = True

    def nak(self, delay_ms: int = 0) -> None:
        raise AssertionError(delay_ms)

    def term(self) -> None:
        self.termed = True


@dataclass
class Consumer:
    deliveries: list[bus.Delivery]
    asked: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])

    def fetch(self, batch: int, timeout_ms: int) -> list[bus.Delivery]:
        self.asked.append((batch, timeout_ms))
        return self.deliveries


def test_the_bus_inbox_reads_each_head_with_its_branch_as_lane_and_acknowledges_it() -> None:
    branched = HeadEvent("push", REPO, HEAD_A, BASE, ACTOR, "pushed")
    bare = HeadEvent("pull_request", REPO, HEAD_B, BASE, ACTOR, "opened")
    first = envelope(branched)
    first = replace(first, data={**first.data, "branch": "dev"})
    deliveries = [Delivery(first), Delivery(envelope(bare))]
    consumer = Consumer([*deliveries])
    inbox = scheduler.BusInbox(consumer)
    assert inbox.heads() == [scheduler.Head(branched, "dev"), scheduler.Head(bare, HEAD_B)]
    assert [delivery.acked for delivery in deliveries] == [True, True]
    assert [delivery.termed for delivery in deliveries] == [False, False]
    assert consumer.asked == [(scheduler.BATCH, scheduler.FETCH_MS)]


def _without(event: HeadEvent, *names: str) -> Envelope:
    full = envelope(event)
    return replace(full, data={name: value for name, value in full.data.items() if name not in names})


@pytest.mark.feature("lab-head-missing-field-terminate")
def test_the_bus_inbox_terminates_each_head_missing_a_field_with_a_logged_reason_and_reads_the_next_head(
    caplog: pytest.LogCaptureFixture,
) -> None:
    one = _without(HeadEvent("push", REPO, HEAD_A, BASE, ACTOR, "pushed"), "action")
    two = _without(HeadEvent("push", REPO, HEAD_A, BASE, ACTOR, "forced"), "actor", "action")
    bare = HeadEvent("pull_request", REPO, HEAD_B, BASE, ACTOR, "opened")
    deliveries = [Delivery(one), Delivery(two), Delivery(envelope(bare))]
    inbox = scheduler.BusInbox(Consumer([*deliveries]))
    with caplog.at_level(logging.WARNING, logger=scheduler.__name__):
        assert inbox.heads() == [scheduler.Head(bare, HEAD_B)]
    assert [delivery.termed for delivery in deliveries] == [True, True, False]
    assert [delivery.acked for delivery in deliveries] == [False, False, True]
    assert caplog.record_tuples == [
        (scheduler.__name__, logging.WARNING, f"terminated lab head {one.id} missing action"),
        (scheduler.__name__, logging.WARNING, f"terminated lab head {two.id} missing actor, action"),
    ]


def _commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
        _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "change")
    return _git(repo, "rev-parse", "HEAD").strip()


def _blob(repo: Path, sha: str, path: str) -> str:
    return _git(repo, "rev-parse", f"{sha}:{path}").strip()


ACTORS = {"actors": ["nestorcolt", "agenticore-app[bot]", "tcc-main-ci[bot]"], "queue_actor": "github-merge-queue[bot]"}
MUTATION = {"run_on": "ready", "mutants_per_changed_line": "all", "arid": []}
DELIVERY = {"lab": {"authoritative": [], "mutation": MUTATION, "check_prefix": "lab", "retry": {"lost": 2}}}


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "origin"
    _git(tmp_path, "init", "-q", "-b", "dev", str(repo))
    base = _commit(
        repo,
        {
            ".github/workflows/test.yml": "jobs: {}\n",
            ".github/input-keys.json": '{"gates": {}}',
            ".github/workflows/trusted-actors.json": json.dumps(ACTORS),
            "delivery.yaml": json.dumps(DELIVERY),
            "uv.lock": "lock",
            "pyproject.toml": "[project]\n",
            "src/a.py": "a = 1\n",
            "srcx/b.py": "b = 1\n",
            "docs/c.md": "c\n",
        },
    )
    head = _commit(repo, {"src/a.py": "a = 2\n", "uv.lock": "lock 2", "docs/tab\tname.md": "t\n"})
    return repo, base, head


def _gate(name: str, job: str, paths: tuple[str, ...], base_paths: tuple[str, ...]) -> StaticGate:
    return StaticGate(name, job, name, (), (), "", paths, base_paths)


def _no_tests(repo: str, commits: Sequence[str]) -> dict[str, Any]:
    return {"units": [], "tests": {}}


def _unknown(kind: str, key: str) -> Verdict | None:
    return None


def test_the_mirror_expander_keys_static_gates_from_git_objects_without_a_checkout(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = origin
    read: list[tuple[str, Any]] = []
    gates = [
        _gate("size", "lint", ("src",), ("src/a.py",)),
        _gate("format", "lint", ("src/",), ()),
        _gate("semgrep", "semgrep", (".",), ("docs/c.md", "sr")),
    ]

    def discover(workflow: str, registry: Any) -> list[StaticGate]:
        read.append((workflow, registry))
        return gates

    monkeypatch.setattr(scheduler, "discover", discover)
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    root = tmp_path / "mirrors" / "stores"
    expander = scheduler.MirrorExpander(node, root, toolchain, _no_tests, _unknown)
    remote = f"file://{repo}"
    request = GradeRequest(remote, head, base, Priority.PULL_REQUEST, ACTOR, False)
    found = expander.expand(request)
    assert [store.name for store in root.iterdir()] == [str(repo).removeprefix("/").replace("/", "--")]
    chain = Toolchain("3.12", {}, _blob(repo, head, "uv.lock"), _blob(repo, head, "pyproject.toml"), "image", {})
    paths = filter(None, _git(repo, "ls-tree", "-r", "-z", "--name-only", head).split("\0"))
    head_files = {path: _blob(repo, head, path) for path in paths}
    assert "docs/tab\tname.md" in head_files
    a_at_head = {"src/a.py": _blob(repo, head, "src/a.py")}
    a_at_base = {"src/a.py": _blob(repo, base, "src/a.py")}
    expected = {
        "lint": (
            Unit("static", static_key(StaticInputs("size", a_at_head, a_at_base, base, chain)), ("size",)),
            Unit("static", static_key(StaticInputs("format", a_at_head, {}, base, chain)), ("format",)),
        ),
        "semgrep": (
            Unit(
                "static",
                static_key(
                    StaticInputs("semgrep", head_files, {"docs/c.md": _blob(repo, base, "docs/c.md")}, base, chain)
                ),
                ("semgrep",),
            ),
        ),
    }
    tree = _git(repo, "rev-parse", f"{head}^{{tree}}").strip()
    assert found == scheduler.frame(Priority.PULL_REQUEST, tree, base, expected)
    assert read == [("jobs: {}\n", {"gates": {}})]
    promoted = expander.expand(GradeRequest(remote, head, base, Priority.PROMOTION, ACTOR, False))
    assert (promoted.tree, promoted.grader, set(promoted.gates), promoted.skipped) == (
        tree,
        head,
        {"lint", "semgrep", "unit"},
        frozenset(),
    )


def test_the_mirror_expander_trusts_the_runner_trust_list_on_dev(tmp_path: Path, origin: tuple[Path, str, str]) -> None:
    repo, _, _ = origin
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    expander = scheduler.MirrorExpander(
        node, tmp_path / "stores", Toolchain("3.12", {}, "", "", "", {}), _no_tests, _unknown
    )
    assert expander.trusted(f"file://{repo}") == frozenset([*ACTORS["actors"], ACTORS["queue_actor"]])
    assert expander.retries(f"file://{repo}") == Policy(lost=2, failed=0)
    _commit(repo, {"delivery.yaml": json.dumps({"lab": {**DELIVERY["lab"], "retry": {"lost": -1}}})})
    with pytest.raises(jsonschema.ValidationError):
        expander.retries(f"file://{repo}")


def _drop(repo: Path, *names: str) -> str:
    _git(repo, "rm", "-q", *names)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "drop")
    return _git(repo, "rev-parse", "HEAD").strip()


def test_a_head_without_a_lockfile_or_pyproject_keys_its_units_with_neither(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, _ = origin
    bare = _drop(repo, "uv.lock", "pyproject.toml")
    monkeypatch.setattr(scheduler, "discover", lambda workflow, registry: [_gate("format", "lint", ("src/",), ())])
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    expander = scheduler.MirrorExpander(
        node, tmp_path / "stores", Toolchain("3.12", {}, "old", "old", "image", {}), _no_tests, _unknown
    )
    found = expander.expand(GradeRequest(f"file://{repo}", bare, base, Priority.PROMOTION, ACTOR, False))
    inputs = StaticInputs(
        "format", {"src/a.py": _blob(repo, bare, "src/a.py")}, {}, bare, Toolchain("3.12", {}, "", "", "image", {})
    )
    assert found.gates == {"lint": (Unit("static", static_key(inputs), ("format",)),), "unit": ()}


def test_the_mirror_expander_fails_on_a_git_read_that_fails(tmp_path: Path, origin: tuple[Path, str, str]) -> None:
    repo, base, _ = origin
    unregistered = _drop(repo, ".github/input-keys.json")
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    expander = scheduler.MirrorExpander(
        node, tmp_path / "stores", Toolchain("3.12", {}, "", "", "", {}), _no_tests, _unknown
    )
    with pytest.raises(subprocess.CalledProcessError):
        expander.expand(GradeRequest(f"file://{repo}", unregistered, base, Priority.PROMOTION, ACTOR, False))
    with pytest.raises(subprocess.CalledProcessError):
        expander.trusted(f"file://{tmp_path / 'missing'}")


ALPHA = "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
FIRST_TEST = "tests/test_alpha.py::test_first"
OTHER_TEST = "tests/test_alpha.py::test_other"
SECOND_TEST = "tests/test_beta.py::test_second"
SUITE_MANIFEST: dict[str, Any] = {
    "version": 2,
    "commit": "dev",
    "environment": "py312",
    "units": ["scripts/alpha.py::first", "scripts/alpha.py::second"],
    "tests": {
        FIRST_TEST: {"units": [0], "files": [], "escapes": False},
        OTHER_TEST: {"units": [0], "files": [], "escapes": False},
        SECOND_TEST: {"units": [1], "files": [], "escapes": False, "outside": ["docs/c.md"]},
    },
    "lines": {},
}
STORED_MANIFEST: dict[str, Any] = {
    **SUITE_MANIFEST,
    "tests": {FIRST_TEST: {"units": [0], "files": [], "escapes": False}},
}
SUITE_DURATIONS = {FIRST_TEST: 40.0, OTHER_TEST: 40.0, SECOND_TEST: 1.0}


@dataclass(frozen=True)
class Suite:
    repo: Path
    seed: str
    base: str
    head: str

    @property
    def remote(self) -> str:
        return f"file://{self.repo}"

    def request(self, tier: Priority, head: str = "") -> GradeRequest:
        return GradeRequest(self.remote, head or self.head, self.base, tier, ACTOR, False)


@dataclass
class Port:
    manifest: dict[str, Any] | None = None
    known: bool = True
    asked: list[tuple[str, list[str]]] = field(default_factory=list[tuple[str, list[str]]])
    looked: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def manifests(self, repo: str, commits: Sequence[str]) -> dict[str, Any] | None:
        self.asked.append((repo, list(commits)))
        return copy.deepcopy(self.manifest)

    def verdicts(self, kind: str, key: str) -> Verdict | None:
        self.looked.append((kind, key))
        return Verdict(kind, key, False, False, 1.0) if self.known else None

    def expander(self, tmp_path: Path) -> scheduler.MirrorExpander:
        node = tmp_path / "node"
        _git(tmp_path, "init", "-q", "--bare", str(node))
        toolchain = Toolchain("3.12", {}, "", "", "image", {})
        return scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, self.manifests, self.verdicts)


def _suite(tmp_path: Path, lab: dict[str, Any]) -> Suite:
    repo = tmp_path / "suite"
    _git(tmp_path, "init", "-q", "-b", "dev", str(repo))
    seed = _commit(
        repo,
        {
            ".github/workflows/test.yml": "jobs: {}\n",
            ".github/input-keys.json": '{"gates": {}}',
            "delivery.yaml": json.dumps({"lab": lab}),
            "uv.lock": "lock",
            "pyproject.toml": "[project]\n",
            ".test_durations": json.dumps(SUITE_DURATIONS),
            "scripts/alpha.py": ALPHA,
            "tests/test_alpha.py": "def test_first():\n    pass\n\n\ndef test_other():\n    pass\n",
            "tests/test_beta.py": "def test_second():\n    pass\n",
        },
    )
    base = _merged(repo, "side", {"docs/c.md": "c\n"})
    _merged(repo, "entry", {"scripts/alpha.py": ALPHA.replace("return 2", "return 3")}, {"docs/p.md": "p\n"})
    head = _commit(repo, {"docs/c.md": "d\n"})
    return Suite(repo, seed, base, head)


def _merged(repo: Path, branch: str, *changes: dict[str, str]) -> str:
    _git(repo, "switch", "-q", "-c", branch)
    for files in changes:
        _commit(repo, files)
    _git(repo, "switch", "-q", "dev")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-m", branch, branch)
    return _git(repo, "rev-parse", "HEAD").strip()


def _suite_units(suite: Suite, head: str, durations: dict[str, float]) -> tuple[Unit, ...]:
    paths = filter(None, _git(suite.repo, "ls-tree", "-r", "-z", "--name-only", head).split("\0"))
    blobs = {path: _blob(suite.repo, head, path) for path in paths}
    shown = ["git", "-C", str(suite.repo), "show"]
    tree = {path: subprocess.run([*shown, f"{head}:{path}"], capture_output=True, check=True).stdout for path in blobs}
    chain = Toolchain("3.12", {}, blobs["uv.lock"], blobs["pyproject.toml"], "image", {})
    return expand_tests.units(SUITE_MANIFEST, tree, blobs, durations, chain)


def _tree(suite: Suite, head: str) -> str:
    return _git(suite.repo, "rev-parse", f"{head}^{{tree}}").strip()


@pytest.fixture
def staticless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "discover", lambda workflow, registry: [])


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize("tier", [Priority.PULL_REQUEST, Priority.HOTFIX])
def test_a_pull_request_or_hotfix_head_expands_no_test_units(tmp_path: Path, tier: Priority) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    port = Port()
    found = port.expander(tmp_path).expand(suite.request(tier))
    assert found == scheduler.frame(tier, _tree(suite, suite.head), suite.base, {})
    assert (port.asked, port.looked) == ([], [])


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize("queue", [{}, {"queue": {"full_suite_entries": 2}}])
def test_a_merge_group_head_runs_only_the_batches_its_entries_invalidate(tmp_path: Path, queue: dict[str, Any]) -> None:
    suite = _suite(tmp_path, {**DELIVERY["lab"], **queue})
    port = Port(SUITE_MANIFEST)
    found = port.expander(tmp_path).expand(suite.request(Priority.QUEUE))
    every = _suite_units(suite, suite.head, SUITE_DURATIONS)
    assert [batch.members for batch in every] == [(FIRST_TEST,), (OTHER_TEST,), (SECOND_TEST,)]
    assert found == scheduler.frame(Priority.QUEUE, _tree(suite, suite.head), suite.base, {"unit": every[2:]})
    assert port.asked == [(suite.remote, [suite.base, suite.seed])]
    assert port.looked == [(batch.kind, batch.key) for batch in every]


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.usefixtures("staticless")
def test_a_merge_group_head_whose_entries_invalidate_no_test_runs_no_batch(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    _git(suite.repo, "switch", "-q", "-c", "docs", suite.base)
    head = _commit(suite.repo, {"docs/q.md": "q\n"})
    port = Port(SUITE_MANIFEST)
    found = port.expander(tmp_path).expand(suite.request(Priority.QUEUE, head))
    assert found == scheduler.frame(Priority.QUEUE, _tree(suite, head), suite.base, {"unit": ()})
    assert port.asked == [(suite.remote, [suite.base, suite.seed])]
    assert port.looked == [(batch.kind, batch.key) for batch in _suite_units(suite, head, SUITE_DURATIONS)]


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize(
    ("queue", "known"), [({}, False), ({"queue": {"full_suite_entries": 1}}, True)], ids=["cache", "entries"]
)
def test_a_merge_group_head_runs_every_batch_when_an_interaction_clause_holds(
    tmp_path: Path, queue: dict[str, Any], known: bool
) -> None:
    suite = _suite(tmp_path, {**DELIVERY["lab"], **queue})
    port = Port(SUITE_MANIFEST, known)
    found = port.expander(tmp_path).expand(suite.request(Priority.QUEUE))
    every = _suite_units(suite, suite.head, SUITE_DURATIONS)
    assert found == scheduler.frame(Priority.QUEUE, _tree(suite, suite.head), suite.base, {"unit": every})


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize("tier", [Priority.PROMOTION, Priority.BACKFILL])
def test_a_promotion_or_backfill_head_runs_every_batch_without_reading_the_cache(
    tmp_path: Path, tier: Priority
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    port = Port(SUITE_MANIFEST)
    found = port.expander(tmp_path).expand(suite.request(tier))
    every = _suite_units(suite, suite.head, SUITE_DURATIONS)
    assert found == scheduler.frame(tier, _tree(suite, suite.head), suite.head, {"unit": every})
    assert (port.asked, port.looked) == ([(suite.remote, [suite.base, suite.seed])], [])


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.usefixtures("staticless")
def test_a_head_without_stored_durations_keeps_each_test_file_whole(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    head = _drop(suite.repo, ".test_durations")
    found = Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(Priority.PROMOTION, head))
    every = _suite_units(suite, head, {})
    assert [batch.members for batch in every] == [(FIRST_TEST, OTHER_TEST), (SECOND_TEST,)]
    assert found.gates == {"unit": every}


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize("tier", [Priority.QUEUE, Priority.PROMOTION])
def test_a_head_that_needs_test_units_without_a_manifest_cannot_expand(tmp_path: Path, tier: Priority) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    with pytest.raises(ValueError, match=f"^no dependency manifest at or before {suite.base}$"):
        Port().expander(tmp_path).expand(suite.request(tier))


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.usefixtures("staticless")
def test_a_merge_group_head_refuses_an_entry_limit_the_delivery_schema_refuses(tmp_path: Path) -> None:
    suite = _suite(tmp_path, {**DELIVERY["lab"], "queue": {"full_suite_entries": 0}})
    with pytest.raises(jsonschema.ValidationError, match="0 is less than the minimum of 1"):
        Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(Priority.QUEUE))


@pytest.mark.feature("lab-scheduler-test-units")
def test_stored_manifests_read_the_packed_manifest_of_the_nearest_stored_ancestor(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path / "objects")
    for commit in ("older", "newer"):
        packed = dep_manifest.pack({**STORED_MANIFEST, "commit": commit})
        objects.write_once(f"{manifest_folder(SLUG)}{commit}.json", json.dumps(packed).encode())
    objects.write_once(f"{manifest_folder('other/repo')}head.json", b"{}")
    stored = scheduler.StoredManifests(objects)
    assert stored(REPO, ["head", "newer", "older"]) == {**STORED_MANIFEST, "commit": "newer"}
    assert stored(REPO, ["head", "older"]) == {**STORED_MANIFEST, "commit": "older"}
    assert stored(REPO, ["head"]) is None
    assert manifest_folder(SLUG) == "lab/manifests/the-cloud-clockwork--agentihooks/"


def _batch_keys(manifest: dict[str, Any] | None, note: bytes) -> dict[tuple[str, ...], str]:
    assert manifest is not None
    tree = {"scripts/alpha.py": ALPHA.encode(), "docs/p.md": note}
    blobs = {path: content.hex() for path, content in tree.items()}
    chain = Toolchain("3.12", {}, "", "", "image", {})
    return {batch.members: batch.key for batch in expand_tests.units(manifest, tree, blobs, {}, chain)}


@pytest.mark.feature("lab-verdict-outside-to-manifest")
def test_a_test_verdict_outside_list_reaches_the_stored_records_and_the_following_key(
    lab: Callable[..., Run],
) -> None:
    members = (OTHER_TEST, SECOND_TEST, "tests/test_gone.py::test_gone")
    first, again, plain = (
        Unit(expand_tests.KIND, key(expand_tests.KIND, number), tests)
        for number, tests in ((1, members), (2, members), (3, (FIRST_TEST,)))
    )
    run = lab({HEAD_A: plan({"unit": (first, again, plain)})})
    folder = manifest_folder(SLUG)
    packed = dep_manifest.pack(copy.deepcopy(SUITE_MANIFEST))
    run.lab.objects.write_once(f"{folder}dev.json", json.dumps(packed).encode())
    stored = scheduler.StoredManifests(run.lab.objects)
    before = stored(REPO, ["dev"])
    run.push(HEAD_A)
    run.tick(1000)
    grants = {grant.unit: grant for grant in run.lease(slots=3)}
    run.finish(grants[first], "passed", {"seconds": 1.0, "outside": ["docs/p.md", "docs/c.md"]})
    run.finish(grants[again], "failed", {"seconds": 1.0, "outside": ["docs/c.md", "docs/p.md"]})
    run.finish(grants[plain], "passed", {"seconds": 1.0, "outside": []})
    run.tick(1100)
    after = stored(REPO, ["dev"])
    records = SUITE_MANIFEST["tests"]
    both = ["docs/c.md", "docs/p.md"]
    assert before == SUITE_MANIFEST
    assert after == {
        **SUITE_MANIFEST,
        "tests": {
            FIRST_TEST: records[FIRST_TEST],
            OTHER_TEST: {**records[OTHER_TEST], "outside": both},
            SECOND_TEST: {**records[SECOND_TEST], "outside": both},
        },
    }
    assert [path.removeprefix(folder).partition("/")[0] for path in run.lab.objects.keys(folder)] == [
        "dev.json",
        "outside",
    ]
    assert _batch_keys(before, b"p\n") == _batch_keys(before, b"q\n")
    assert _batch_keys(after, b"p\n")[(FIRST_TEST, OTHER_TEST)] != _batch_keys(after, b"q\n")[(FIRST_TEST, OTHER_TEST)]


@pytest.mark.feature("lab-control-routes")
def test_the_hotfix_port_reads_the_heads_the_control_routes_marked() -> None:
    client = fake_redis()
    now = [1000]
    port = control.Priorities(client, lambda: now[0])
    assert port() == frozenset()
    control.prioritize(client, ["fix-branch", "c0ffee"], 1000)
    assert port() == frozenset({"fix-branch", "c0ffee"})
    now[0] = 1000 + control.PRIORITY_MS
    assert port() == frozenset()


@dataclass
class FakeChecks:
    repo: str


def test_build_wires_the_service_ports_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler.MirrorExpander, "trusted", lambda self, repo: frozenset({repo}))
    monkeypatch.setattr(scheduler.MirrorExpander, "retries", lambda self, repo: Policy(lost=len(repo)))
    monkeypatch.setattr(scheduler, "Checks", FakeChecks)
    environ = {
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: "repository",
        scheduler.IMAGE: "image",
    }
    client = fake_redis()
    consumer = Consumer([])
    objects = LocalObjects(tmp_path / "objects")
    built = scheduler.build(environ, client, objects, consumer)
    assert built.objects is objects
    assert built.settings.trusted == frozenset({"repository"})
    assert built.settings.retries == Policy(lost=len("repository"))
    assert built.ports.hotfixes() == frozenset()
    assert isinstance(built.ports.inbox, scheduler.BusInbox) and built.ports.inbox.consumer is consumer
    expander = built.ports.expander
    assert isinstance(expander, scheduler.MirrorExpander)
    assert (expander.node, expander.root) == (tmp_path / "node", tmp_path / "stores")
    assert expander.toolchain == Toolchain(sys.version.split()[0], {}, "", "", "image", {})
    assert isinstance(expander.manifests, scheduler.StoredManifests) and expander.manifests.objects is objects
    stored = Verdict("static", key("static", 9), True, False, 1.0)
    Store(client, objects).put(stored, {}, b"")
    assert expander.verdicts("static", stored.key) == stored
    imageless = {name: value for name, value in environ.items() if name != scheduler.IMAGE}
    bare = scheduler.build(imageless, client, objects, consumer).ports.expander
    assert isinstance(bare, scheduler.MirrorExpander) and bare.toolchain.image == ""
    first = built.ports.checks(SLUG)
    assert first == FakeChecks(SLUG) and built.ports.checks(SLUG) is first
    assert built.ports.checks("other/repo") == FakeChecks("other/repo")
    hotfixes = built.ports.hotfixes
    assert isinstance(hotfixes, control.Priorities)
    assert (hotfixes.client, hotfixes.clock) == (client, scheduler._now_ms)


@pytest.mark.feature("lab-scheduler-service")
def test_main_reads_the_runtime_config_serves_both_ports_and_runs_the_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import boto3  # pyright: ignore[reportMissingTypeStubs]
    import redis

    from scripts.events import config, factory

    calls: dict[str, Any] = {"served": 0}
    client = fake_redis()

    def from_url(url: str, **options: Any) -> Any:
        calls["redis"] = (url, options)
        return client

    def s3(name: str, **options: Any) -> str:
        calls["s3"] = (name, options)
        return "s3 client"

    class Events:
        def consumer(self, durable: str, subject: str) -> str:
            calls["consumer"] = (durable, subject)
            return "consumer"

    class Bus:
        lab_heads = Events()

    def build(environ: Any, redis_client: Any, objects: Any, consumer: Any) -> scheduler.Scheduler:
        calls["build"] = (environ, redis_client, objects, consumer)
        ports = scheduler.Ports(Inbox(), Expander({}), lambda repo: Poster(), frozenset[str])
        return scheduler.Scheduler(redis_client, objects, ports, scheduler.Settings(frozenset()))

    class Server:
        def serve_forever(self) -> None:
            calls["served"] += 1

    def listen(
        found: Any,
        leases: Leases,
        routes: lease_api.ControlRoutes,
        spending: Any,
        clock: Any,
        ports: tuple[int, int],
    ) -> list[Server]:
        calls["listen"] = (found, leases, routes, spending, clock, ports)
        return [Server(), Server()]

    class Thread:
        def __init__(self, target: Callable[[], None], daemon: bool) -> None:
            calls["thread"] = daemon
            self.target = target

        def start(self) -> None:
            self.target()

    class Clock:
        slept: list[float] = []

        @staticmethod
        def time() -> float:
            return 1000.25

        @staticmethod
        def sleep(seconds: float) -> None:
            Clock.slept.append(seconds)

    def serve(found: scheduler.Scheduler, interval: float, passes: int, clock: Any, sleep: Any) -> int:
        calls["serve"] = (found, interval, passes, clock, sleep)
        return 7

    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        yaml.safe_dump(
            {
                "classes": list(CLASSES),
                "budget": {"coreMinutesPerDay": 600, "shed": {"backfill": 0.5}},
                "backfill": {"auditShare": 0.1, "moduleSweeps": False},
            }
        )
    )
    environ = {
        scheduler.contract.REDIS_URL: "redis://lab",
        scheduler.contract.RUNTIME_CONFIG: str(runtime),
        scheduler.MINIO_ENDPOINT: "endpoint",
        scheduler.MINIO_ACCESS_KEY: "access",
        scheduler.MINIO_SECRET_KEY: "secret",
        scheduler.MINIO_BUCKET: "bucket",
        scheduler.contract.CONTROL_TOKEN: "tok",
    }
    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    monkeypatch.setattr(boto3, "client", s3)
    provisioning = config.BusConfig(backend="nats", url="nats://lab")
    bound = config.BusConfig(backend="nats", url="nats://lab", provision=False)
    monkeypatch.setattr(
        config.BusConfig, "from_environ", classmethod(lambda cls, found: provisioning if found is environ else None)
    )
    monkeypatch.setattr(factory, "open_bus", lambda found: Bus() if found == bound else None)
    monkeypatch.setattr(scheduler, "build", build)
    monkeypatch.setattr(scheduler, "listen", listen)
    monkeypatch.setattr(scheduler, "Thread", Thread)
    monkeypatch.setattr(scheduler, "time", Clock)
    monkeypatch.setattr(scheduler, "serve", serve)
    assert scheduler.main(["--interval", "0.5", "--passes", "3"], environ) == 7
    assert calls["redis"] == ("redis://lab", {"decode_responses": True})
    options = {"endpoint_url": "endpoint", "aws_access_key_id": "access", "aws_secret_access_key": "secret"}
    assert calls["s3"] == ("s3", options)
    built_from, redis_client, objects, consumer = calls["build"]
    assert (built_from, redis_client, consumer) == (environ, client, "consumer")
    assert (objects.client, objects.bucket, objects.prefix) == ("s3 client", "bucket", "")
    assert calls["consumer"] == (scheduler.DURABLE, "ci.lab.head.>")
    found, leases, routes, spending, clock, ports = calls["listen"]
    assert found.queue.client is client and leases.queue is found.queue
    assert routes == lease_api.ControlRoutes(client, "tok", scheduler._now_ms)
    assert (leases.ttl_ms, leases.poll_ms) == (scheduler.LEASE_TTL_MS, scheduler.LEASE_POLL_MS)
    assert leases.clock() == 1_000_250 and clock() == 1_000_250
    leases.sleep(1500)
    assert Clock.slept == [1.5]
    assert spending.client is client and spending.clock() == 1_000_250
    assert spending.policy == budget.Policy(600, {"backfill": 0.5})
    assert ports == (8800, 9108)
    assert calls["thread"] is True and calls["served"] == 2
    found, interval, passes, clock, sleep = calls["serve"]
    assert (found.queue.client, interval, passes, clock, sleep) == (client, 0.5, 3, Clock.time, Clock.sleep)
    assert scheduler.main([], environ) == 7
    assert calls["serve"][1:3] == (scheduler.INTERVAL_S, 0)
    del environ[scheduler.contract.CONTROL_TOKEN]
    assert scheduler.main([], environ) == 7
    assert calls["listen"][2].token == ""
    calls.clear()
    runtime.write_text(yaml.safe_dump({"classes": []}))
    with pytest.raises(delivery.DeliveryError):
        scheduler.main([], environ)
    assert calls == {}


@pytest.mark.feature("lab-scheduler-reindex-on-start")
@pytest.mark.parametrize(
    ("setting", "printed"),
    [
        ({"CI_LAB_REINDEX_ON_START": "true"}, "reindexed 1 verdicts\n"),
        ({"CI_LAB_REINDEX_ON_START": "false"}, ""),
        ({}, ""),
    ],
)
def test_main_rebuilds_the_verdict_index_before_serving_only_when_reindex_on_start_is_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: dict[str, str], printed: str
) -> None:
    import boto3  # pyright: ignore[reportMissingTypeStubs]
    import redis

    from scripts.events import config, factory

    client = fake_redis()
    objects = LocalObjects(tmp_path / "objects")
    verdict = Verdict(kind="test", key=key("test", 1), passed=True, flaky=False, seconds=1.0)
    Store(client, objects).put(verdict, {}, b"ok\n")
    client.flushall()
    served: list[scheduler.Scheduler] = []

    class Stdout(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flushed: list[str] = []

        def flush(self) -> None:
            self.flushed.append(self.getvalue())
            super().flush()

    class Events:
        def consumer(self, durable: str, subject: str) -> str:
            return f"{durable} {subject}"

    class Bus:
        lab_heads = Events()

    def from_url(_url: str, **_options: Any) -> Any:
        return client

    def s3(name: str, **_options: Any) -> str:
        return name

    def from_environ(_cls: type[Any], _found: Any) -> config.BusConfig:
        return config.BusConfig(backend="nats", url="nats://lab")

    def open_bus(_found: Any) -> Bus:
        return Bus()

    def poster(_repo: str) -> Poster:
        return Poster()

    def build(_environ: Any, redis_client: Any, _objects: Any, _consumer: Any) -> scheduler.Scheduler:
        ports = scheduler.Ports(Inbox(), Expander({}), poster, frozenset[str])
        return scheduler.Scheduler(redis_client, objects, ports, scheduler.Settings(frozenset()))

    def listen(*_arguments: Any) -> list[Any]:
        return []

    def serve(found: scheduler.Scheduler, *_arguments: Any) -> int:
        served.append(found)
        return 0

    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        yaml.safe_dump(
            {
                "classes": list(CLASSES),
                "budget": {"coreMinutesPerDay": 600, "shed": {"backfill": 0.5}},
                "backfill": {"auditShare": 0.1, "moduleSweeps": False},
            }
        )
    )
    environ = {
        scheduler.contract.REDIS_URL: "redis://lab",
        scheduler.contract.RUNTIME_CONFIG: str(runtime),
        scheduler.MINIO_ENDPOINT: "endpoint",
        scheduler.MINIO_ACCESS_KEY: "access",
        scheduler.MINIO_SECRET_KEY: "secret",
        scheduler.MINIO_BUCKET: "bucket",
    } | setting
    stdout = Stdout()
    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    monkeypatch.setattr(boto3, "client", s3)
    monkeypatch.setattr(config.BusConfig, "from_environ", classmethod(from_environ))
    monkeypatch.setattr(factory, "open_bus", open_bus)
    monkeypatch.setattr(scheduler, "build", build)
    monkeypatch.setattr(scheduler, "listen", listen)
    monkeypatch.setattr(scheduler, "serve", serve)
    monkeypatch.setattr(sys, "stdout", stdout)

    assert scheduler.main([], environ) == 0

    assert stdout.getvalue() == printed
    assert stdout.flushed == ([printed] if printed else [])
    assert served[0].store.get("test", verdict.key) == (verdict if printed else None)


def test_the_service_names_its_command_in_its_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        scheduler.main(["--help"], {})
    assert capsys.readouterr().out.startswith("usage: python -m scripts.ci_lab scheduler [-h]")


def test_the_cli_runs_the_scheduler_service_and_keeps_the_self_test(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[list[str], Any]] = []

    def main(argv: list[str], environ: Any) -> int:
        seen.append((list(argv), environ))
        return 5

    monkeypatch.setattr(scheduler, "main", main)
    assert cli.main(["scheduler", "--passes", "1"], {"A": "1"}) == 5
    assert seen == [(["--passes", "1"], {"A": "1"})]
    assert cli.main(["scheduler", "--self-test"], {}) == 0
    assert json.loads(capsys.readouterr().out)["service"] == "scheduler"
    assert len(seen) == 1
