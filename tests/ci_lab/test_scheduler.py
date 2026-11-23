import copy
import io
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import jsonschema
import pytest
import requests
import yaml

from scripts import ci_wiring, dep_manifest
from scripts.ci_lab import __main__ as cli
from scripts.ci_lab import (
    budget,
    checks,
    control,
    delivery,
    evidence,
    executors,
    expand_mutation,
    expand_tests,
    interaction,
    lease_api,
    manifests,
    metrics,
    queue,
    relay,
    scheduler,
    static_gates,
    worktrees,
)
from scripts.ci_lab.capacity import Load
from scripts.ci_lab.checks import Output
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.digests import GradeInputs, StaticInputs, Toolchain, digest, grade_key, static_key
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.leases import Grant, Leases
from scripts.ci_lab.manifests import Actions, Fetcher, FetchRefused, FetchUnanswered
from scripts.ci_lab.objects import LocalObjects, flakes_path, manifest_folder, unit_path, upload_path
from scripts.ci_lab.outcomes import CRASH
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.static_gates import StaticGate
from scripts.ci_lab.store import Store, Verdict
from scripts.ci_lab.units import Priority, Unit
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.ci_mutation.plan import MUTANT_SECONDS
from scripts.dep_manifest import _git
from scripts.events import bus
from scripts.events.envelope import Envelope
from tests.ci_lab.gate_required_cases import load, materialize

REPO = "/the-cloud-clockwork/agentihooks"
SLUG = "the-cloud-clockwork/agentihooks"
ACTOR = "nestorcolt"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
BASE = "c" * 40
TREE = "d" * 40
OTHER_TREE = "e" * 40
LOCK = ".github/test-requirements.lock"
APP_SETTINGS = {"GITHUB_APP_ID": "1", "GITHUB_APP_INSTALLATION_ID": "2", "GITHUB_APP_PRIVATE_KEY": "fixture"}

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

    def merge_base(self, repo: str, head: str) -> str:
        return self.plans[head].grader

    def reachable(self, repo: str, head: str) -> bool:
        return True

    def groups(self, repo: str) -> frozenset[str]:
        return frozenset(self.plans)


@dataclass
class Poster:
    posts: list[Post] = field(default_factory=list[Post])

    def seed(self, head: str, gate: str, run: str, run_id: int, status: str, shadow: bool) -> None:
        pass

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        self.posts.append((head, gate, status, conclusion, output, shadow))
        return len(self.posts)

    def moves(self, head: str, gate: str) -> list[tuple[str, str | None]]:
        return [(post[2], post[3]) for post in self.posts if post[0] == head and post[1] == gate]


class FakeKube:
    def __init__(self) -> None:
        self.patches: list[tuple[str, bool]] = []

    def __call__(self, pod: str, busy: bool) -> bool:
        self.patches.append((pod, busy))
        return True


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
    kube: FakeKube = field(default_factory=FakeKube)
    now: int = 0

    def checks(self, repo: str) -> Poster:
        self.repos.append(repo)
        return self.poster

    def hotfixes(self) -> frozenset[str]:
        self.asked.append(self.now)
        return frozenset(self.fixes)

    def build(self, quiet_ms: int, retries: Policy, spending: budget.Budget | None) -> scheduler.Scheduler:
        ports = scheduler.Ports(self.inbox, self.expander, self.checks, self.hotfixes, self.kube)
        settings = scheduler.Settings(frozenset({ACTOR}), quiet_ms, retries)
        return scheduler.Scheduler(self.client, self.objects, ports, settings, spending)


@dataclass
class Run:
    lab: Lab
    engine: scheduler.Scheduler

    @property
    def store(self) -> Store:
        return self.engine.store

    def push(
        self,
        head: str,
        lane: str = "feature",
        event: str = "push",
        action: str = "pushed",
        actor: str = ACTOR,
        ref: str = "",
    ) -> None:
        self.lab.inbox.pending.append(scheduler.Head(HeadEvent(event, REPO, head, BASE, actor, action), lane, ref))

    def tick(self, at: int) -> list[str]:
        self.lab.now = at
        return self.engine.tick(at)

    def leases(self, ttl_ms: int = 1000) -> Leases:
        manifests = partial(scheduler.stored_manifest, self.lab.objects)
        return Leases(self.engine.queue, ttl_ms, lambda: self.lab.now, lambda _: None, 1, self.lab.kube, manifests)

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
    def make(
        plans: dict[str, scheduler.Plan],
        quiet_ms: int = 0,
        retries: Policy = Policy(),
        spend: budget.Policy | None = None,
    ) -> Run:
        client = fake_redis()
        state = Lab(client, LocalObjects(tmp_path / "objects"), Inbox(), Expander(plans))
        spending = None if spend is None else budget.Budget(client, lambda: state.now, spend)
        return Run(state, state.build(quiet_ms, retries, spending))

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
    assert f"grading {grade.run} with 0 of 2 units held" in actions
    assert f"completed {grade.run} success" in actions
    assert run.lab.repos == [SLUG]


@pytest.mark.feature("lab-remote-heads")
def test_a_grade_ref_head_posts_no_check_run_and_keeps_each_check_s_conclusion_for_the_relay(
    lab: Callable[..., Run],
) -> None:
    units = (unit(1), unit(2))
    run = lab({HEAD_A: plan({"lint": units}), HEAD_B: plan({"lint": units})})
    for item in units:
        run.cache(item)
    run.push(HEAD_A, lane="engineer-1", ref="refs/grade/t1")
    run.push(HEAD_B, lane="engineer-2")
    actions = run.tick(1000)
    relayed = run.grade(HEAD_A)
    assert relayed.ref == "refs/grade/t1"
    assert relayed.conclusion == "success"
    assert relayed.posted == {"lint": ["completed", "success"], scheduler.REQUIRED: ["completed", "success"]}
    assert relayed.runs == {}
    assert run.lab.poster.moves(HEAD_A, "lint") == []
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED) == []
    assert f"left lint completed on {HEAD_A} to the relay" in actions
    assert f"left {scheduler.REQUIRED} completed on {HEAD_A} to the relay" in actions
    assert run.grade(HEAD_B).ref == ""
    assert run.lab.poster.moves(HEAD_B, scheduler.REQUIRED) == [("completed", "success")]


@pytest.mark.feature("lab-lease-event-branch")
def test_a_grant_carries_the_event_of_its_grade_class_and_the_pull_request_head_branch(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    pulled = HeadEvent("pull_request", REPO, HEAD_A, BASE, ACTOR, "opened", "fix-x")
    run.lab.inbox.pending.append(scheduler.Head(pulled, HEAD_A))
    run.push(HEAD_B)
    run.tick(1000)
    grants = {grant.head: grant for grant in run.lease(slots=2)}
    assert (grants[HEAD_A].event, grants[HEAD_A].head_branch) == ("pull_request", "fix-x")
    assert (grants[HEAD_B].event, grants[HEAD_B].head_branch) == ("push", "")
    assert run.grade(HEAD_A).head_branch == "fix-x"


@pytest.mark.feature("lab-lease-event-branch")
def test_a_request_held_before_the_head_branch_field_grades_with_no_head_branch(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    tier = int(Priority.PULL_REQUEST)
    request = {"repo": REPO, "head": HEAD_A, "base": BASE, "tier": tier, "actor": ACTOR, "bypass_negative": False}
    held = {"request": request, "lane": HEAD_A, "ref": "", "since": 0}
    run.lab.client.hset(scheduler.WAITING, f"{REPO}|{HEAD_A}|{tier}", json.dumps(held))
    run.tick(1000)
    [grant] = run.lease()
    assert (grant.event, grant.head_branch) == ("pull_request", "")


@pytest.mark.feature("lab-remote-heads")
def test_a_grade_ref_head_held_in_its_quiet_window_keeps_its_ref(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=500)
    run.cache(unit(1))
    run.push(HEAD_A, lane="engineer-1", ref="refs/grade/t1")
    assert run.tick(1000) == [f"received {HEAD_A} on engineer-1"]
    run.tick(1600)
    assert run.grade(HEAD_A).ref == "refs/grade/t1"
    assert run.lab.poster.posts == []


@pytest.mark.feature("lab-remote-heads")
def test_a_held_head_takes_the_grade_ref_it_arrives_again_with_and_keeps_one_it_holds(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(1),)})}, quiet_ms=500)
    run.cache(unit(1))
    run.push(HEAD_A, lane="engineer-1")
    run.push(HEAD_B, lane="engineer-2", ref="refs/grade/t2")
    run.tick(1000)
    run.push(HEAD_A, lane="engineer-1", ref="refs/grade/t1")
    run.push(HEAD_B, lane="engineer-2")
    assert run.tick(1200) == [f"holding {HEAD_A}", f"holding {HEAD_B}"]
    run.tick(1600)
    assert (run.grade(HEAD_A).ref, run.grade(HEAD_B).ref) == ("refs/grade/t1", "refs/grade/t2")
    assert run.lab.poster.posts == []


@pytest.mark.feature("lab-remote-heads")
def test_a_head_held_before_grade_refs_were_kept_starts_with_no_ref_and_posts(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=500)
    run.cache(unit(1))
    run.push(HEAD_A, lane="engineer-1")
    run.tick(1000)
    for name, raw in run.lab.client.hgetall(scheduler.WAITING).items():
        older = {key: value for key, value in json.loads(raw).items() if key != "ref"}
        run.lab.client.hset(scheduler.WAITING, name, json.dumps(older))
    run.tick(1600)
    assert run.grade(HEAD_A).ref == ""
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED) == [("completed", "success")]


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


CHECK_RUNS = f"{checks.API}/repos/{SLUG}/check-runs"


class FixtureToken:
    def get_token(self) -> str:
        return "fixture"


def github(calls: list[tuple[str, str]]) -> Callable[..., Mock]:
    def request(method: str, url: str, **_: Any) -> Mock:
        calls.append((method, url))
        created = sum(1 for sent, _ in calls if sent == "POST")
        run = created if method == "POST" else int(url.rsplit("/", 1)[1])
        return Mock(status_code=201 if method == "POST" else 200, json=Mock(return_value={"id": run}), text="")

    return request


def restarted(run: Run, poster: checks.Checks) -> Run:
    ports = scheduler.Ports(run.lab.inbox, run.lab.expander, lambda _repo: poster, run.lab.hotfixes, run.lab.kube)
    settings = scheduler.Settings(frozenset({ACTOR}), 0, Policy())
    return Run(run.lab, scheduler.Scheduler(run.lab.client, run.lab.objects, ports, settings))


@pytest.mark.feature("lab-scheduler-check-run-restart")
def test_a_restarted_scheduler_advances_the_stored_check_runs_with_no_new_create(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    seeds: list[tuple[Any, ...]] = []
    seed = checks.Checks.seed

    def spy(self: checks.Checks, *args: Any) -> None:
        seeds.append(args)
        seed(self, *args)

    monkeypatch.setattr(requests, "request", github(calls))
    monkeypatch.setattr(checks.Checks, "seed", spy)
    first = restarted(lab({HEAD_A: plan({"lint": (unit(1),)})}), checks.Checks(SLUG, FixtureToken()))
    first.push(HEAD_A)
    first.tick(1000)
    grade = first.grade(HEAD_A)
    again = restarted(first, checks.Checks(SLUG, FixtureToken()))
    again.lease()
    assert again.tick(1100) == [f"posted lint in_progress on {HEAD_A}", f"posted required in_progress on {HEAD_A}"]
    assert calls == [
        ("POST", CHECK_RUNS),
        ("POST", CHECK_RUNS),
        ("PATCH", f"{CHECK_RUNS}/1"),
        ("PATCH", f"{CHECK_RUNS}/2"),
    ]
    assert seeds == [
        (HEAD_A, "lint", grade.run, 1, "queued", True),
        (HEAD_A, scheduler.REQUIRED, grade.run, 2, "queued", True),
    ]
    assert grade.runs == again.grade(HEAD_A).runs == {"lint": 1, scheduler.REQUIRED: 2}


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


@pytest.mark.feature("lab-worker-deletion-cost")
def test_the_reap_pass_clears_the_pod_cost_of_a_worker_only_once_its_last_lease_expired(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1), unit(2))})})
    run.push(HEAD_A)
    run.tick(1000)
    first, second = run.lease(slots=2, ttl_ms=100)
    assert run.lab.kube.patches == [("worker", True)]
    assert run.leases(ttl_ms=500).heartbeat(second.job, second.token) == 1500
    assert f"reaped {first.job} pending" in run.tick(1100)
    assert run.lab.kube.patches == [("worker", True)]
    assert f"reaped {second.job} pending" in run.tick(1500)
    assert run.lab.kube.patches == [("worker", True), ("worker", False)]


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


@pytest.mark.feature("lab-dev-manifest-fetch")
def test_a_head_whose_manifest_fetch_github_refuses_is_dropped_with_its_reason(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({})

    def refused(request: GradeRequest) -> scheduler.Plan:
        raise FetchRefused(f"GET actions/workflows/test.yml/runs answered 404 for {request.base}")

    monkeypatch.setattr(run.lab.expander, "expand", refused)
    run.push(HEAD_A)
    assert run.tick(1000)[-1] == f"cannot expand {HEAD_A}: GET actions/workflows/test.yml/runs answered 404 for {BASE}"
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []


@pytest.mark.feature("lab-manifest-fetch-holds-head")
def test_a_head_whose_manifest_fetch_github_answers_with_a_server_error_waits_until_a_pass_finds_no_manifest(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({})
    refusals = [FetchUnanswered(f"GET actions/workflows/test.yml/runs answered 502 for {BASE}")]

    def expand(request: GradeRequest) -> scheduler.Plan:
        if refusals:
            raise refusals.pop()
        raise ValueError(f"no dependency manifest at or before {request.base}")

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    run.push(HEAD_A)
    assert run.tick(1000)[-1] == f"holding {HEAD_A}: GET actions/workflows/test.yml/runs answered 502 for {BASE}"
    assert list(run.lab.client.hgetall(scheduler.WAITING)) == [f"{REPO}|feature|{int(Priority.PROMOTION)}"]
    assert run.grades() == []
    assert run.tick(2000)[-1] == f"cannot expand {HEAD_A}: no dependency manifest at or before {BASE}"
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []


@pytest.mark.feature("lab-manifest-fetch-off-pass")
def test_a_head_whose_manifest_still_fetches_keeps_its_window_and_each_pass_reads_the_fetch_again(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=500)
    run.cache(unit(1))
    fetching = [manifests.FetchPending(f"{SLUG} manifest of {BASE} is still fetching")] * 2
    planned = run.lab.expander.expand

    def expand(request: GradeRequest) -> scheduler.Plan:
        if fetching:
            raise fetching.pop()
        return planned(request)

    monkeypatch.setattr(run.lab.expander, "expand", expand)
    run.push(HEAD_A)
    assert run.tick(1000) == [f"received {HEAD_A} on feature"]
    held = run.lab.client.hgetall(scheduler.WAITING)
    holding = f"holding {HEAD_A}: {SLUG} manifest of {BASE} is still fetching"
    assert run.tick(1500) == [holding]
    assert run.lab.client.hgetall(scheduler.WAITING) == held
    assert run.tick(1501) == [holding]
    assert run.tick(1502)[0] == f"grading {run.grade(HEAD_A).run} with 0 of 1 units held"
    assert run.lab.client.hgetall(scheduler.WAITING) == {}


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
    run.tick(1100)
    assert [grant.unit for grant in run.lease(slots=2)] == [shared, joined]
    run.push(HEAD_B, lane="pr-2", event="pull_request", action="synchronize")
    run.tick(1100)
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
        running_slots={"queue": 2},
        pending_slots={"promotion": 2, "pull_request": 1},
        workers_ready=1,
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
        workers_ready=2,
    )
    run.engine.queue.offer("busy", 1, 1000)
    assert run.engine.load(1000 + scheduler.READY_MS).slots_total == 2 + 1 + 4
    assert run.engine.load(1001 + scheduler.READY_MS).slots_total == 2 + 4
    assert run.engine.load(1001 + scheduler.READY_MS).workers_ready == 1


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


def _built(api: str) -> bytes:
    deadline = time.monotonic() + 10
    while True:
        try:
            return _read(f"{api}/capacity")
        except urllib.error.HTTPError as error:
            if error.code != 503 or time.monotonic() > deadline:
                raise
        time.sleep(0.05)


@pytest.mark.feature("lab-scheduler-service")
def test_listen_serves_leases_capacity_grades_and_metrics_from_one_budgeted_load(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})})
    run.push(HEAD_A)
    run.tick(1000)
    spending = budget.Budget(run.lab.client, lambda: 0, budget.Policy(100.0, {"backfill": 0.5}))
    spending.charge(1, 3600.0)
    routes = lease_api.ControlRoutes(run.lab.client, "tok", lambda: 1000)
    leased_first = threading.Event()
    load = run.engine.load

    def after_lease(now_ms: int) -> Load:
        assert leased_first.wait(10)
        return load(now_ms)

    monkeypatch.setattr(run.engine, "load", after_lease)
    servers = scheduler.listen(run.engine, run.leases(), routes, spending, lambda: 1000, (0, 0, 0))
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    api, control, exposition = (f"http://127.0.0.1:{server.server_address[1]}" for server in servers)
    try:
        body = json.dumps({"worker": "w", "free_slots": 1, "capabilities": [], "wait_ms": 0}).encode()
        leased = json.loads(_read(f"{api}/lease", body))["leases"]
        assert [grant["job"] for grant in leased] == [scheduler.job_id(HEAD_A, item)]
        leased_first.set()
        capacity = json.loads(_built(api))
        assert capacity["classes"]["promotion"]["running"] == 1
        assert capacity["budget_fraction"] == pytest.approx(0.4)
        assert capacity["shed"] == ["backfill"]
        grades = json.loads(_read(f"{api}/grades/{HEAD_A}"))
        assert [grade["run"] for grade in grades["grades"]] == [run.grade(HEAD_A).run]
        job, token = leased[0]["job"], leased[0]["token"]
        signed = json.loads(_read(f"{api}/upload-url?job={job}&token={token}&name=report"))
        assert signed == {"url": run.lab.objects.upload_url(upload_path(job, "report"), 0)}
        resume = urllib.request.Request(f"{control}/resume", data=b"", headers={"Authorization": "Bearer tok"})
        with urllib.request.urlopen(resume, timeout=5) as reply:
            assert json.loads(reply.read()) == {"resumed": []}
        text = _read(f"{exposition}/metrics").decode()
        assert 'ci_lab_units_running{class="promotion"} 1.0' in text
        assert 'ci_lab_class_shed{class="backfill"} 1.0' in text
        assert "ci_lab_slots_busy 1.0" in text
    finally:
        leased_first.set()
        for server in servers:
            server.shutdown()
            server.server_close()


def _status(url: str, authorization: str | None) -> int:
    headers = {} if authorization is None else {"Authorization": authorization}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=b"{}", headers=headers), timeout=5) as reply:
            return reply.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.mark.feature("lab-control-routes")
def test_listen_serves_the_control_routes_only_on_the_control_port(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    spending = budget.Budget(run.lab.client, lambda: 0, budget.Policy(100.0, {}))
    routes = lease_api.ControlRoutes(run.lab.client, "tok", lambda: 1000)
    servers = scheduler.listen(run.engine, run.leases(), routes, spending, lambda: 1000, (0, 0, 0))
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    api, control, _ = (f"http://127.0.0.1:{server.server_address[1]}" for server in servers)
    try:
        assert [_status(f"{api}{path}", "Bearer tok") for path in ("/priority", "/pause", "/resume")] == [404] * 3
        assert _status(f"{control}/resume", None) == 401
        assert _status(f"{control}/resume", "Bearer other") == 401
        assert _status(f"{control}/resume", "Bearer tok") == 200
        assert _status(f"{control}/lease", "Bearer tok") == 404
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def scrape(run: Run, now: int, mirror: Path | None = None) -> str:
    spending = budget.Budget(run.lab.client, lambda: now, budget.Policy(100.0, {}))
    routes = lease_api.ControlRoutes(run.lab.client, "tok", lambda: now)
    servers = scheduler.listen(run.engine, run.leases(), routes, spending, lambda: now, (0, 0, 0), mirror)
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _built(f"http://127.0.0.1:{servers[0].server_address[1]}")
        return _read(f"http://127.0.0.1:{servers[2].server_address[1]}/metrics").decode()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.mark.feature("lab-scheduler-metrics")
def test_the_exposition_asks_for_the_running_and_waiting_slots_after_a_pass(lab: Callable[..., Run]) -> None:
    held, waiting, big = unit(1), unit(2), unit(3)
    run = lab({HEAD_A: plan({"lint": (held, waiting, big)})})
    run.push(HEAD_A)
    run.tick(1000)
    run.lab.client.hset(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, big)}", "slots", 3)
    assert [grant.unit for grant in run.lease()] == [held]
    run.tick(1100)
    assert "\nci_lab_slots_wanted 5.0\n" in scrape(run, 1100)


LOW_BUDGET = budget.Policy(100.0, {"backfill": 0.5})
DAY_MS = 24 * 60 * 60 * 1000


def _spend(run: Run) -> None:
    spending = run.engine.spending
    assert spending is not None
    spending.charge(1, 3600.0)


@pytest.mark.feature("lab-scheduler-budget")
def test_a_forced_low_budget_enqueues_no_backfill_unit_and_still_enqueues_every_pull_request_unit(
    lab: Callable[..., Run],
) -> None:
    swept, checked, linted = unit(1), unit(2), unit(3)
    run = lab({HEAD_A: plan({"lint": (swept,)}), HEAD_B: plan({"lint": (checked, linted)})}, spend=LOW_BUDGET)
    _spend(run)
    run.engine.queue.offer("worker", 8, 1000)
    run.push(HEAD_A, lane="sweep", event="backfill")
    run.push(HEAD_B, lane="pr-2", event="pull_request", action="synchronize")
    actions = run.tick(1000)
    held, pulled = run.grade(HEAD_A), run.grade(HEAD_B)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, checked), scheduler.job_id(HEAD_B, linted)]
    assert [(slot.job, slot.held) for slot in held.units.values()] == [(scheduler.job_id(HEAD_A, swept), True)]
    assert [slot.held for slot in pulled.units.values()] == [False, False]
    assert f"grading {held.run} with 1 of 1 units held" in actions
    assert f"enqueued 2 units of {pulled.run}" in actions
    assert f"enqueued 1 units of {held.run}" not in actions
    assert held.run in run.lab.client.smembers(scheduler.LIVE)
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED) == [("queued", None)]
    assert run.tick(1100) == []
    reset = DAY_MS + 1000
    run.engine.queue.offer("worker", 8, reset)
    assert f"enqueued 1 units of {held.run}" in run.tick(reset)
    assert run.engine.queue.pending()[-1] == scheduler.job_id(HEAD_A, swept)
    assert [slot.held for slot in run.grade(HEAD_A).units.values()] == [False]


@pytest.mark.feature("lab-scheduler-budget")
def test_backfill_takes_only_the_slots_left_free_after_the_units_waiting_in_other_classes(
    lab: Callable[..., Run],
) -> None:
    first, second, third, fourth, checked, linted = (unit(number) for number in range(1, 7))
    run = lab({HEAD_A: plan({"lint": (first, second, third, fourth)}), HEAD_B: plan({"lint": (checked, linted)})})
    run.engine.queue.offer("worker", 3, 1000)
    run.push(HEAD_A, lane="sweep", event="backfill")
    run.push(HEAD_B, lane="pr-2", event="pull_request", action="synchronize")
    run.tick(1000)
    pulled = [scheduler.job_id(HEAD_B, checked), scheduler.job_id(HEAD_B, linted)]
    assert run.engine.queue.pending() == [*pulled, scheduler.job_id(HEAD_A, first)]
    assert "\nci_lab_slots_wanted 2.0\n" in scrape(run, 1000)
    assert run.tick(1050) == []
    assert run.engine.queue.pending() == [*pulled, scheduler.job_id(HEAD_A, first)]
    grants = run.lease(slots=3)
    assert [grant.job for grant in grants] == [*pulled, scheduler.job_id(HEAD_A, first)]
    for grant in grants:
        run.finish(grant, "passed", {"seconds": 1.0})
    run.tick(1100)
    assert run.engine.queue.pending() == []
    assert run.lease(slots=3) == []
    assert run.tick(1200) == [f"enqueued 3 units of {run.grade(HEAD_A).run}"]
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item) for item in (second, third, fourth)]


@pytest.mark.feature("lab-lease-class-order")
def test_a_pull_request_grade_admitted_after_a_merge_group_grade_is_leased_first(lab: Callable[..., Run]) -> None:
    grouped, checked = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (grouped,)}), HEAD_B: plan({"lint": (checked,)})})
    run.push(HEAD_A, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.tick(1000)
    run.push(HEAD_B, lane="pr-2", event="pull_request", action="synchronize")
    run.tick(2000)

    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, checked), scheduler.job_id(HEAD_A, grouped)]
    assert [grant.job for grant in run.lease(slots=1)] == [scheduler.job_id(HEAD_B, checked)]
    queued = run.engine.queue.job(scheduler.job_id(HEAD_A, grouped))
    assert queued is not None
    assert queued.rank == int(Priority.QUEUE)


@pytest.mark.feature("lab-scheduler-budget")
def test_a_newer_backfill_head_drops_the_held_units_of_the_older_one(lab: Callable[..., Run]) -> None:
    older, newer = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (older,)}), HEAD_B: plan({"lint": (newer,)})}, spend=LOW_BUDGET)
    _spend(run)
    run.engine.queue.offer("worker", 4, 1000)
    run.push(HEAD_A, lane="sweep", event="backfill")
    run.tick(1000)
    run.push(HEAD_B, lane="sweep", event="backfill")
    run.tick(1100)
    old = run.grade(HEAD_A)
    assert old.superseded
    assert [(slot.held, slot.outcome) for slot in old.units.values()] == [(False, "dropped")]
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED)[-1] == ("completed", "cancelled")
    assert old.run not in run.lab.client.smembers(scheduler.LIVE)
    assert run.engine.queue.pending() == []
    run.engine.queue.offer("worker", 4, DAY_MS)
    run.tick(DAY_MS)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, newer)]


@pytest.mark.feature("lab-scheduler-budget-charge")
def test_a_finished_unit_charges_its_slots_times_its_report_seconds_once_and_no_seconds_charges_nothing(
    lab: Callable[..., Run],
) -> None:
    timed, untimed, unreported = unit(1), unit(2), unit(3)
    run = lab({HEAD_B: plan({"lint": (timed, untimed, unreported)})}, spend=LOW_BUDGET)
    run.engine.queue.enqueue([timed], HEAD_B, {timed.key: queue.Requirement(slots=2)})
    run.push(HEAD_B, lane="pr-2", event="pull_request", action="synchronize")
    run.tick(1000)
    first, second, third = run.lease(slots=4)
    assert (first.slots, second.slots, third.slots) == (2, 1, 1)
    run.finish(first, "passed", {"seconds": 1500.0})
    run.finish(second, "passed", {})
    run.finish(third, "passed")
    run.tick(1100)
    spending = run.engine.spending
    assert spending is not None
    assert spending.spent() == 50.0
    run.tick(1200)
    assert spending.spent() == 50.0


@pytest.mark.feature("lab-scheduler-budget-charge")
def test_the_first_pass_after_finished_units_shed_a_class_names_it_with_the_spent_share_once(
    lab: Callable[..., Run],
) -> None:
    pulled, swept = unit(1), unit(2)
    run = lab({HEAD_B: plan({"lint": (pulled,)}), HEAD_A: plan({"lint": (swept,)})}, spend=LOW_BUDGET)
    run.engine.queue.offer("worker", 8, 1000)
    run.push(HEAD_B, lane="pr-2", event="pull_request", action="synchronize")
    run.tick(1000)
    (grant,) = run.lease()
    run.finish(grant, "passed", {"seconds": 3600.0})
    assert not any(action.startswith("shed ") for action in run.tick(1100))
    line = "shed backfill with 0.60 of the day's core budget spent"
    assert line in run.tick(1200)
    run.push(HEAD_A, lane="sweep", event="backfill")
    assert line not in run.tick(1300)
    assert [slot.held for slot in run.grade(HEAD_A).units.values()] == [True]
    assert run.engine.queue.pending() == []


@pytest.mark.feature("lab-scheduler-metrics")
def test_the_exposition_counts_the_workers_that_polled_within_the_ready_window_after_a_pass(
    lab: Callable[..., Run],
) -> None:
    run = lab({})
    run.lab.now = 1000
    assert run.leases().lease("first", 2, frozenset(), 0) == []
    run.lab.now = 1500
    assert run.leases().lease("second", 1, frozenset(), 0) == []
    run.tick(1000 + scheduler.READY_MS)
    assert "\nci_lab_workers_ready 2.0\n" in scrape(run, 1000 + scheduler.READY_MS)
    assert "\nci_lab_workers_ready 1.0\n" in scrape(run, 1001 + scheduler.READY_MS)


@pytest.mark.feature("lab-scheduler-metrics")
def test_the_exposition_counts_leased_units_past_their_estimate_after_a_pass(lab: Callable[..., Run]) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first, second)})})
    run.push(HEAD_A)
    run.tick(1000)
    assert [grant.unit for grant in run.lease(ttl_ms=10**6)] == [first]
    run.lab.now = 2000
    assert [grant.unit for grant in run.lease(ttl_ms=10**6)] == [second]
    estimate_ms = int(scheduler.UNIT_ESTIMATE_S * 1000)
    run.tick(1000 + estimate_ms)
    assert "\nci_lab_units_overdue 0.0\n" in scrape(run, 1000 + estimate_ms)
    assert "\nci_lab_units_overdue 1.0\n" in scrape(run, 1001 + estimate_ms)
    assert "\nci_lab_units_overdue 2.0\n" in scrape(run, 2001 + estimate_ms)
    run.lab.client.hdel(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, first)}", "leased_ms")
    assert "\nci_lab_units_overdue 1.0\n" in scrape(run, 2001 + estimate_ms)


@pytest.mark.feature("lab-scheduler-metrics")
def test_the_exposition_counts_each_finished_attempts_core_seconds_once_in_its_most_urgent_class_after_a_pass(
    lab: Callable[..., Run],
) -> None:
    shared, solo = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (shared, solo)})}, retries=Policy(failed=1), spend=LOW_BUDGET)
    run.push(HEAD_A)
    run.tick(1000)
    run.lab.expander.plans[HEAD_A] = plan({"lint": (shared,)})
    run.push(HEAD_A, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.tick(1100)
    run.lab.client.hset(f"{queue.PREFIX}job:{scheduler.job_id(HEAD_A, shared)}", "slots", 2)
    grants = {grant.unit: grant for grant in run.lease(slots=3)}
    run.finish(grants[shared], scheduler.PASSED, {"seconds": 30.0})
    run.finish(grants[solo], scheduler.FAILED, {"seconds": 5.0})
    run.tick(1200)
    text = scrape(run, 1200)
    assert 'ci_lab_core_seconds_total{class="queue"} 60.0' in text
    assert 'ci_lab_core_seconds_total{class="promotion"} 5.0' in text
    [again] = run.lease()
    run.finish(again, scheduler.PASSED, {"seconds": 5.0})
    run.tick(1300)
    run.tick(1400)
    text = scrape(run, 1400)
    assert 'ci_lab_core_seconds_total{class="queue"} 60.0' in text
    assert 'ci_lab_core_seconds_total{class="promotion"} 10.0' in text
    assert run.lab.client.smembers(scheduler.LIVE) == set()
    spending = run.engine.spending
    assert spending is not None
    assert spending.spent() == pytest.approx(70.0 / 60)


@pytest.mark.feature("lab-scheduler-metrics")
def test_the_exposition_shows_the_last_finished_units_duration_per_kind_after_a_pass(lab: Callable[..., Run]) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first, second)})})
    run.push(HEAD_A)
    run.tick(1000)
    grants = {grant.unit: grant for grant in run.lease(slots=2)}
    run.finish(grants[first], scheduler.PASSED, {"seconds": 4.0})
    run.tick(1100)
    assert 'ci_lab_unit_duration_seconds{kind="static"} 4.0' in scrape(run, 1100)
    run.finish(grants[second], scheduler.PASSED, {"seconds": 9.5})
    run.tick(1200)
    assert 'ci_lab_unit_duration_seconds{kind="static"} 9.5' in scrape(run, 1200)


@pytest.mark.feature("lab-scheduler-metrics")
def test_the_exposition_counts_each_verdict_store_lookup_a_pass_makes(lab: Callable[..., Run]) -> None:
    cached, fresh, other = unit(1), unit(2), unit(3)
    run = lab({HEAD_A: plan({"lint": (cached, fresh, other)}), HEAD_B: plan({"lint": (cached, fresh)})})
    run.cache(cached)
    run.push(HEAD_A)
    run.tick(1000)
    text = scrape(run, 1000)
    assert 'ci_lab_cache_hits_total{cache="verdict"} 1.0' in text
    assert 'ci_lab_cache_misses_total{cache="verdict"} 2.0' in text
    assert [grant.unit for grant in run.lease()] == [fresh]
    run.push(HEAD_B)
    run.tick(1100)
    assert run.grade(HEAD_B).units[ref(fresh)].job == scheduler.job_id(HEAD_A, fresh)
    text = scrape(run, 1100)
    assert 'ci_lab_cache_hits_total{cache="verdict"} 2.0' in text
    assert 'ci_lab_cache_misses_total{cache="verdict"} 3.0' in text


@pytest.mark.feature("lab-scheduler-metrics")
def test_the_exposition_counts_the_git_and_uv_lookups_a_finished_units_report_carries_after_a_pass(
    lab: Callable[..., Run],
) -> None:
    first, second, silent = unit(1), unit(2), unit(3)
    run = lab({HEAD_A: plan({"lint": (first, second, silent)})})
    run.push(HEAD_A)
    run.tick(1000)
    grants = {grant.unit: grant for grant in run.lease(slots=3)}
    cut = {"git": {"hits": 2, "misses": 1}, "uv": {"hits": 0, "misses": 1}}
    reused = {"git": {"hits": 1, "misses": 0}, "uv": {"hits": 1, "misses": 0}}
    run.finish(grants[first], scheduler.PASSED, {"seconds": 1.0, "caches": cut})
    run.finish(grants[second], scheduler.PASSED, {"seconds": 1.0, "caches": reused})
    run.finish(grants[silent], scheduler.PASSED, {"seconds": 1.0})
    run.tick(1100)
    run.tick(1200)
    text = scrape(run, 1200)
    assert 'ci_lab_cache_hits_total{cache="git"} 3.0' in text
    assert 'ci_lab_cache_misses_total{cache="git"} 1.0' in text
    assert 'ci_lab_cache_hits_total{cache="uv"} 1.0' in text
    assert 'ci_lab_cache_misses_total{cache="uv"} 1.0' in text


@pytest.mark.feature("lab-scheduler-metrics")
def test_the_exposition_counts_each_grade_a_pass_closes_by_class_and_conclusion(lab: Callable[..., Run]) -> None:
    passing, failing = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (passing,)}), HEAD_B: plan({"lint": (failing,)})})
    run.cache(passing)
    run.cache(failing, passed=False)
    run.push(HEAD_A)
    run.push(HEAD_B, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.tick(1000)
    run.tick(1100)
    text = scrape(run, 1100)
    assert 'ci_lab_heads_graded_total{class="promotion",conclusion="success"} 1.0' in text
    failed = run.grade(HEAD_B).conclusion
    assert failed in ("failure", "success")
    assert f'ci_lab_heads_graded_total{{class="queue",conclusion="{failed}"}} 1.0' in text
    assert text.count("ci_lab_heads_graded_total{") == 2


@pytest.mark.feature("lab-scheduler-metrics")
def test_the_exposition_counts_each_expired_lease_a_pass_requeues(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    run.push(HEAD_A)
    run.tick(1000)
    run.lease(ttl_ms=100)
    run.tick(1100)
    text = scrape(run, 1100)
    assert "\nci_lab_units_redelivered_total 1.0\n" in text
    assert "\nci_lab_units_lost_total " not in text


@pytest.mark.feature("lab-scheduler-metrics")
def test_the_exposition_counts_each_expired_lease_a_pass_marks_lost(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    run.push(HEAD_A)
    run.tick(1000)
    run.lease(ttl_ms=100)
    run.tick(1100)
    run.lease(ttl_ms=100)
    run.tick(1300)
    text = scrape(run, 1300)
    assert "\nci_lab_units_lost_total 1.0\n" in text
    assert "\nci_lab_units_redelivered_total 1.0\n" in text


FLUSHED = "<flushed>"


@dataclass
class Stdout:
    events: list[str] = field(default_factory=list[str])

    def write(self, text: str) -> int:
        self.events.append(text)
        return len(text)

    def flush(self) -> None:
        self.events.append(FLUSHED)


@dataclass
class CountingRelay:
    """A relay stand in that writes each tick to the output and returns one action naming it."""

    out: Stdout
    ticks: int = 0

    def tick(self) -> list[str]:
        self.ticks += 1
        self.out.events.append(f"tick {self.ticks}")
        return [f"relayed {self.ticks}"]


class SilentRelay:
    """A relay stand in with nothing to relay."""

    def tick(self) -> list[str]:
        return []


@dataclass
class RelayBuilds:
    """A relay build stand in recording the environment, scheduler and consumer of each build."""

    builds: list[tuple[Any, scheduler.Scheduler, Any]] = field(
        default_factory=list[tuple[Any, scheduler.Scheduler, Any]]
    )

    def __call__(self, environ: Any, found: scheduler.Scheduler, consumer: Any) -> str:
        self.builds.append((environ, found, consumer))
        return "relay"


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

    assert scheduler.serve(run.engine, SilentRelay(), 0.25, 2, lambda: 2.5, sleep) == 0
    assert slept == [0.25, 0.25]
    assert out.events == [f"received {HEAD_A} on feature", "\n", FLUSHED]
    assert json.loads(run.lab.client.hget(scheduler.WAITING, f"{REPO}|feature|3"))["since"] == 2500


@pytest.mark.feature("lab-scheduler-runs-relay")
def test_serve_ticks_the_relay_after_each_pass_and_prints_its_actions(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=60_000)
    run.push(HEAD_A)
    out = Stdout()
    monkeypatch.setattr(sys, "stdout", out)
    relaying = CountingRelay(out)

    def sleep(seconds: float) -> None:
        out.events.append(f"slept {seconds}")

    assert scheduler.serve(run.engine, relaying, 0.25, 2, lambda: 2.5, sleep) == 0
    assert relaying.ticks == 2
    assert out.events == [
        f"received {HEAD_A} on feature",
        "\n",
        FLUSHED,
        "tick 1",
        "relayed 1",
        "\n",
        FLUSHED,
        "slept 0.25",
        "tick 2",
        "relayed 2",
        "\n",
        FLUSHED,
        "slept 0.25",
    ]


def test_serve_without_a_pass_count_runs_until_stopped(lab: Callable[..., Run]) -> None:
    run = lab({})
    passes: list[float] = []

    def sleep(seconds: float) -> None:
        passes.append(seconds)
        if len(passes) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        scheduler.serve(run.engine, CountingRelay(Stdout()), 1.0, 0, lambda: 1.0, sleep)
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


@pytest.mark.feature("lab-remote-heads")
def test_the_bus_inbox_keeps_the_grade_ref_a_lab_remote_notice_names() -> None:
    pushed = HeadEvent("notice", REPO, HEAD_A, BASE, ACTOR, "pushed")
    notice = envelope(pushed)
    notice = replace(notice, data={**notice.data, "ref": "refs/grade/t1", "branch": "engineer-1"})
    inbox = scheduler.BusInbox(Consumer([Delivery(notice), Delivery(envelope(pushed))]))
    assert inbox.heads() == [scheduler.Head(pushed, "engineer-1", "refs/grade/t1"), scheduler.Head(pushed, HEAD_A)]


@pytest.mark.feature("lab-lease-event-branch")
def test_the_bus_inbox_reads_the_head_branch_and_a_head_published_before_it_with_none() -> None:
    branched = HeadEvent("pull_request", REPO, HEAD_A, BASE, ACTOR, "opened", "fix-x")
    older = HeadEvent("pull_request", REPO, HEAD_B, BASE, ACTOR, "opened")
    deliveries = [Delivery(envelope(branched)), Delivery(_without(older, "head_branch"))]
    inbox = scheduler.BusInbox(Consumer([*deliveries]))
    assert inbox.heads() == [scheduler.Head(branched, HEAD_A), scheduler.Head(older, HEAD_B)]
    assert [delivery.acked for delivery in deliveries] == [True, True]


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
            ".github/workflows/scan.yaml": "jobs: {scan: {}}\n",
            ".github/workflows/nested/deep.yml": "jobs: {}\n",
            ".github/workflows/empty.yml": "",
            "delivery.yaml": json.dumps(DELIVERY),
            LOCK: "lock",
            "pyproject.toml": "[project]\n",
            "src/a.py": "a = 1\n",
            "srcx/b.py": "b = 1\n",
            "docs/c.md": "c\n",
        },
    )
    head = _commit(repo, {"src/a.py": "a = 2\n", LOCK: "lock 2", "docs/tab\tname.md": "t\n"})
    return repo, base, head


def _gate(name: str, job: str, paths: tuple[str, ...], base_paths: tuple[str, ...]) -> StaticGate:
    return StaticGate(name, job, name, (), (), "", paths, base_paths)


def _no_tests(repo: str, commits: Sequence[str]) -> dict[str, Any]:
    return {"commit": "dev", "units": [], "tests": {}}


def _unknown(kind: str, key: str) -> Verdict | None:
    return None


def test_the_mirror_expander_keys_static_gates_from_git_objects_without_a_checkout(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = origin
    read: list[tuple[str, Any, Any]] = []
    gates = [
        _gate("size", "lint", ("src",), ("src/a.py",)),
        _gate("format", "lint", ("src/",), ()),
        _gate("semgrep", "semgrep", (".",), ("docs/c.md", "sr")),
    ]

    def discover(workflow: str, registry: Any, reusable: Any) -> list[StaticGate]:
        read.append((workflow, registry, reusable))
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
    chain = Toolchain("3.12", {}, _blob(repo, head, LOCK), _blob(repo, head, "pyproject.toml"), "image", {})
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
    reusable = {"test.yml": {"jobs": {}}, "scan.yaml": {"jobs": {"scan": {}}}, "empty.yml": {}}
    assert read == [("jobs: {}\n", {"gates": {}}, reusable)]
    promoted = expander.expand(GradeRequest(remote, head, base, Priority.PROMOTION, ACTOR, False))
    assert (promoted.tree, promoted.grader, set(promoted.gates), promoted.skipped) == (
        tree,
        head,
        {"lint", "semgrep", "unit"},
        frozenset(),
    )


@pytest.mark.feature("lab-lease-event-branch")
def test_the_mirror_expander_keys_only_the_gates_that_read_an_event_name_with_its_value(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = origin
    plain = _gate("size", "lint", ("src",), ())
    evented = replace(_gate("plant", "lint", ("src",), ()), reads=("github.event_name",))
    branched = replace(_gate("fragments", "lint", ("src",), ()), reads=("github.head_ref", "github.ref_name"))

    def discover(workflow: str, registry: Any, reusable: Any) -> list[StaticGate]:
        return [plain, evented, branched]

    monkeypatch.setattr(scheduler, "discover", discover)
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)
    chain = Toolchain("3.12", {}, _blob(repo, head, LOCK), _blob(repo, head, "pyproject.toml"), "image", {})
    files = {"src/a.py": _blob(repo, head, "src/a.py")}

    def keyed(name: str, grader: str, event: dict[str, str]) -> Unit:
        return Unit("static", static_key(StaticInputs(name, files, {}, grader, chain, event)), (name,))

    request = GradeRequest(f"file://{repo}", head, base, Priority.PULL_REQUEST, ACTOR, False, "fix-x")
    assert expander.expand(request).gates["lint"] == (
        keyed("size", base, {}),
        keyed("plant", base, {"github.event_name": "pull_request"}),
        keyed("fragments", base, {"github.head_ref": "fix-x", "github.ref_name": ""}),
    )
    assert expander.expand(replace(request, head_branch="")).gates["lint"] == (
        keyed("size", base, {}),
        keyed("plant", base, {"github.event_name": "pull_request"}),
    )
    promoted = expander.expand(replace(request, tier=Priority.PROMOTION, head_branch=""))
    assert promoted.gates["lint"] == (keyed("size", head, {}), keyed("plant", head, {"github.event_name": "push"}))


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
    bare = _drop(repo, LOCK, "pyproject.toml")
    monkeypatch.setattr(
        scheduler, "discover", lambda workflow, registry, reusable: [_gate("format", "lint", ("src/",), ())]
    )
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


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.feature("lab-reusable-workflow-gates")
def test_the_mirror_expander_expands_the_real_tests_workflow_with_its_reusable_workflows(tmp_path: Path) -> None:
    repo = tmp_path / "origin"
    _git(tmp_path, "init", "-q", "-b", "dev", str(repo))
    workflows = ROOT / ".github" / "workflows"
    registry = ROOT / ".github" / "input-keys.json"
    tree = {f".github/workflows/{path.name}": path.read_text() for path in workflows.iterdir() if path.is_file()}
    base = _commit(repo, {**tree, ".github/input-keys.json": registry.read_text(), LOCK: "lock"})
    head = _commit(repo, {"src/a.py": "a = 1\n"})
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    expander = scheduler.MirrorExpander(
        node, tmp_path / "stores", Toolchain("3.12", {}, "", "", "", {}), _no_tests, _unknown
    )
    found = expander.expand(GradeRequest(f"file://{repo}", head, base, Priority.PROMOTION, ACTOR, False))
    text = (workflows / "test.yml").read_text()
    expected: dict[str, list[str]] = {"unit": []}
    ungraded: list[str] = []
    for gate in static_gates.discover(text, json.loads(registry.read_text()), ci_wiring.load(ROOT)):
        if gate.missing or {"github.head_ref", "github.ref_name"} & set(gate.reads):
            ungraded.append(gate.name)
        else:
            expected.setdefault(gate.job, []).append(gate.name)
    assert {job: [name for item in units for name in item.members] for job, units in found.gates.items()} == expected
    assert {"semgrep/scan", "semgrep/feature-tests", "semgrep/charts"} <= set(found.gates)
    assert ungraded == [
        "lint/refuse-changelog-fragments-named-off-the-branch-or-opening-without-a-section-heading",
        "lint/artifact-sanity-checks",
    ]


CHANGELOG = "lint/refuse-changelog-fragments-named-off-the-branch-or-opening-without-a-section-heading"
TRUSTED = "lint/require-trusted-actors-before-private-runner-admission"
SELF_PLANT = "lint/prove-the-mutation-gate-kills-with-the-real-test-and-fails-with-a-weakened-one"
ARTIFACT = "lint/artifact-sanity-checks"


@pytest.mark.feature("lab-lease-event-branch")
def test_the_mirror_expander_plans_the_changelog_fragment_gate_for_a_pull_request_head_with_its_branch(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "origin"
    _git(tmp_path, "init", "-q", "-b", "dev", str(repo))
    workflows = ROOT / ".github" / "workflows"
    tree = {f".github/workflows/{path.name}": path.read_text() for path in workflows.iterdir() if path.is_file()}
    tree |= {name: (ROOT / name).read_text() for name in (".github/input-keys.json", "delivery.yaml")}
    base = _commit(repo, {**tree, LOCK: "lock"})
    head = _commit(repo, {"src/a.py": "a = 1\n"})
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, _no_tests, _unknown)
    request = GradeRequest(f"file://{repo}", head, base, Priority.PULL_REQUEST, ACTOR, False, "fix-x")

    def linted(found: scheduler.Plan) -> list[str]:
        return [name for item in found.gates["lint"] for name in item.members]

    planned = linted(expander.expand(request))
    assert {CHANGELOG, TRUSTED, SELF_PLANT} <= set(planned)
    assert ARTIFACT not in planned
    assert CHANGELOG not in linted(expander.expand(replace(request, head_branch="")))


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
PINNED = '[project]\nname = "suite"\n\n[project.optional-dependencies]\ndev = ["mutmut==3.6.0"]\n'
FIRST = "scripts.alpha.x_first"
SECOND = "scripts.alpha.x_second"
GAMMA = "tests/test_gamma.py"


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
            LOCK: "lock",
            "pyproject.toml": PINNED,
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
    chain = Toolchain("3.12", {}, blobs[LOCK], blobs["pyproject.toml"], "image", {})
    return expand_tests.units(SUITE_MANIFEST, _contents(suite, head), blobs, durations, chain)


def _contents(suite: Suite, head: str) -> dict[str, bytes]:
    paths = filter(None, _git(suite.repo, "ls-tree", "-r", "-z", "--name-only", head).split("\0"))
    shown = ["git", "-C", str(suite.repo), "show"]
    return {path: subprocess.run([*shown, f"{head}:{path}"], capture_output=True, check=True).stdout for path in paths}


def _mutation_gates(
    suite: Suite, head: str, changes: dict[str, list[int]], files: list[str], covering: dict[str, list[str]]
) -> dict[str, tuple[Unit, ...]]:
    contents = _contents(suite, head)
    keys = expand_tests.fingerprints(SUITE_MANIFEST, contents)
    checkout = suite.repo.parent / f"expected-{head}"
    _git(suite.repo, "worktree", "add", "-q", "--detach", str(checkout), head)
    stats = expand_mutation.stats_units(checkout, files, executors.OPTIONS)
    covered = {name: {test: keys[test] for test in tests} for name, tests in covering.items()}
    mutants = expand_mutation.mutant_units(
        checkout, changes, covered, expand_tests.unlisted_test_hashes(SUITE_MANIFEST, contents)
    )
    _git(suite.repo, "worktree", "remove", "--force", str(checkout))
    return {name: units for name, units in {"mutation-stats": stats, "mutation": mutants}.items() if units}


def _tree(suite: Suite, head: str) -> str:
    return _git(suite.repo, "rev-parse", f"{head}^{{tree}}").strip()


@pytest.fixture
def staticless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "discover", lambda workflow, registry, reusable: [])


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
    expected = scheduler.frame(Priority.QUEUE, _tree(suite, suite.head), suite.base, {"unit": every[2:]})
    assert found == replace(expected, manifest="dev")
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
    assert found == replace(
        scheduler.frame(Priority.QUEUE, _tree(suite, head), suite.base, {"unit": ()}), manifest="dev"
    )
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
    expected = scheduler.frame(Priority.QUEUE, _tree(suite, suite.head), suite.base, {"unit": every})
    assert found == replace(expected, manifest="dev")


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize(("known", "picked"), [(True, slice(2, 3)), (False, slice(0, 3))], ids=["invalidated", "full"])
def test_a_merge_group_entry_that_adds_a_test_file_the_manifest_lacks_runs_its_unit(
    tmp_path: Path, known: bool, picked: slice
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    _git(suite.repo, "switch", "-q", "-c", "added", suite.base)
    beta = "def test_second():\n    assert True\n"
    head = _commit(suite.repo, {GAMMA: "def test_third():\n    pass\n", "tests/test_beta.py": beta})
    found = Port(SUITE_MANIFEST, known).expander(tmp_path).expand(suite.request(Priority.QUEUE, head))
    every = _suite_units(suite, head, SUITE_DURATIONS)
    paths = filter(None, _git(suite.repo, "ls-tree", "-r", "-z", "--name-only", head).split("\0"))
    blobs = {path: _blob(suite.repo, head, path) for path in paths}
    chain = Toolchain("3.12", {}, blobs[LOCK], blobs["pyproject.toml"], "image", {})
    added = expand_tests.unlisted_units(SUITE_MANIFEST, _contents(suite, head), [GAMMA], chain)
    assert [unit.members for unit in added] == [(GAMMA,)]
    expected = scheduler.frame(Priority.QUEUE, _tree(suite, head), suite.base, {"unit": (*every[picked], *added)})
    assert found == replace(expected, manifest="dev")


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
    mutation = _mutation_gates(
        suite, suite.head, {"scripts/alpha.py": [6]}, ["tests/test_beta.py"], {SECOND: [SECOND_TEST]}
    )
    expected = scheduler.frame(tier, _tree(suite, suite.head), suite.head, {"unit": every, **mutation})
    assert found == replace(expected, manifest="dev")
    assert (port.asked, port.looked) == ([(suite.remote, [suite.base, suite.seed])], [])


@pytest.mark.feature("lab-scheduler-test-units")
@pytest.mark.usefixtures("staticless")
def test_a_head_without_stored_durations_keeps_each_test_file_whole(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    head = _drop(suite.repo, ".test_durations")
    found = Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(Priority.PROMOTION, head))
    every = _suite_units(suite, head, {})
    assert [batch.members for batch in every] == [(FIRST_TEST, OTHER_TEST), (SECOND_TEST,)]
    mutation = _mutation_gates(suite, head, {"scripts/alpha.py": [6]}, ["tests/test_beta.py"], {SECOND: [SECOND_TEST]})
    assert found.gates == {"unit": every, **mutation}


def _store(tmp_path: Path, suite: Suite) -> Path:
    return tmp_path / "stores" / str(suite.repo).removeprefix("/").replace("/", "--")


@pytest.mark.feature("lab-scheduler-mutation-units")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize("tier", [Priority.HOTFIX, Priority.PULL_REQUEST, Priority.QUEUE])
def test_a_hotfix_pull_request_or_merge_group_head_expands_no_mutation_units(tmp_path: Path, tier: Priority) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    found = Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(tier))
    assert {"mutation-stats", "mutation"}.isdisjoint(found.gates)
    assert list((tmp_path / "stores").iterdir()) == [_store(tmp_path, suite)]


@pytest.mark.feature("lab-scheduler-mutation-units")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize("tier", [Priority.PROMOTION, Priority.BACKFILL])
def test_a_promotion_or_backfill_head_names_the_mutants_of_each_changed_function_in_a_checkout_it_removes(
    tmp_path: Path, tier: Priority, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    source = "def first():\n    return 4\n\n\ndef second():\n    return 3\n"
    head = _commit(suite.repo, {"scripts/alpha.py": source})
    checkouts: list[tuple[Path, str]] = []
    build = expand_mutation.mutant_units

    def spy(root: Path, changes: Any, covering: Any, unlisted: Any, *, timeout: float) -> tuple[Unit, ...]:
        checkouts.append((root.parent, (root / "scripts/alpha.py").read_text()))
        return build(root, changes, covering, unlisted, timeout=timeout)

    monkeypatch.setattr(expand_mutation, "mutant_units", spy)
    found = Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(tier, head))
    monkeypatch.undo()
    assert checkouts == [(tmp_path / "stores", source)]
    mutation = _mutation_gates(
        suite,
        head,
        {"scripts/alpha.py": [2, 6]},
        ["tests/test_alpha.py", "tests/test_beta.py"],
        {FIRST: [FIRST_TEST, OTHER_TEST], SECOND: [SECOND_TEST]},
    )
    assert found.gates == {"unit": _suite_units(suite, head, SUITE_DURATIONS), **mutation}
    stats, mutants = found.gates["mutation-stats"], found.gates["mutation"]
    assert [(unit.kind, unit.members) for unit in stats] == [
        ("stats", ("tests/test_alpha.py",)),
        ("stats", ("tests/test_beta.py",)),
    ]
    assert [(unit.kind, executors.function_of(unit.members[0])) for unit in mutants] == [
        ("mutant", FIRST),
        ("mutant", SECOND),
    ]
    store = _store(tmp_path, suite)
    assert list((tmp_path / "stores").iterdir()) == [store]
    assert _git(store, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.feature("lab-mutant-unit-split")
@pytest.mark.usefixtures("staticless")
def test_a_promotion_head_splits_a_mutant_unit_priced_over_the_executors_unit_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    head = _commit(suite.repo, {"scripts/alpha.py": ALPHA.replace("return 1", "return 4 + 1")})
    monkeypatch.setattr(executors, "UNIT_TIMEOUT_S", MUTANT_SECONDS + 1.0)
    mutants = Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(Priority.PROMOTION, head)).gates["mutation"]
    assert len(mutants) > 2
    assert [unit.members for unit in mutants] == [(f"{FIRST}__mutmut_{index}",) for index in range(1, len(mutants) + 1)]


@pytest.mark.feature("lab-scheduler-mutation-units")
@pytest.mark.usefixtures("staticless")
def test_a_promotion_head_keys_a_mutant_unit_by_the_tests_that_ran_its_function(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    expander = Port(SUITE_MANIFEST).expander(tmp_path)
    _git(suite.repo, "switch", "-q", "-c", "other", suite.base)
    changed = _commit(suite.repo, {"scripts/alpha.py": ALPHA.replace("return 1", "return 4")})
    before = expander.expand(suite.request(Priority.PROMOTION, changed)).gates["mutation"]
    other = _commit(suite.repo, {"tests/test_beta.py": "def test_second():\n    assert True\n"})
    own = _commit(
        suite.repo, {"tests/test_alpha.py": "def test_first():\n    assert True\n\n\ndef test_other():\n    pass\n"}
    )
    assert [executors.function_of(unit.members[0]) for unit in before] == [FIRST]
    assert expander.expand(suite.request(Priority.PROMOTION, other)).gates["mutation"] == before
    after = expander.expand(suite.request(Priority.PROMOTION, own)).gates["mutation"]
    assert [unit.members for unit in after] == [unit.members for unit in before]
    assert after[0].key != before[0].key


@pytest.mark.feature("lab-mutant-unit-unlisted-tests")
@pytest.mark.usefixtures("staticless")
def test_a_promotion_head_keys_a_mutant_unit_by_the_test_modules_the_manifest_lacks(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    expander = Port(SUITE_MANIFEST).expander(tmp_path)
    _git(suite.repo, "switch", "-q", "-c", "other", suite.base)
    changed = _commit(suite.repo, {"scripts/alpha.py": ALPHA.replace("return 1", "return 4")})
    added = _commit(suite.repo, {"tests/test_gamma.py": "def test_third():\n    pass\n"})
    edited = _commit(suite.repo, {"tests/test_gamma.py": "def test_third():\n    assert True\n"})
    found = [
        expander.expand(suite.request(Priority.PROMOTION, head)).gates["mutation"] for head in (changed, added, edited)
    ]
    assert [[executors.function_of(unit.members[0]) for unit in units] for units in found] == [[FIRST]] * 3
    assert len({units[0].key for units in found}) == 3


@pytest.mark.feature("lab-mutant-unit-unlisted-reads")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize("fixture", ["tests/fixtures/gamma.json", "scripts/gamma.json"])
def test_a_promotion_head_rekeys_a_mutant_unit_when_a_file_a_test_module_the_manifest_lacks_reads_changes(
    tmp_path: Path, fixture: str
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    expander = Port(SUITE_MANIFEST).expander(tmp_path)
    _git(suite.repo, "switch", "-q", "-c", "other", suite.base)
    reads = (
        "from pathlib import Path\n\n\n"
        f"def test_third():\n    assert (Path(__file__).parents[1] / {fixture!r}).read_text()\n"
    )
    added = _commit(
        suite.repo,
        {"scripts/alpha.py": ALPHA.replace("return 1", "return 4"), GAMMA: reads, fixture: '{"value": 1}\n'},
    )
    edited = _commit(suite.repo, {fixture: '{"value": 2}\n'})
    found = [expander.expand(suite.request(Priority.PROMOTION, head)).gates["mutation"] for head in (added, edited)]
    assert [[executors.function_of(unit.members[0]) for unit in units] for units in found] == [[FIRST]] * 2
    assert found[0][0].key != found[1][0].key


@pytest.mark.feature("lab-scheduler-mutation-units")
@pytest.mark.usefixtures("staticless")
def test_a_promotion_head_expands_no_stats_unit_for_a_covering_test_file_it_deletes(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    head = _drop(suite.repo, "tests/test_beta.py")
    found = Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(Priority.PROMOTION, head))
    mutation = _mutation_gates(suite, head, {"scripts/alpha.py": [6]}, [], {SECOND: [SECOND_TEST]})
    assert list(mutation) == ["mutation"]
    assert found.gates == {"unit": _suite_units(suite, head, SUITE_DURATIONS), **mutation}


@pytest.mark.feature("lab-scheduler-mutation-units")
@pytest.mark.usefixtures("staticless")
def test_a_promotion_head_expands_the_stats_unit_of_a_covering_test_inside_a_class(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    test = "tests/test_beta.py::TestBeta::test_second"
    tests = {FIRST_TEST: SUITE_MANIFEST["tests"][FIRST_TEST], test: {"units": [1], "files": [], "escapes": False}}
    found = Port({**SUITE_MANIFEST, "tests": tests}).expander(tmp_path).expand(suite.request(Priority.PROMOTION))
    assert [(unit.kind, unit.members) for unit in found.gates["mutation-stats"]] == [("stats", ("tests/test_beta.py",))]
    assert [executors.function_of(unit.members[0]) for unit in found.gates["mutation"]] == [SECOND]


@pytest.mark.feature("lab-scheduler-mutation-units")
@pytest.mark.usefixtures("staticless")
def test_a_promotion_head_expands_a_mutant_unit_for_a_changed_function_no_recorded_test_ran(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    head = _commit(suite.repo, {"scripts/alpha.py": ALPHA.replace("2", "3") + "\n\ndef third():\n    return 3\n"})
    found = Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(Priority.PROMOTION, head))
    mutation = _mutation_gates(
        suite, head, {"scripts/alpha.py": [6, 7, 8, 9, 10]}, ["tests/test_beta.py"], {SECOND: [SECOND_TEST]}
    )
    assert [executors.function_of(unit.members[0]) for unit in mutation["mutation"]] == [
        SECOND,
        "scripts.alpha.x_third",
    ]
    assert found.gates == {"unit": _suite_units(suite, head, SUITE_DURATIONS), **mutation}


@pytest.mark.feature("lab-scheduler-mutation-units")
@pytest.mark.usefixtures("staticless")
def test_a_promotion_head_that_changes_no_measured_python_expands_no_mutation_units(tmp_path: Path) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    _git(suite.repo, "switch", "-q", "-c", "other", suite.base)
    head = _commit(suite.repo, {"docs/q.md": "q\n", "tests/test_beta.py": "def test_second():\n    assert True\n"})
    found = Port(SUITE_MANIFEST).expander(tmp_path).expand(suite.request(Priority.PROMOTION, head))
    assert found.gates == {"unit": _suite_units(suite, head, SUITE_DURATIONS)}


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


def unfetched(slug: str, commit: str) -> bytes | None:
    return None


def packed_file(manifest: dict[str, Any], commit: str) -> bytes:
    found: dict[str, Any] = dep_manifest.pack({**manifest, "commit": commit})
    return json.dumps(found, separators=(",", ":")).encode()


@dataclass
class Fetches:
    found: dict[str, bytes]
    asked: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def __call__(self, slug: str, commit: str) -> bytes | None:
        self.asked.append((slug, commit))
        return self.found.get(commit)


@pytest.mark.feature("lab-scheduler-test-units")
def test_stored_manifests_read_the_packed_manifest_of_the_nearest_stored_ancestor(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path / "objects")
    for commit in ("older", "newer"):
        packed = dep_manifest.pack({**STORED_MANIFEST, "commit": commit})
        objects.write_once(f"{manifest_folder(SLUG)}{commit}.json", json.dumps(packed).encode())
    objects.write_once(f"{manifest_folder('other/repo')}head.json", b"{}")
    stored = scheduler.StoredManifests(objects, unfetched)
    assert stored(REPO, ["head", "newer", "older"]) == {**STORED_MANIFEST, "commit": "newer"}
    assert stored(REPO, ["head", "older"]) == {**STORED_MANIFEST, "commit": "older"}
    assert stored(REPO, ["head"]) is None
    assert manifest_folder(SLUG) == "lab/manifests/the-cloud-clockwork--agentihooks/"


@pytest.mark.feature("lab-verdict-outside-to-manifest")
def test_a_note_joins_the_outside_list_a_stored_or_fetched_record_already_holds(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path / "objects")
    objects.write_once(f"{manifest_folder(SLUG)}older.json", packed_file(SUITE_MANIFEST, "older"))
    stored = scheduler.StoredManifests(objects, Fetches({"newer": packed_file(SUITE_MANIFEST, "newer")}))
    assert stored(REPO, ["newer"]) == {**SUITE_MANIFEST, "commit": "newer"}
    for commit in ("newer", "older"):
        scheduler.note_outside(
            objects, REPO, commit, (SECOND_TEST, "tests/test_gone.py::test_gone"), ["docs/p.md", "docs/a.md"]
        )
    records = SUITE_MANIFEST["tests"]
    second = {**records[SECOND_TEST], "outside": ["docs/a.md", "docs/c.md", "docs/p.md"]}
    for commit in ("newer", "older"):
        assert stored(REPO, [commit]) == {**SUITE_MANIFEST, "commit": commit, "tests": {**records, SECOND_TEST: second}}


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
    first, twin, failed, plain = (
        Unit(expand_tests.KIND, key(expand_tests.KIND, number), tests)
        for number, tests in ((1, members), (2, members), (3, (FIRST_TEST,)), (4, (FIRST_TEST,)))
    )
    run = lab({HEAD_A: replace(plan({"unit": (first, twin, failed, plain)}), manifest="dev")})
    folder = manifest_folder(SLUG)
    packed = dep_manifest.pack(copy.deepcopy(SUITE_MANIFEST))
    run.lab.objects.write_once(f"{folder}dev.json", json.dumps(packed).encode())
    stored = scheduler.StoredManifests(run.lab.objects)
    before = stored(REPO, ["dev"])
    run.push(HEAD_A)
    run.tick(1000)
    grants = {grant.unit: grant for grant in run.lease(slots=4)}
    run.finish(grants[first], "passed", {"seconds": 1.0, "outside": ["docs/p.md", "docs/c.md"]})
    run.finish(grants[twin], "passed", {"seconds": 1.0, "outside": ["docs/c.md", "docs/p.md"]})
    run.finish(grants[failed], "failed", {"seconds": 1.0, "outside": ["docs/c.md"]})
    run.finish(grants[plain], "passed", {"seconds": 1.0, "outside": []})
    run.tick(1100)
    after = stored(REPO, ["dev"])
    records = SUITE_MANIFEST["tests"]
    both = ["docs/c.md", "docs/p.md"]
    assert before == SUITE_MANIFEST
    assert after == {
        **SUITE_MANIFEST,
        "tests": {
            FIRST_TEST: {**records[FIRST_TEST], "outside": ["docs/c.md"]},
            OTHER_TEST: {**records[OTHER_TEST], "outside": both},
            SECOND_TEST: {**records[SECOND_TEST], "outside": both},
        },
    }
    notes = [
        f"{folder}outside/dev/{digest({'tests': list(tests), 'outside': files})}.json"
        for tests, files in ((members, both), ((FIRST_TEST,), ["docs/c.md"]))
    ]
    assert run.lab.objects.keys(folder) == sorted([f"{folder}dev.json", *notes])
    change = [[("docs/p.md", "p\n", "q\n")]]
    assert before is not None and after is not None
    assert interaction.invalidated(before, change) == frozenset()
    assert interaction.invalidated(after, change) == {OTHER_TEST, SECOND_TEST}
    assert _batch_keys(before, b"p\n") == _batch_keys(before, b"q\n")
    assert _batch_keys(after, b"p\n")[(FIRST_TEST, OTHER_TEST)] != _batch_keys(after, b"q\n")[(FIRST_TEST, OTHER_TEST)]


@pytest.mark.feature("lab-outside-notes-per-commit")
def test_a_note_of_an_older_manifest_commit_leaves_a_newer_commit_key_unchanged(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path / "objects")
    folder = manifest_folder(SLUG)
    for commit in ("older", "newer"):
        objects.write_once(f"{folder}{commit}.json", packed_file(SUITE_MANIFEST, commit))
    note: dict[str, object] = {"tests": [FIRST_TEST, OTHER_TEST], "outside": ["docs/p.md"]}
    objects.write_once(f"{folder}outside/older/{digest(note)}.json", json.dumps(note).encode())
    stored = scheduler.StoredManifests(objects, unfetched)
    newer, older = stored(REPO, ["newer", "older"]), stored(REPO, ["older"])
    records = SUITE_MANIFEST["tests"]
    widened = {test: {**records[test], "outside": ["docs/p.md"]} for test in (FIRST_TEST, OTHER_TEST)}
    assert newer == {**SUITE_MANIFEST, "commit": "newer"}
    assert older == {**SUITE_MANIFEST, "commit": "older", "tests": {**records, **widened}}
    assert _batch_keys(newer, b"p\n") == _batch_keys(newer, b"q\n")
    assert _batch_keys(older, b"p\n")[(FIRST_TEST, OTHER_TEST)] != _batch_keys(older, b"q\n")[(FIRST_TEST, OTHER_TEST)]


@pytest.mark.feature("lab-outside-notes-per-commit")
def test_a_manifest_read_removes_the_notes_of_commits_with_no_stored_manifest(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path / "objects")
    folder, other = manifest_folder(SLUG), manifest_folder("other/repo")
    objects.write_once(f"{folder}kept.json", packed_file(STORED_MANIFEST, "kept"))
    note = json.dumps({"tests": [FIRST_TEST], "outside": ["docs/p.md"]}).encode()
    kept, gone, flat = f"{folder}outside/kept/n.json", f"{folder}outside/gone/n.json", f"{folder}outside/n.json"
    for path in (kept, gone, flat, f"{other}outside/gone/n.json"):
        objects.write_once(path, note)
    found = scheduler.StoredManifests(objects, unfetched)(REPO, ["kept"])
    first = {**STORED_MANIFEST["tests"][FIRST_TEST], "outside": ["docs/p.md"]}
    assert found == {**STORED_MANIFEST, "commit": "kept", "tests": {FIRST_TEST: first}}
    assert objects.keys(folder) == [f"{folder}kept.json", kept]
    assert objects.keys(other) == [f"{other}outside/gone/n.json"]
    assert scheduler.StoredManifests(objects, unfetched)(REPO, ["missing"]) is None
    assert objects.keys(folder) == [f"{folder}kept.json", kept]


@pytest.mark.feature("lab-outside-notes-per-commit")
def test_a_grade_that_keyed_no_manifest_writes_no_outside_note(lab: Callable[..., Run]) -> None:
    batch = Unit(expand_tests.KIND, key(expand_tests.KIND, 1), (FIRST_TEST,))
    run = lab({HEAD_A: plan({"unit": (batch,)})})
    run.push(HEAD_A)
    run.tick(1000)
    (grant,) = run.lease(slots=1)
    run.finish(grant, "passed", {"seconds": 1.0, "outside": ["docs/p.md"]})
    run.tick(1100)
    assert run.store.get(batch.kind, batch.key) is not None
    assert run.lab.objects.keys(manifest_folder(SLUG)) == []


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
    registry: metrics.Registry | None = None


@pytest.mark.feature("lab-worker-deletion-cost")
def test_build_wires_the_service_ports_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler.MirrorExpander, "trusted", lambda self, repo: frozenset({repo}))
    monkeypatch.setattr(scheduler.MirrorExpander, "retries", lambda self, repo: Policy(lost=len(repo)))
    monkeypatch.setattr(scheduler, "Checks", FakeChecks)
    kube = FakeKube()
    pods_of: list[Mapping[str, str]] = []

    def worker_pods(found: Mapping[str, str]) -> FakeKube:
        pods_of.append(found)
        return kube

    monkeypatch.setattr(scheduler, "worker_pods", worker_pods)
    environ = {
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: "repository",
        scheduler.IMAGE: "image",
    }
    client = fake_redis()
    consumer = Consumer([])
    objects = LocalObjects(tmp_path / "objects")
    spending = budget.Budget(client, lambda: 0, budget.Policy(100.0, {}))
    built = scheduler.build(environ, client, objects, consumer, spending)
    assert built.spending is spending
    assert built.ports.pods is kube and pods_of == [environ]
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
    background = expander.manifests.fetch
    assert isinstance(background, manifests.Background)
    fetch = background.fetch
    assert isinstance(fetch, Fetcher) and fetch.reader is Actions and fetch.readers == {}
    stored = Verdict("static", key("static", 9), True, False, 1.0)
    Store(client, objects).put(stored, {}, b"")
    assert expander.verdicts("static", stored.key) == stored
    imageless = {name: value for name, value in environ.items() if name != scheduler.IMAGE}
    bare = scheduler.build(imageless, client, objects, consumer, spending).ports.expander
    assert isinstance(bare, scheduler.MirrorExpander) and bare.toolchain.image == ""
    first = built.ports.checks(SLUG)
    assert first == FakeChecks(SLUG, built.registry) and built.ports.checks(SLUG) is first
    assert built.ports.checks("other/repo") == FakeChecks("other/repo", built.registry)
    hotfixes = built.ports.hotfixes
    assert isinstance(hotfixes, control.Priorities)
    assert (hotfixes.client, hotfixes.clock) == (client, scheduler._now_ms)


@pytest.mark.feature("lab-remote-heads")
def test_build_gives_each_expander_mirror_the_lab_remote_the_environment_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler.MirrorExpander, "trusted", lambda self, repo: frozenset({repo}))
    monkeypatch.setattr(scheduler.MirrorExpander, "retries", lambda self, repo: Policy())
    monkeypatch.setattr(scheduler, "worker_pods", lambda found: FakeKube())
    environ = {
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: "repository",
        "AGENTIHOOKS_LAB_REMOTE": "http://ci-lab-remote.ci-lab:8080/agentihooks.git",
        "AGENTIHOOKS_LAB_REMOTE_TOKEN": "lab-token",
    }
    client = fake_redis()
    spending = budget.Budget(client, lambda: 0, budget.Policy(100.0, {}))
    objects = LocalObjects(tmp_path / "objects")
    built = scheduler.build(environ, client, objects, Consumer([]), spending)
    expander = built.ports.expander
    assert isinstance(expander, scheduler.MirrorExpander)
    verdicts = getattr(expander.verdicts, "__self__")
    assert isinstance(verdicts, Store) and (verdicts.client, verdicts.objects) == (client, objects)
    found = worktrees.Lab("http://ci-lab-remote.ci-lab:8080/agentihooks.git", "lab-token")
    store = tmp_path / "stores" / "the-cloud-clockwork--agentihooks"
    assert expander._mirror(REPO) == worktrees.Mirror(tmp_path / "node", store, REPO, found)


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
            calls.setdefault("consumers", []).append((durable, subject))
            return f"{durable} consumer"

    class Bus:
        lab_heads = Events()

    def build(environ: Any, redis_client: Any, objects: Any, consumer: Any, spending: Any) -> scheduler.Scheduler:
        calls["build"] = (environ, redis_client, objects, consumer, spending)
        ports = scheduler.Ports(Inbox(), Expander({}), lambda repo: Poster(), frozenset[str], FakeKube())
        return scheduler.Scheduler(redis_client, objects, ports, scheduler.Settings(frozenset()), spending)

    class Server:
        def serve_forever(self) -> None:
            calls["served"] += 1

    def listen(
        found: Any,
        leases: Leases,
        routes: lease_api.ControlRoutes,
        spending: Any,
        clock: Any,
        ports: tuple[int, int, int],
        mirror: Path,
    ) -> list[Server]:
        calls["listen"] = (found, leases, routes, spending, clock, ports, mirror)
        return [Server(), Server(), Server()]

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

    builds = RelayBuilds()

    def serve(found: scheduler.Scheduler, relaying: Any, interval: float, passes: int, clock: Any, sleep: Any) -> int:
        calls["serve"] = (found, relaying, interval, passes, clock, sleep)
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
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
    } | APP_SETTINGS
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
    monkeypatch.setattr(relay, "build", builds)
    assert scheduler.main(["--interval", "0.5", "--passes", "3"], environ) == 7
    assert calls["redis"] == ("redis://lab", {"decode_responses": True})
    name, options = calls["s3"]
    signing = options.pop("config")
    assert (name, options) == (
        "s3",
        {
            "endpoint_url": "endpoint",
            "region_name": "us-east-1",
            "aws_access_key_id": "access",
            "aws_secret_access_key": "secret",
        },
    )
    assert (signing.signature_version, signing.s3) == ("s3v4", {"addressing_style": "path"})
    built_from, redis_client, objects, consumer, built_spending = calls["build"]
    assert (built_from, redis_client, consumer) == (environ, client, "ci-lab-scheduler consumer")
    assert (objects.client, objects.bucket, objects.prefix) == ("s3 client", "bucket", "")
    assert calls["consumers"] == [("ci-lab-scheduler", "ci.lab.head.>"), ("ci-lab-relay", "ci.lab.head.notice")]
    relayed_from, relayed, relay_consumer = builds.builds[0]
    assert (relayed_from, relayed.queue.client, relay_consumer) == (environ, client, "ci-lab-relay consumer")
    found, leases, routes, spending, clock, ports, mirror = calls["listen"]
    assert mirror == tmp_path / "node"
    assert found.queue.client is client and leases.queue is found.queue
    assert leases.pods is found.ports.pods
    assert isinstance(leases.manifests, partial)
    assert (leases.manifests.func, leases.manifests.args) == (scheduler.stored_manifest, (found.objects,))
    assert routes == lease_api.ControlRoutes(client, "tok", scheduler._now_ms)
    assert (leases.ttl_ms, leases.poll_ms) == (scheduler.LEASE_TTL_MS, scheduler.LEASE_POLL_MS)
    assert leases.clock() == 1_000_250 and clock() == 1_000_250
    leases.sleep(1500)
    assert Clock.slept == [1.5]
    assert spending.client is client and spending.clock() == 1_000_250
    assert spending.policy == budget.Policy(600, {"backfill": 0.5})
    assert built_spending is spending and found.spending is spending
    assert ports == (8800, 8801, 9108)
    assert calls["thread"] is True and calls["served"] == 3
    found, relaying, interval, passes, clock, sleep = calls["serve"]
    assert found is relayed and relaying == "relay"
    assert (found.queue.client, interval, passes, clock, sleep) == (client, 0.5, 3, Clock.time, Clock.sleep)
    assert scheduler.main([], environ) == 7
    assert calls["serve"][2:4] == (scheduler.INTERVAL_S, 0)
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

    def build(_environ: Any, redis_client: Any, _objects: Any, _consumer: Any, _spending: Any) -> scheduler.Scheduler:
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
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
        **APP_SETTINGS,
    } | setting
    stdout = Stdout()
    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    monkeypatch.setattr(boto3, "client", s3)
    monkeypatch.setattr(config.BusConfig, "from_environ", classmethod(from_environ))
    monkeypatch.setattr(factory, "open_bus", open_bus)
    monkeypatch.setattr(scheduler, "build", build)
    monkeypatch.setattr(scheduler, "listen", listen)
    monkeypatch.setattr(scheduler, "serve", serve)
    monkeypatch.setattr(relay, "build", RelayBuilds())
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


def flake_rows(item: Unit, *tests: str) -> list[dict[str, Any]]:
    return [{"test": test, "run": item.key, "run_attempt": 2, "attempts": 2, "outcome": "flaky"} for test in tests]


def flakes_at(run: Run, item: Unit) -> bytes:
    assert run.lab.objects.keys("lab/flakes/") == [flakes_path(item.kind, item.key)]
    return run.lab.objects.read(flakes_path(item.kind, item.key))


@pytest.mark.feature("lab-flakes-object-once")
def test_only_a_passed_flaky_unit_with_flake_rows_writes_its_flakes_object(lab: Callable[..., Run]) -> None:
    flaky, steady, failed, rowless, empty = (unit(number) for number in range(1, 6))
    run = lab({HEAD_A: plan({"lint": (flaky, steady, failed, rowless, empty)})})
    run.push(HEAD_A)
    run.tick(1000)
    grants = {grant.unit: grant for grant in run.lease(slots=5)}
    rows = flake_rows(flaky, "tests/test_a.py::test_one", "tests/test_a.py::test_two")
    run.finish(grants[flaky], "passed", {"flaky": True, "seconds": 3.5, "flakes": rows})
    run.finish(grants[steady], "passed", {"flaky": False, "flakes": flake_rows(steady, "t")})
    run.finish(grants[failed], "failed", {"flaky": True, "flakes": flake_rows(failed, "t")})
    run.finish(grants[rowless], "passed", {"flaky": True})
    run.finish(grants[empty], "passed", {"flaky": True, "flakes": []})
    run.tick(1100)
    assert flakes_at(run, flaky) == "".join(json.dumps(row) + "\n" for row in rows).encode()
    assert run.store.get(flaky.kind, flaky.key) == Verdict(flaky.kind, flaky.key, True, True, 3.5)


@pytest.mark.feature("lab-flakes-object-once")
def test_a_second_remember_of_a_flaky_unit_keeps_the_first_flakes_object_and_verdict(lab: Callable[..., Run]) -> None:
    flaky = unit(1)
    first, second = (lab({head: plan({"lint": (flaky,)})}) for head in (HEAD_A, HEAD_B))
    for run, head, seconds, test in ((first, HEAD_A, 3.5, "tests/test_a.py::test_one"), (second, HEAD_B, 9.0, "t")):
        run.push(head)
        run.tick(1000)
        (grant,) = run.lease()
        run.finish(grant, "passed", {"flaky": True, "seconds": seconds, "flakes": flake_rows(flaky, test)})
        run.tick(1100)
        assert run.grade(head).conclusion == "success"
    assert json.loads(flakes_at(second, flaky)) == flake_rows(flaky, "tests/test_a.py::test_one")[0]
    assert second.store.get(flaky.kind, flaky.key) == Verdict(flaky.kind, flaky.key, True, True, 3.5)
