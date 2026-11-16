import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from scripts.ci_lab import __main__ as cli
from scripts.ci_lab import scheduler
from scripts.ci_lab.checks import Output
from scripts.ci_lab.digests import StaticInputs, Toolchain, static_key
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.leases import Grant, Leases
from scripts.ci_lab.objects import LocalObjects, unit_path, upload_path
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.static_gates import StaticGate
from scripts.ci_lab.store import Store, Verdict
from scripts.ci_lab.units import Priority, Unit
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.events import bus
from scripts.events.envelope import Envelope
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


def key(kind: str, number: int) -> str:
    return f"{kind}-v1-{number:064x}"


def unit(number: int, kind: str = "static") -> Unit:
    return Unit(kind, key(kind, number), (f"gate-{number}",))


def ref(item: Unit) -> str:
    return f"{item.kind}:{item.key}"


def plan(gates: dict[str, tuple[Unit, ...]], tree: str = TREE) -> scheduler.Plan:
    return scheduler.Plan(tree, BASE, gates)


def pull_plan(gates: dict[str, tuple[Unit, ...]]) -> scheduler.Plan:
    required: dict[str, tuple[Unit, ...]] = {job: () for job in scheduler.PULL_REQUIRED}
    return scheduler.Plan(TREE, BASE, {**required, **gates}, outputs={"select": {"scope": "none"}})


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
        client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
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
    assert run.engine.queue.pending() == []
    assert grade.conclusion == "success"
    assert run.lab.poster.moves(HEAD_A, "lint") == [("completed", "success")]
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED) == [("completed", "success")]
    assert run.lab.client.smembers(scheduler.LIVE) == set()
    assert f"grading {grade.run} with 0 of 2 units enqueued" in actions
    assert f"completed {grade.run} success" in actions
    assert run.lab.repos == [SLUG]


@pytest.mark.feature("lab-scheduler-grade")
def test_a_newer_head_drops_pending_units_and_reuses_carried_verdicts(lab: Callable[..., Run]) -> None:
    running, finished, waiting, fresh = unit(1), unit(2), unit(3), unit(4)
    run = lab(
        {
            HEAD_A: plan({"semgrep": (finished,), "lint": (running, waiting)}),
            HEAD_B: plan({"semgrep": (finished,), "lint": (running, fresh)}),
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
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, fresh)]
    assert new.units[ref(running)].job == grants[1].job
    assert new.units[ref(finished)].outcome == "passed"
    assert new.units[ref(finished)].job == ""
    assert run.lab.poster.moves(HEAD_A, "semgrep")[-1] == ("completed", "success")
    assert run.lab.poster.moves(HEAD_A, "lint")[-1] == ("completed", "cancelled")
    assert run.lab.poster.moves(HEAD_A, scheduler.REQUIRED)[-1] == ("completed", "cancelled")
    run.finish(grants[1], "passed", {})
    run.finish(run.lease()[0], "passed", {})
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
    assert run.tick(1100) == [f"posted lint in_progress on {HEAD_A}", f"posted required in_progress on {HEAD_A}"]
    assert f"reaped {first.job} pending" in run.tick(1101)
    job = run.engine.queue.job(first.job)
    assert job is not None and (job.state, job.attempts) == ("pending", 1)
    second = run.lease(ttl_ms=100)[0]
    assert second.job == first.job
    assert f"reaped {second.job} lost" in run.tick(1300)
    assert run.grade(HEAD_A).conclusion == "failure"
    assert run.lab.poster.moves(HEAD_A, "lint")[-1] == ("completed", "failure")
    assert run.store.get(item.kind, item.key) is None


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


def test_an_untrusted_head_is_never_held_or_graded(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    run.push(HEAD_A, actor="stranger")
    assert run.tick(1000) == [f"untrusted stranger head {HEAD_A}"]
    assert run.lab.expander.seen == []
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


def test_a_dev_push_grade_reuses_the_merge_group_grade_of_the_same_tree(lab: Callable[..., Run]) -> None:
    queued, pushed, other = unit(1), unit(2), unit(3)
    run = lab(
        {
            HEAD_A: plan({"lint": (queued,)}),
            HEAD_B: plan({"lint": (pushed,)}),
            BASE: plan({"lint": (other,)}, tree=OTHER_TREE),
        }
    )
    run.push(HEAD_A, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.tick(1000)
    run.finish(run.lease()[0], "passed", {})
    run.tick(1100)
    queue = run.grade(HEAD_A)
    assert run.lab.client.get(f"{scheduler.TREES}{REPO}|{TREE}") == queue.run
    run.push(HEAD_B, lane="dev")
    run.tick(1200)
    pushed_grade = run.grade(HEAD_B)
    assert run.engine.queue.pending() == []
    assert pushed_grade.gates == {"lint": [ref(queued)]}
    assert pushed_grade.conclusion == "success"
    assert run.lab.client.get(f"{scheduler.TREES}{REPO}|{TREE}") == queue.run
    run.push(BASE, lane="dev")
    run.tick(1300)
    assert run.engine.queue.pending() == [scheduler.job_id(BASE, other)]


def test_a_merge_group_grade_of_a_tree_serves_no_other_tier(lab: Callable[..., Run]) -> None:
    queued, pulled = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (queued,)}), HEAD_B: plan({"lint": (pulled,)})})
    run.push(HEAD_A, lane="gh-readonly-queue/dev/pr-1", event="merge_group", action="checks_requested")
    run.tick(1000)
    run.finish(run.lease()[0], "passed", {})
    run.tick(1100)
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


def test_a_head_whose_expansion_fails_is_dropped_with_its_reason(lab: Callable[..., Run]) -> None:
    run = lab({})
    run.push(HEAD_A)
    actions = run.tick(1000)
    assert actions[-1].startswith(f"cannot expand {HEAD_A}: ")
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []


def test_a_live_run_without_a_grade_is_forgotten(lab: Callable[..., Run]) -> None:
    run = lab({})
    run.lab.client.sadd(scheduler.LIVE, "grade-v1-gone")
    assert run.tick(1000) == ["forgot grade-v1-gone"]
    assert run.lab.client.smembers(scheduler.LIVE) == set()


def test_serve_runs_the_given_passes_and_prints_each_action(
    lab: Callable[..., Run], capsys: pytest.CaptureFixture[str]
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=60_000)
    run.push(HEAD_A)
    slept: list[float] = []
    assert scheduler.serve(run.engine, 0.25, 2, lambda: 2.5, slept.append) == 0
    assert slept == [0.25, 0.25]
    assert capsys.readouterr().out == f"received {HEAD_A} on feature\n"
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

    def ack(self) -> None:
        self.acked = True

    def nak(self, delay_ms: int = 0) -> None:
        raise AssertionError(delay_ms)

    def term(self) -> None:
        raise AssertionError(self.event.id)


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
    assert consumer.asked == [(scheduler.BATCH, scheduler.FETCH_MS)]


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
            "uv.lock": "lock",
            "pyproject.toml": "[project]\n",
            "src/a.py": "a = 1\n",
            "srcx/b.py": "b = 1\n",
            "docs/c.md": "c\n",
        },
    )
    head = _commit(repo, {"src/a.py": "a = 2\n", "uv.lock": "lock 2"})
    return repo, base, head


def _gate(name: str, job: str, paths: tuple[str, ...], base_paths: tuple[str, ...]) -> StaticGate:
    return StaticGate(name, job, name, (), (), "", paths, base_paths)


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
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain)
    remote = f"file://{repo}"
    request = GradeRequest(remote, head, base, Priority.PULL_REQUEST, ACTOR, False)
    found = expander.expand(request)
    chain = Toolchain("3.12", {}, _blob(repo, head, "uv.lock"), _blob(repo, head, "pyproject.toml"), "image", {})
    head_files = {path: _blob(repo, head, path) for path in _git(repo, "ls-tree", "-r", "--name-only", head).split()}
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
    assert found == scheduler.Plan(_git(repo, "rev-parse", f"{head}^{{tree}}").strip(), base, expected)
    assert read == [("jobs: {}\\n", {"gates": {}})]
    promoted = expander.expand(GradeRequest(remote, head, base, Priority.PROMOTION, ACTOR, False))
    assert promoted.grader == head


def test_the_mirror_expander_trusts_the_runner_trust_list_on_dev(tmp_path: Path, origin: tuple[Path, str, str]) -> None:
    repo, _, _ = origin
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", Toolchain("3.12", {}, "", "", "", {}))
    assert expander.trusted(f"file://{repo}") == frozenset([*ACTORS["actors"], ACTORS["queue_actor"]])


def test_panic_fixes_name_the_fix_branches_while_panic_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import panic
    from scripts.swarm.store import RedisStore, SwarmError

    client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    fixes = scheduler.PanicFixes(client)
    assert fixes() == frozenset()
    panic.engage(RedisStore(client), "outage", False, ["first", "second"], 1)
    monkeypatch.setattr(panic, "fix_branch", lambda url: f"fix-{url}")
    assert fixes() == frozenset({"fix-first", "fix-second"})

    def unreadable(url: str) -> str:
        raise SwarmError(url)

    monkeypatch.setattr(panic, "fix_branch", unreadable)
    assert fixes() == frozenset()


@dataclass
class FakeChecks:
    repo: str


def test_build_wires_the_service_ports_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler.MirrorExpander, "trusted", lambda self, repo: frozenset({repo}))
    monkeypatch.setattr(scheduler, "Checks", FakeChecks)
    environ = {
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: "repository",
        scheduler.IMAGE: "image",
    }
    client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    consumer = Consumer([])
    built = scheduler.build(environ, client, LocalObjects(tmp_path / "objects"), consumer, None)
    assert built.settings.trusted == frozenset({"repository"})
    assert built.ports.hotfixes() == frozenset()
    assert isinstance(built.ports.inbox, scheduler.BusInbox) and built.ports.inbox.consumer is consumer
    expander = built.ports.expander
    assert isinstance(expander, scheduler.MirrorExpander)
    assert (expander.node, expander.root, expander.toolchain.image) == (tmp_path / "node", tmp_path / "stores", "image")
    first = built.ports.checks(SLUG)
    assert first == FakeChecks(SLUG) and built.ports.checks(SLUG) is first
    assert built.ports.checks("other/repo") == FakeChecks("other/repo")
    swarm = object()
    hotfixes = scheduler.build(environ, client, LocalObjects(tmp_path / "objects"), consumer, swarm).ports.hotfixes
    assert isinstance(hotfixes, scheduler.PanicFixes) and hotfixes.redis is swarm


def test_main_serves_the_lease_api_and_runs_the_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3  # pyright: ignore[reportMissingTypeStubs]
    import redis

    from hooks import _redis
    from scripts.events import config, factory

    calls: dict[str, Any] = {}
    client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)

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

    def build(environ: Any, redis_client: Any, objects: Any, consumer: Any, swarm: Any) -> scheduler.Scheduler:
        calls["build"] = (environ, redis_client, objects, consumer, swarm)
        ports = scheduler.Ports(Inbox(), Expander({}), lambda repo: Poster(), frozenset[str])
        return scheduler.Scheduler(redis_client, objects, ports, scheduler.Settings(frozenset()))

    class Server:
        def serve_forever(self) -> None:
            calls["served"] = True

    def serve_api(host: str, port: int, leases: Leases, signer: Any) -> Server:
        calls["api"] = (host, port, leases, signer)
        return Server()

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

    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    monkeypatch.setattr(boto3, "client", s3)
    monkeypatch.setattr(config.BusConfig, "from_environ", classmethod(lambda cls, environ: "config"))
    monkeypatch.setattr(factory, "open_bus", lambda found: Bus() if found == "config" else None)
    monkeypatch.setattr(_redis, "get_redis", lambda: "swarm")
    monkeypatch.setattr(scheduler, "build", build)
    monkeypatch.setattr(scheduler.lease_api, "serve", serve_api)
    monkeypatch.setattr(scheduler, "Thread", Thread)
    monkeypatch.setattr(scheduler, "time", Clock)
    monkeypatch.setattr(scheduler, "serve", serve)
    environ = {
        scheduler.contract.REDIS_URL: "redis://lab",
        scheduler.MINIO_ENDPOINT: "endpoint",
        scheduler.MINIO_ACCESS_KEY: "access",
        scheduler.MINIO_SECRET_KEY: "secret",
        scheduler.MINIO_BUCKET: "bucket",
    }
    assert scheduler.main(["--interval", "0.5", "--passes", "3"], environ) == 7
    assert calls["redis"] == ("redis://lab", {"decode_responses": True})
    options = {"endpoint_url": "endpoint", "aws_access_key_id": "access", "aws_secret_access_key": "secret"}
    assert calls["s3"] == ("s3", options)
    built_from, redis_client, objects, consumer, swarm = calls["build"]
    assert (built_from, redis_client, consumer, swarm) == (environ, client, "consumer", "swarm")
    assert (objects.client, objects.bucket, objects.prefix) == ("s3 client", "bucket", "")
    assert calls["consumer"] == (scheduler.DURABLE, "ci.lab.head.>")
    host, port, leases, signer = calls["api"]
    assert (host, port, leases.ttl_ms, leases.poll_ms) == ("", 8800, scheduler.LEASE_TTL_MS, scheduler.LEASE_POLL_MS)
    assert signer.objects is objects and leases.queue.client is client
    assert leases.clock() == 1_000_250
    leases.sleep(1500)
    assert Clock.slept == [1.5]
    assert calls["thread"] is True and calls["served"] is True
    found, interval, passes, clock, sleep = calls["serve"]
    assert (found.queue.client, interval, passes, clock, sleep) == (client, 0.5, 3, Clock.time, Clock.sleep)
    assert scheduler.main([], environ) == 7
    assert calls["serve"][1:3] == (scheduler.INTERVAL_S, 0)


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
