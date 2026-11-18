"""The CI lab scheduler: `Scheduler.tick` takes admitted heads from intake to completed lab check runs.

One pass reads new head events, admits trusted actors, holds each lane through its quiet window (a hotfix waits for
none), expands a head into units per gate from the git mirror, answers units from the verdict store, enqueues the
misses, reaps expired leases (one requeue, then lost), collects finished jobs into the store, folds each gate's units
and then the gates by the tier's `Gate — Required` rules, and moves the check runs: queued on receipt, in_progress on
the first unit, completed on the last. A grade's state is the JSON document at `agentihooks-ci:grade:<run>`.

A newer head of a lane drops the older head's pending units, carries its leased and finished jobs and cancels its
checks; a push grade of a tree a merge group grade passed carries that grade's gates. A head or lane the control
routes marked hotfix grades in the hotfix class, and a paused class holds its heads but the excepted ones until
resume. `agentihooks ci-lab-scheduler` runs the service: the lease API with its control routes, capacity and a
commit's grades on the scheduler port, metrics on the metrics port, both from one load, and one pass every interval.
"""

import argparse
import io
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from threading import Thread
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import jsonschema
import yaml

from scripts import ci_select, dep_manifest
from scripts.ci_lab import (
    budget,
    caches,
    contract,
    control,
    evidence,
    expand_tests,
    interaction,
    lease_api,
    metrics,
    service,
)
from scripts.ci_lab.capacity import Load
from scripts.ci_lab.checks import Checks, Output
from scripts.ci_lab.delivery import CLASSES, DELIVERY, DELIVERY_SCHEMA, load_runtime
from scripts.ci_lab.digests import GradeInputs, StaticInputs, Toolchain, digest, grade_key, static_key
from scripts.ci_lab.intake import GradeRequest, admit
from scripts.ci_lab.leases import Leases, release
from scripts.ci_lab.manifests import Fetcher
from scripts.ci_lab.objects import Objects, S3Objects, Uploads, manifest_folder, upload_path
from scripts.ci_lab.outcomes import CANNOT_RUN, DROPPED, FAILED, LOSSES, PASSED
from scripts.ci_lab.pods import Pods, unmanaged, worker_pods
from scripts.ci_lab.queue import DEFAULT, Job, Queue
from scripts.ci_lab.retry import Policy, UnitResult, decide, policy
from scripts.ci_lab.static_gates import REGISTRY, WORKFLOW, discover
from scripts.ci_lab.store import PREFIX, Store, Verdict
from scripts.ci_lab.units import Priority, Unit
from scripts.ci_lab.webhooks import HEAD_SUBJECT, HeadEvent
from scripts.ci_lab.worktrees import Mirror
from scripts.ci_mutation import scope
from scripts.ci_runner_trust import ACTORS, validate_actors
from scripts.events.bus import Consumer

__all__ = [
    "GRADES",
    "BusInbox",
    "Flags",
    "Head",
    "MirrorExpander",
    "Plan",
    "Ports",
    "READY_MS",
    "Scheduler",
    "Settings",
    "StoredManifests",
    "UNIT_ESTIMATE_S",
    "build",
    "fold",
    "frame",
    "listen",
    "main",
    "serve",
    "summary",
]

GRADES = f"{PREFIX}grade:"
LIVE = f"{PREFIX}grades"
LANES = f"{PREFIX}lane:"
WAITING = f"{PREFIX}waiting"
TREES = f"{PREFIX}tree:"
UNGRADED = frozenset({*LOSSES, CANNOT_RUN, DROPPED})
FINISHED = frozenset({"done", "lost"})
REAPED = {"pending": contract.UNITS_REDELIVERED_TOTAL, "lost": contract.UNITS_LOST_TOTAL}
REQUIRED = "required"
EVENTS = ("pull_request", "pull_request", "merge_group", "push", "workflow_dispatch")
GRADED_EVENTS = frozenset({"merge_group", "pull_request"})
QUEUE_REQUIRED = ("select", "split", "shard-check", "test-count")
PULL_REQUIRED = ("select", "test-count", "lint", "type-gate", "docs", "semgrep", "sonar", "writer-lock")
SCOPED = {"full": ("split", "unit", "shard-check"), "selected": ("unit-selected",)}
SCOPES = frozenset({"full", "selected", "none"})
UNIT_JOBS = ("unit", "unit-lost", "unit-rerun")
UNIT_ROWS = frozenset(
    {("success", "skipped", "skipped"), ("failure", "success", "success"), ("cancelled", "success", "success")}
)
FAIL_FAST = "queue-fail-fast"
FAIL_FAST_RESULTS = frozenset({"success", "failure", "cancelled"})
MUTATION_JOBS = frozenset({"mutation", "mutation-plan", "mutation-stats"})
STATS_JOB = "mutation-stats"
MUTANT_JOB = "mutation"
ALWAYS_SKIPPABLE = frozenset({"unit-lost", "unit-rerun", "queue-baseline", "unit-selected"})
PUSH_SKIPPABLE = frozenset({"type-gate", "lint", "semgrep", "test-count"})
HEAD_FIELDS = tuple(item.name for item in fields(HeadEvent))
STATIC = "static"
WHOLE_TREE = "."
DURATIONS = ".test_durations"
NOTES = "outside/"
DEV = "dev"
DURABLE = "ci-lab-scheduler"
SUBJECT = f"{HEAD_SUBJECT}.>"
BATCH = 50
FETCH_MS = 100
QUIET_MS = 30_000
INTERVAL_S = 2.0
LEASE_TTL_MS = 120_000
LEASE_POLL_MS = 500
UNIT_ESTIMATE_S = 60.0
READY_MS = 2 * lease_api.MAX_WAIT_MS
MINIO_ENDPOINT = f"{contract.MINIO_PREFIX}ENDPOINT"
MINIO_BUCKET = f"{contract.MINIO_PREFIX}BUCKET"
MINIO_ACCESS_KEY = f"{contract.MINIO_PREFIX}ACCESS_KEY"
MINIO_SECRET_KEY = f"{contract.MINIO_PREFIX}SECRET_KEY"
MIRROR_STORE = "CI_LAB_MIRROR_STORE"
IMAGE = "CI_LAB_IMAGE"
REPOSITORY = "CI_LAB_REPOSITORY"
REINDEX_ON_START = "CI_LAB_REINDEX_ON_START"
MANIFEST_DEPTH = 10

Needs = Mapping[str, Mapping[str, Any]]
Manifests = Callable[[str, Sequence[str]], dict[str, Any] | None]
Fetch = Callable[[str, str], bytes | None]
Verdicts = Callable[[str, str], Verdict | None]
UNPACK = cast("Callable[[dict[str, Any]], dict[str, Any]]", dep_manifest.unpack)


@dataclass(frozen=True)
class Flags:
    """The Gate — Required step's inputs besides the jobs, as the workflow passes them: true, false or empty."""

    mutation: str = "false"
    inline: str = "false"
    kind: str = "false"
    reused: str = "false"


def _alternative(value: Any, default: Any) -> Any:
    """Return jq's `value // default`: the default when the value is null or false."""
    return default if value is None or value is False else value


def _result(needs: Needs, job: str) -> Any:
    """Return a job's result, null when the job or its result is absent."""
    return (needs.get(job) or {}).get("result")


def _outputs(needs: Needs, job: str) -> Mapping[str, Any]:
    """Return a job's outputs, empty when the job or its outputs are absent."""
    return _alternative((needs.get(job) or {}).get("outputs"), {})


def _forwarded(needs: Needs) -> dict[str, Mapping[str, Any]]:
    """Count skipped mutation stats as success when the mutation plan forwarded them."""
    jobs = dict(needs)
    forwarded = (
        _result(needs, "mutation-plan") == "success" and _outputs(needs, "mutation-plan").get("forwarded") == "true"
    )
    if forwarded and _result(needs, "mutation-stats") == "skipped":
        jobs["mutation-stats"] = {**needs["mutation-stats"], "result": "success"}
    return jobs


def _authoritative(verdict: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the lab verdict job's authoritative gates, or None when they are not one JSON object."""
    raw = _alternative(_alternative(verdict.get("outputs"), {}).get("authoritative"), "")
    if raw == "":
        return {}
    if not isinstance(raw, str):
        return None
    try:
        gates = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return cast(dict[str, Any], gates) if isinstance(gates, dict) else None  # pragma: no mutate


def _lab(event: str, needs: Needs) -> dict[str, Mapping[str, Any]] | None:
    """Return the needs without the lab verdict job, its authoritative results taken on a pull request, or None when
    that job failed or published no authoritative object."""
    verdict = _alternative(needs.get("lab-verdict"), {"result": "success"})
    authoritative = _authoritative(verdict)
    result = verdict.get("result")
    if authoritative is None or (result != "success" and (event == "pull_request" or result != "skipped")):
        return None
    jobs = {job: record for job, record in needs.items() if job != "lab-verdict"}
    if event != "pull_request":
        return jobs
    outputs = _alternative(verdict.get("outputs"), {})
    for job, record in jobs.items():
        lab = _alternative(outputs.get(f"lab_{job}"), "")
        if authoritative.get(job) is True and lab != "":
            jobs[job] = {**(record or {}), "result": lab}
    return jobs


def _required(event: str, jobs: Needs, flags: Flags) -> list[str]:
    """Return the jobs a merge group or pull request run requires to succeed."""
    if event == "merge_group":
        return list(QUEUE_REQUIRED)
    select = _outputs(jobs, "select")
    required = [job for job in PULL_REQUIRED if not (job == "sonar" and select.get("sonar") == "skip")]
    required += SCOPED.get(str(select.get("scope")), ())
    if flags.mutation == "true":
        required += ["mutation-plan", "mutation-stats"] + ([] if flags.inline == "true" else ["mutation"])
    return required


def _admitted(event: str, jobs: Needs, flags: Flags) -> bool:
    """Whether the run shape fits its event: a queue run's fail fast watcher and unit rows, a pull request's scope."""
    if event == "merge_group":
        return FAIL_FAST in jobs and tuple(_result(jobs, job) for job in UNIT_JOBS) in UNIT_ROWS
    return flags.mutation in ("true", "false") and _outputs(jobs, "select").get("scope") in SCOPES


def _entry(event: str, job: str, record: Mapping[str, Any] | None, required: Sequence[str]) -> bool:
    """Whether one job of a merge group or pull request run ended as the rules allow."""
    result = (record or {}).get("result")
    if job in required:
        return result == "success"
    if event == "merge_group" and job == FAIL_FAST:
        failed = _alternative(_alternative((record or {}).get("outputs"), {}).get("failed"), "")
        return result in FAIL_FAST_RESULTS and failed == ""
    if event == "merge_group" and job in UNIT_JOBS:
        return True
    return result == "skipped"


def _gated(event: str, jobs: Needs, flags: Flags) -> bool:
    """Apply the merge group and pull request rules: required jobs succeed and every other job is skipped."""
    required = _required(event, jobs, flags)
    if not _admitted(event, jobs, flags) or not all(_result(jobs, job) == "success" for job in required):
        return False
    return all(_entry(event, job, record, required) for job, record in jobs.items())


def _skippable(event: str, flags: Flags) -> frozenset[str]:
    """Return the jobs a push or dispatch run may skip under the flags."""
    skips = set(ALWAYS_SKIPPABLE)
    if event == "push":
        skips |= PUSH_SKIPPABLE
    if flags.mutation == "false":
        skips |= MUTATION_JOBS
    if flags.inline == "true":
        skips.add("mutation")
    if flags.kind == "false":
        skips.add("helm-kind")
    return frozenset(skips)


def _pushed(event: str, jobs: Needs, flags: Flags) -> bool:
    """Apply the push and dispatch rules: no reused queue result, and every job succeeds or may be skipped."""
    skips = _skippable(event, flags)
    passing = (_result(jobs, job) == "success" or (_result(jobs, job) == "skipped" and job in skips) for job in jobs)
    return flags.reused != "true" and all(passing)


def fold(event: str, needs: Needs, flags: Flags) -> bool:
    """Return whether `Gate — Required` passes: the rules of its step in the test workflow over one run's needs."""
    if _result(needs, "select") in ("failure", "cancelled"):
        return False
    jobs = _lab(event, _forwarded(needs))
    if jobs is None:
        return False
    if event in GRADED_EVENTS:
        return _gated(event, jobs, flags)
    return _pushed(event, jobs, flags)


@dataclass(frozen=True)
class Head:
    """One head the inbox read: its event and its lane, the branch whose newer head supersedes it."""

    event: HeadEvent
    lane: str


@dataclass(frozen=True)
class Plan:
    """How to grade one head: the tree and grader revision it reads, the units of each gate, the jobs the tier skips,
    and the job outputs and flags the `Gate — Required` rules read."""

    tree: str
    grader: str
    gates: Mapping[str, tuple[Unit, ...]]
    skipped: frozenset[str] = frozenset()
    outputs: Mapping[str, Mapping[str, str]] = field(default_factory=dict[str, Mapping[str, str]])
    flags: Flags = Flags()


class Inbox(Protocol):
    """Where new heads come from."""

    def heads(self) -> list[Head]:
        """Return the heads that arrived since the last call."""
        ...


class Expander(Protocol):
    """What expands a grade request into units."""

    def expand(self, request: GradeRequest) -> Plan:
        """Return the plan of one grade request."""
        ...


class Poster(Protocol):
    """Where check runs go: `Checks.post`, seeded by `Checks.seed` with the check runs a grade already holds."""

    def seed(self, head: str, gate: str, run: str, run_id: int, status: str, shadow: bool) -> None:
        """Remember a check run posted before this poster existed, so its next post advances it in place."""
        ...

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        """Create or advance one check run and return its id."""
        ...


@dataclass(frozen=True)
class Ports:
    """The scheduler's outside world: heads in, plans, check runs out per repository, the heads marked hotfix and the
    worker pods whose deletion cost follows their leases."""

    inbox: Inbox
    expander: Expander
    checks: Callable[[str], Poster]
    hotfixes: Callable[[], frozenset[str]]
    pods: Pods = unmanaged


@dataclass(frozen=True)
class Settings:
    """The trusted actors, the quiet window of a lane, the retries of a lost unit and whether check runs are shadow."""

    trusted: frozenset[str]
    quiet_ms: int = QUIET_MS
    retries: Policy = Policy()
    shadow: bool = True


@dataclass
class Slot:
    """One unit of a grade: the job grading it, if any, its outcome once final, whether a worker started it and
    whether it waits for the day's budget to admit it to the queue."""

    kind: str
    key: str
    members: list[str]
    job: str = ""
    outcome: str = ""
    started: bool = False
    held: bool = False


@dataclass
class State:
    """A grade's state: what it grades, its units by gate, whether a newer head superseded it, what it posted and the
    id of each check run it posted."""

    run: str
    repo: str
    head: str
    base: str
    tier: int
    lane: str
    tree: str
    grader: str
    flags: Flags
    outputs: dict[str, dict[str, str]]
    skipped: list[str]
    gates: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    units: dict[str, Slot] = field(default_factory=dict[str, Slot])
    superseded: bool = False
    conclusion: str = ""
    posted: dict[str, list[str | None]] = field(default_factory=dict[str, list[str | None]])
    runs: dict[str, int] = field(default_factory=dict[str, int])


def _parse(raw: str | bytes) -> State:
    """Read a grade state document."""
    data = json.loads(raw)
    data["flags"] = Flags(**data["flags"])
    data["units"] = {ref: Slot(**slot) for ref, slot in data["units"].items()}
    return State(**data)


def _evidence(state: State) -> evidence.GradeEvidence:
    """Return a closed grade's evidence: its inputs and each of its units with the grade run that used its verdict."""
    inputs = GradeInputs(state.repo, state.head, state.base, CLASSES[state.tier], state.grader)
    units = tuple(evidence.GradeUnit(slot.kind, slot.key, state.run) for slot in state.units.values())
    return evidence.GradeEvidence(state.run, inputs, units)


def _request(data: Mapping[str, Any]) -> GradeRequest:
    """Read a held grade request."""
    return GradeRequest(
        repo=str(data["repo"]),
        head=str(data["head"]),
        base=str(data["base"]),
        tier=Priority(data["tier"]),
        actor=str(data["actor"]),
        bypass_negative=bool(data["bypass_negative"]),
    )


def job_id(head: str, unit: Unit) -> str:
    """Return the queue job of a unit on a head."""
    return f"{head}:{unit.kind}:{unit.key}"


def _verdict(slots: Sequence[Slot]) -> str | None:
    """Return success or failure once every slot is final, else None."""
    if not all(slot.outcome for slot in slots):
        return None
    return "success" if all(slot.outcome == PASSED for slot in slots) else "failure"


def _target(state: State, refs: Sequence[str]) -> tuple[str, str | None]:
    """Return the status and conclusion a check over the refs should show."""
    slots = [state.units[ref] for ref in refs]
    if state.superseded:
        return "completed", "cancelled"
    verdict = _verdict(slots)
    if verdict is not None:
        return "completed", verdict
    if any(slot.job and (slot.started or slot.outcome) for slot in slots):
        return "in_progress", None
    return "queued", None


def _needs(state: State) -> dict[str, dict[str, Any]]:
    """Return the grade as the needs of a run: each gate's result and the jobs the tier skips, with their outputs."""
    results: dict[str, str | None] = {job: "skipped" for job in state.skipped}
    results.update({gate: _verdict([state.units[ref] for ref in refs]) for gate, refs in state.gates.items()})
    return {job: {"result": result, "outputs": dict(state.outputs.get(job, {}))} for job, result in results.items()}


def _checks(state: State) -> dict[str, tuple[str, str | None]]:
    """Return each check run's target: one per gate with units and the required check folding the gates."""
    targets = {gate: _target(state, refs) for gate, refs in state.gates.items() if refs}
    status, conclusion = _target(state, list(state.units))
    if status == "completed" and not state.superseded:
        conclusion = "success" if fold(EVENTS[state.tier], _needs(state), state.flags) else "failure"
    targets[REQUIRED] = (status, conclusion)
    return targets


def _slug(repo: str) -> str:
    """Return owner/name of a repository URL."""
    return urlsplit(repo).path.removeprefix("/")


def summary(state: State) -> dict[str, Any]:
    """Return a grade as GET /grades/<sha> shows it: what it grades, its class and lane, how many of its units are
    final, its conclusion once it closed and each check run's last post."""
    checks = {
        check: {"status": status, "conclusion": conclusion} for check, (status, conclusion) in state.posted.items()
    }
    return {
        "run": state.run,
        "repo": state.repo,
        "head": state.head,
        "base": state.base,
        "class": CLASSES[state.tier],
        "lane": state.lane,
        "tree": state.tree,
        "grader": state.grader,
        "conclusion": state.conclusion or None,
        "superseded": state.superseded,
        "units": {"total": len(state.units), "final": sum(1 for slot in state.units.values() if slot.outcome)},
        "checks": checks,
    }


class Scheduler:
    """The lab's grading over its Redis state, verdict store, object store and ports."""

    def __init__(
        self, client: Any, objects: Objects, ports: Ports, settings: Settings, spending: budget.Budget | None = None
    ) -> None:
        self.client = client
        self.objects = objects
        self.store = Store(client, objects)
        self.queue = Queue(client)
        self.ports = ports
        self.settings = settings
        self.spending = spending
        self.registry = metrics.Registry()

    def tick(self, now_ms: int) -> list[str]:
        """Run one pass: intake, release quiet lanes, reap expired leases, enqueue the held units the budget admits,
        then advance every live grade, counting each finished job in the most urgent tier of the live grades holding
        it."""
        actions = [*self._intake(now_ms), *self._release(now_ms), *self._reap(now_ms), *self._admit(now_ms)]
        tiers, _, _ = self._demand()
        for run in sorted(self.client.smembers(LIVE)):
            actions.extend(self._advance(run, tiers))
        return actions

    def grade(self, run: str) -> State | None:
        """Return a grade's state, or None for an unknown run."""
        raw = self.client.get(GRADES + run)
        return None if raw is None else _parse(raw)

    def load(self, now_ms: int) -> Load:
        """Return the queue and slots now: each pending job's estimate and slots and each leased job and its slots per
        the most urgent class of the live grades holding it, verdict cache use per class of those grades, the slots
        leased jobs take, the leased jobs whose stored lease start is more than one estimate ago, and the workers that
        offered free slots on a lease poll within the ready window with those slots; a leased job frees its slots in
        one estimate."""
        classes, hits, misses = self._demand()
        pending: dict[str, list[float]] = {}
        pending_slots: Counter[str] = Counter()
        for job in self._jobs(self.queue.pending()):
            if job.id in classes:
                name = CLASSES[classes[job.id]]
                pending.setdefault(name, []).append(UNIT_ESTIMATE_S * job.requirement.slots)
                pending_slots[name] += job.requirement.slots
        running: Counter[str] = Counter()
        running_slots: Counter[str] = Counter()
        busy = overdue = 0
        since = now_ms - int(UNIT_ESTIMATE_S * 1000)
        for job in self._jobs(self.queue.leased()):
            busy += job.requirement.slots
            overdue += bool(job.leased_ms) and job.leased_ms < since
            if job.id in classes:
                name = CLASSES[classes[job.id]]
                running[name] += 1
                running_slots[name] += job.requirement.slots
        ready = now_ms - READY_MS
        self.queue.prune(ready)
        offers = self.queue.offers(ready)
        return Load(
            pending,
            dict(running),
            busy + sum(free for free, _ in offers.values()),
            busy,
            cache_hits=dict(hits),
            cache_misses=dict(misses),
            frees_in=(UNIT_ESTIMATE_S,) * busy,
            running_slots=dict(running_slots),
            pending_slots=dict(pending_slots),
            workers_ready=len(offers),
            overdue=overdue,
        )

    def _demand(self) -> tuple[dict[str, int], Counter[str], Counter[str]]:
        """Return the most urgent tier of the live grades holding each queue job, and per class the live grades' units
        the verdict store answered and those it sent to the queue."""
        tiers: dict[str, int] = {}
        hits: Counter[str] = Counter()
        misses: Counter[str] = Counter()
        for state in self._live():
            name = CLASSES[state.tier]
            for slot in state.units.values():
                if not slot.job:
                    hits[name] += 1
                    continue
                misses[name] += 1
                tiers[slot.job] = min(tiers.get(slot.job, state.tier), state.tier)
        return tiers, hits, misses

    def grades_of(self, head: str) -> list[dict[str, Any]]:
        """Return every grade of a commit by run: its live grades and the closed ones its evidence markers name."""
        prefix = evidence.head_prefix(head)
        runs = {key.removeprefix(prefix) for key in self.objects.keys(prefix)}
        runs.update(state.run for state in self._live() if state.head == head)
        return [summary(state) for run in sorted(runs) if (state := self.grade(run)) is not None]

    def _live(self) -> list[State]:
        """Return the state of every live grade that still has one."""
        return [state for run in sorted(self.client.smembers(LIVE)) if (state := self.grade(run)) is not None]

    def _jobs(self, ids: Sequence[str]) -> list[Job]:
        """Return the queue jobs of the ids whose records still exist."""
        return [job for job in map(self.queue.job, ids) if job is not None]

    def _save(self, state: State) -> None:
        self.client.set(GRADES + state.run, json.dumps(asdict(state)))

    def _intake(self, now_ms: int) -> list[str]:
        heads = self.ports.inbox.heads()
        fixes = self.ports.hotfixes() if heads else frozenset[str]()
        actions: list[str] = []
        for head in heads:
            request = admit(head.event, self.settings.trusted)
            if request is None:
                actions.append(f"untrusted {head.event.actor} head {head.event.head}")
                continue
            if head.lane in fixes or head.event.head in fixes:
                request = replace(request, tier=Priority.HOTFIX)
            actions.append(self._hold(request, head.lane, now_ms))
        return actions

    def _hold(self, request: GradeRequest, lane: str, now_ms: int) -> str:
        """Hold a head in its lane's quiet window; a new head of the lane restarts the window."""
        name = f"{request.repo}|{lane}|{int(request.tier)}"
        held = self.client.hget(WAITING, name)
        if held is not None and json.loads(held)["request"]["head"] == request.head:
            return f"holding {request.head}"
        self.client.hset(WAITING, name, json.dumps({"request": asdict(request), "lane": lane, "since": now_ms}))
        return f"received {request.head} on {lane}"

    def _release(self, now_ms: int) -> list[str]:
        """Start each held head whose quiet window ended, except one a pause of its class holds."""
        actions: list[str] = []
        pauses = control.paused(self.client)
        for name, raw in sorted(self.client.hgetall(WAITING).items()):
            held = json.loads(raw)
            request = _request(held["request"])
            quiet = 0 if request.tier == Priority.HOTFIX else self.settings.quiet_ms
            if now_ms - held["since"] < quiet or control.holds(
                pauses, CLASSES[request.tier], request.head, held["lane"]
            ):
                continue
            self.client.hdel(WAITING, name)
            try:
                actions.append(self._start(request, held["lane"], now_ms))
            except (
                subprocess.CalledProcessError,
                ValueError,
                KeyError,
                yaml.YAMLError,
                jsonschema.ValidationError,
            ) as error:
                actions.append(f"cannot expand {request.head}: {error}")
        return actions

    def _start(self, request: GradeRequest, lane: str, now_ms: int) -> str:
        """Open a grade: supersede the lane's older head, place every unit and hold the store's misses for the budget;
        a request that bypasses negative verdicts grades again under a run of its own."""
        plan = self.ports.expander.expand(request)
        inputs = GradeInputs(request.repo, request.head, request.base, CLASSES[request.tier], plan.grader)
        run = grade_key(inputs)
        if self.client.exists(GRADES + run):
            if not request.bypass_negative:
                return f"already graded {run}"
            run = f"{run}-{now_ms}"
        carried = self._supersede(request, lane)
        state = State(
            run=run,
            repo=request.repo,
            head=request.head,
            base=request.base,
            tier=int(request.tier),
            lane=lane,
            tree=plan.tree,
            grader=plan.grader,
            flags=plan.flags,
            outputs={job: dict(values) for job, values in plan.outputs.items()},
            skipped=sorted(plan.skipped),
        )
        source = self._merge_group(request, plan.tree)
        misses: list[Unit] = []
        for gate, units in plan.gates.items():
            if source is not None and gate in source.gates:
                state.gates[gate] = list(source.gates[gate])
                state.units.update({ref: replace(source.units[ref]) for ref in source.gates[gate]})
                continue
            state.gates[gate] = [self._place(state, unit, request.bypass_negative, carried, misses) for unit in units]
        self._save(state)
        self.client.sadd(LIVE, run)
        self.client.set(f"{LANES}{request.repo}|{lane}|{int(request.tier)}", run)
        return f"grading {run} with {len(misses)} of {len(state.units)} units held"

    def _merge_group(self, request: GradeRequest, tree: str) -> State | None:
        """Return the passing merge group grade of the tree a push grade grades, if one exists."""
        if request.tier != Priority.PROMOTION:
            return None
        run = self.client.get(f"{TREES}{request.repo}|{tree}")
        return None if run is None else self.grade(run)

    def _supersede(self, request: GradeRequest, lane: str) -> dict[str, str]:
        """Drop the pending and held units of the lane's older head and return its unfinished jobs by unit."""
        previous = self.client.get(f"{LANES}{request.repo}|{lane}|{int(request.tier)}")
        old = None if previous is None else self.grade(previous)
        if old is None or old.head == request.head:
            return {}
        dropped = set(self.queue.supersede(old.head))
        old.superseded = True
        for slot in old.units.values():
            if slot.held:
                slot.held, slot.outcome = False, DROPPED
        self._save(old)
        return {
            ref: slot.job
            for ref, slot in old.units.items()
            if slot.job and not slot.outcome and slot.job not in dropped
        }

    def _place(self, state: State, unit: Unit, bypass: bool, carried: Mapping[str, str], misses: list[Unit]) -> str:
        """Answer a unit from the store, a carried job or a new job held for the budget, counting the store lookup, and
        return its ref."""
        ref = f"{unit.kind}:{unit.key}"
        if ref in state.units:
            return ref
        slot = Slot(unit.kind, unit.key, list(unit.members))
        verdict = self.store.get(unit.kind, unit.key, bypass_negative=bypass)
        lookup = contract.CACHE_MISSES_TOTAL if verdict is None else contract.CACHE_HITS_TOTAL
        self.registry.inc(lookup, 1.0, {"cache": caches.VERDICT})
        if verdict is not None:
            slot.outcome = PASSED if verdict.passed else FAILED
        elif ref in carried:
            slot.job = carried[ref]
        else:
            slot.job, slot.held = job_id(state.head, unit), True
            misses.append(unit)
        state.units[ref] = slot
        return ref

    def _reap(self, now_ms: int) -> list[str]:
        """Requeue each expired lease while the lost retries allow, else mark its unit lost, counting either, and clear
        the deletion cost of a worker pod whose last lease it reaped."""
        actions: list[str] = []
        for expired in self.queue.expired(now_ms):
            job = self.queue.job(expired)
            if job is not None and (state := self.queue.reap(job, now_ms, self.settings.retries.lost)):
                self.registry.inc(REAPED[state])
                actions.append(f"reaped {expired} {state}")
                release(self.queue, self.ports.pods, job.worker)
        return actions

    def _admit(self, now_ms: int) -> list[str]:
        """Enqueue the held units the day's budget admits, the most urgent grade first: every unit of a class not shed,
        and backfill only into the slots workers offered free after every unit already waiting in the queue, backfill
        and those admitted in this pass included, since the queue leases in order."""
        grades = sorted(
            (state for state in self._live() if any(slot.held for slot in state.units.values())),
            key=lambda state: state.tier,
        )
        if not grades:
            return []
        shed = () if self.spending is None else self.spending.status().shed
        load = self.load(now_ms)
        waiting = sum(load.pending_slots.values())
        actions: list[str] = []
        for state in grades:
            admitted: list[Unit] = []
            for slot in state.units.values():
                if slot.held and budget.admits(CLASSES[state.tier], DEFAULT.slots, load.slots_free, waiting, shed):
                    slot.held = False
                    admitted.append(Unit(slot.kind, slot.key, tuple(slot.members)))
                    waiting += DEFAULT.slots
            if admitted:
                self.queue.enqueue(admitted, state.head, {}, state.base, state.grader)
                self._save(state)
                actions.append(f"enqueued {len(admitted)} units of {state.run}")
        return actions

    def _advance(self, run: str, tiers: Mapping[str, int]) -> list[str]:
        """Collect a grade's finished jobs, each counted in its tier, post its check runs and close it once every unit
        is final, counting the closed grade."""
        state = self.grade(run)
        if state is None:
            self.client.srem(LIVE, run)
            return [f"forgot {run}"]
        for slot in state.units.values():
            self._collect(slot, tiers, state.repo)
        actions = self._post(state)
        if all(slot.outcome for slot in state.units.values()):
            self.client.srem(LIVE, run)
            state.conclusion = str(state.posted[REQUIRED][1])
            if state.tier == Priority.QUEUE and state.conclusion == "success":
                self.client.set(f"{TREES}{state.repo}|{state.tree}", run)
            evidence.record(self.objects, _evidence(state))
            graded = {"class": CLASSES[state.tier], "conclusion": state.conclusion}
            self.registry.inc(contract.HEADS_GRADED_TOTAL, 1.0, graded)
            actions.append(f"completed {run} {state.conclusion}")
        self._save(state)
        return actions

    def _collect(self, slot: Slot, tiers: Mapping[str, int], repo: str) -> None:
        """Take a finished job into its slot in the class of its tier; a vanished job was dropped and a held one waits
        for the budget."""
        if slot.outcome or slot.held or not slot.job:
            return
        job = self.queue.job(slot.job)
        if job is None:
            slot.outcome = DROPPED
        elif job.state in FINISHED:
            self._finished(slot, job, tiers[job.id], repo)
        else:
            slot.started = slot.started or job.state == "leased"

    def _finished(self, slot: Slot, job: Job, tier: int, repo: str) -> None:
        """Count the finished attempt once in the tier's class from its report, then take its outcome into the slot,
        storing its verdict, unless the retry policy runs it again."""
        uploaded = self._uploaded(job)
        if uploaded is not None and self.queue.tally(job):
            metrics.observe_unit(self.registry, CLASSES[tier], job.unit.kind, job.requirement.slots, uploaded[0])
        if not self._rerun(job):
            slot.outcome = job.outcome
            self._remember(job, uploaded, repo)

    def _rerun(self, job: Job) -> bool:
        """Move a finished job back to pending when the retry policy allows its outcome a rerun; return whether it
        moved."""
        result = UnitResult(job.outcome, job.attempts)
        return decide(result, self.settings.retries) == "retry" and self.queue.retry(job)

    def _remember(self, job: Job, uploaded: tuple[dict[str, Any], bytes] | None, repo: str) -> None:
        """Store the verdict of a graded job from the report and log its worker uploaded, and note the files its batch
        opened outside the repository's manifest; a lost or unrun job is no verdict."""
        if job.outcome in UNGRADED or uploaded is None:
            return
        report, log = uploaded
        passed = job.outcome == PASSED
        verdict = Verdict(
            job.unit.kind, job.unit.key, passed, passed and bool(report.get("flaky")), float(report.get("seconds", 0.0))
        )
        self.store.put(verdict, report, log)
        if report.get("outside"):
            note_outside(self.objects, repo, job.unit.members, report["outside"])

    def _uploaded(self, job: Job) -> tuple[dict[str, Any], bytes] | None:
        """Return the report and log a finished job's worker uploaded, the log empty when it uploaded none; None
        without a report."""
        report_path, log_path = upload_path(job.id, "report"), upload_path(job.id, "log")
        present = set(self.objects.keys(str(PurePosixPath(report_path).parent) + "/"))
        if report_path not in present:
            return None
        log = self.objects.read(log_path) if log_path in present else b""
        return json.loads(self.objects.read(report_path)), log

    def _post(self, state: State) -> list[str]:
        """Post each check run whose target moved since its last post; a completed check run never moves again. A
        check run the grade already holds seeds the poster first, so a restarted scheduler advances it in place."""
        moved = [
            (check, status, conclusion)
            for check, (status, conclusion) in _checks(state).items()
            if (posted := state.posted.get(check)) != [status, conclusion]
            and (posted is None or posted[0] != "completed")
        ]
        if not moved:
            return []
        poster = self.ports.checks(_slug(state.repo))
        shadow = self.settings.shadow
        for check, status, conclusion in moved:
            if check in state.runs:
                poster.seed(state.head, check, state.run, state.runs[check], str(state.posted[check][0]), shadow)
            output = self._output(state, check, status)
            state.runs[check] = poster.post(state.head, check, status, conclusion, output, shadow)
            state.posted[check] = [status, conclusion]
        return [f"posted {check} {status} on {state.head}" for check, status, _ in moved]

    def _output(self, state: State, check: str, status: str) -> Output:
        """Return a check run's output: its units, the grade run, the grader revision and the head's commit page."""
        refs = list(state.units) if check == REQUIRED else state.gates[check]
        units = tuple(Unit(slot.kind, slot.key, tuple(slot.members)) for slot in (state.units[ref] for ref in refs))
        title = f"{CLASSES[state.tier]} grade {status}"
        summary = f"{len(units)} units of {check} on {state.head} against {state.base}"
        return Output(title, summary, units, state.run, state.grader, f"{state.repo}/commit/{state.head}")


class BusInbox:
    """Lab head events read from the bus consumer of `ci.lab.head.>`, each acknowledged once read, or terminated."""

    def __init__(self, consumer: Consumer, batch: int = BATCH, timeout_ms: int = FETCH_MS) -> None:
        self.consumer = consumer
        self.batch = batch
        self.timeout_ms = timeout_ms

    def heads(self) -> list[Head]:
        """Return the heads of the next batch of envelopes; a head's lane is its branch, else the head itself. An
        envelope missing a head field is terminated with a logged warning, never redelivered to the dead letter cap.
        """
        heads: list[Head] = []
        for delivery in self.consumer.fetch(self.batch, self.timeout_ms):
            data = cast(Mapping[str, Any], delivery.event.data)  # pragma: no mutate
            missing = [name for name in HEAD_FIELDS if name not in data]
            if missing:
                logging.getLogger(__name__).warning(
                    "terminated lab head %s missing %s", delivery.event.id, ", ".join(missing)
                )
                delivery.term()
                continue
            event = HeadEvent(*(str(data[name]) for name in HEAD_FIELDS))
            heads.append(Head(event, str(data.get("branch") or event.head)))
            delivery.ack()
        return heads


def _within(files: Mapping[str, str], paths: Sequence[str]) -> dict[str, str]:
    """Return the files under the paths, each a file or a folder; `.` names the whole tree."""
    if WHOLE_TREE in paths:
        return dict(files)
    folders = tuple(path.removesuffix("/") + "/" for path in paths)
    return {path: blob for path, blob in files.items() if path in paths or path.startswith(folders)}


def _lab_block(document: str) -> Mapping[str, Any]:
    """Return the lab block of a delivery.yaml document, validated against its schema."""
    data = yaml.safe_load(document)
    jsonschema.validate(data, DELIVERY_SCHEMA)
    return data["lab"]


def frame(tier: Priority, tree: str, grader: str, gates: Mapping[str, tuple[Unit, ...]]) -> Plan:
    """Return a tier's plan: the lab's gates its `Gate — Required` rules require, and every other job they require as
    a gate without units, which passes, so only what the lab grades can turn the grade red.

    A pull request or hotfix grade runs with no test scope and no mutation; a merge group grade runs only the queue
    suite with no lost unit rerun; a promotion or backfill grade takes every lab gate, since those rules require no
    job by name.
    """
    if tier >= Priority.PROMOTION:
        return Plan(tree, grader, dict(gates))
    if tier == Priority.QUEUE:
        required = (*QUEUE_REQUIRED, FAIL_FAST, UNIT_JOBS[0])
        return Plan(tree, grader, {job: gates.get(job, ()) for job in required}, frozenset(UNIT_JOBS[1:]))
    framed = {job: gates.get(job, ()) for job in PULL_REQUIRED}
    return Plan(tree, grader, framed, outputs={"select": {"scope": "none"}})


@dataclass(frozen=True)
class StoredManifests:
    """Packed dependency manifests in the lab object store, one per dev commit under `objects.manifest_folder`; one of
    the newest `MANIFEST_DEPTH` commits with none stored is fetched from its dev push Tests run and stored once. The
    notes of the files test batches opened outside them sit under its `outside/` folder."""

    objects: Objects
    fetch: Fetch = field(default_factory=Fetcher)

    def __call__(self, repo: str, commits: Sequence[str]) -> dict[str, Any] | None:
        """Return the manifest of the first commit that has one stored or, among the newest, fetched, with every note of
        the repository merged into its test records; None when none has."""
        slug = _slug(repo)
        folder = manifest_folder(slug)
        stored = frozenset(self.objects.keys(folder))
        notes = (name for name in stored if name.startswith(folder + NOTES))
        for index, commit in enumerate(commits):
            path = f"{folder}{commit}.json"
            if path in stored:
                return self._merged(UNPACK(json.loads(self.objects.read(path))), notes)
            packed = self.fetch(slug, commit) if index < MANIFEST_DEPTH else None
            if packed is not None:
                self.objects.write_once(path, packed)
                return self._merged(UNPACK(json.loads(packed)), notes)
        return None

    def _merged(self, manifest: dict[str, Any], notes: Iterable[str]) -> dict[str, Any]:
        """Return the manifest with each note's files added to the outside list of every test record it names."""
        records: dict[str, dict[str, Any]] = manifest["tests"]
        for name in notes:
            note = json.loads(self.objects.read(name))
            for test in note["tests"]:
                if test in records:
                    records[test]["outside"] = sorted({*records[test].get("outside", ()), *note["outside"]})
        return manifest


def note_outside(objects: Objects, repo: str, tests: Sequence[str], outside: Sequence[str]) -> None:
    """Write a batch's tests and the files it opened outside the manifest as one note of the repository, named by its
    content so the same batch and files write it once."""
    record: dict[str, object] = {"tests": tests, "outside": sorted(outside)}
    objects.write_once(f"{manifest_folder(_slug(repo))}{NOTES}{digest(record)}.json", json.dumps(record).encode())


class MirrorExpander:
    """Units of a head read from a git mirror with ls-tree and show. Only the mutation units use a temporary checkout
    of the head, since their builders read files from disk."""

    def __init__(self, node: Path, root: Path, toolchain: Toolchain, manifests: Manifests, verdicts: Verdicts) -> None:
        self.node = node
        self.root = root
        self.toolchain = toolchain
        self.manifests = manifests
        self.verdicts = verdicts

    def _mirror(self, repo: str) -> Mirror:
        self.root.mkdir(parents=True, exist_ok=True)
        return Mirror(self.node, self.root / _slug(repo).replace("/", "--"), repo)

    def _git(self, mirror: Mirror, *argv: str) -> str:
        done = subprocess.run(["git", "-C", str(mirror.store), *argv], capture_output=True, text=True, check=True)
        return done.stdout

    def _files(self, mirror: Mirror, sha: str) -> dict[str, str]:
        """Return path to blob id of every file of a commit's tree."""
        listing = self._git(mirror, "ls-tree", "-r", "-z", sha)
        files: dict[str, str] = {}
        for entry in filter(None, listing.split("\0")):
            meta, path = entry.split("\t", 1)
            files[path] = meta.split()[2]
        return files

    def _workflows(self, mirror: Mirror, sha: str) -> dict[str, Any]:
        """Return each workflow file directly in a commit's workflow folder, parsed and keyed by file name the way
        scripts.ci_wiring.load reads a tree, for the reusable workflows a static job calls."""
        folder = WORKFLOW.parent.as_posix()
        files = {
            PurePosixPath(path).name: blob
            for path, blob in self._files(mirror, sha).items()
            if PurePosixPath(path).parent.as_posix() == folder and path.endswith((".yml", ".yaml"))
        }
        return {name: yaml.safe_load(text) or {} for name, text in self._contents(mirror, files).items()}

    def _on_dev(self, repo: str, path: str) -> str:
        """Return one file of the repository's dev branch head."""
        mirror = self._mirror(repo)
        listing = subprocess.run(
            ["git", "ls-remote", repo, f"refs/heads/{DEV}"], capture_output=True, text=True, check=True
        )
        head = listing.stdout.split()[0]
        mirror.fetch(head)
        return self._git(mirror, "show", f"{head}:{path}")

    def trusted(self, repo: str) -> frozenset[str]:
        """Return the runner trust list on the repository's dev branch."""
        return frozenset(validate_actors(json.loads(self._on_dev(repo, ACTORS))))

    def retries(self, repo: str) -> Policy:
        """Return the retry policy of the lab block of delivery.yaml on the repository's dev branch."""
        return policy(_lab_block(self._on_dev(repo, DELIVERY)))

    def expand(self, request: GradeRequest) -> Plan:
        """Return one unit per static gate the grader's workflow runs, keyed by the files it reads at head and base,
        the unit job's test units of a merge group, promotion or backfill head and the mutation stats and mutant units
        of a promotion or backfill head, framed by the request's tier."""
        mirror = self._mirror(request.repo)
        grader = request.head if request.tier >= Priority.PROMOTION else request.base
        for sha in dict.fromkeys((request.head, request.base, grader)):
            mirror.fetch(sha)
        head, base = self._files(mirror, request.head), self._files(mirror, request.base)
        workflow = self._git(mirror, "show", f"{grader}:{WORKFLOW.as_posix()}")
        registry = json.loads(self._git(mirror, "show", f"{grader}:{REGISTRY.as_posix()}"))
        toolchain = replace(self.toolchain, lockfile=head.get("uv.lock", ""), pyproject=head.get("pyproject.toml", ""))
        gates: dict[str, list[Unit]] = {}
        for gate in discover(workflow, registry, self._workflows(mirror, grader)):
            inputs = StaticInputs(
                gate.name, _within(head, gate.paths), _within(base, gate.base_paths), grader, toolchain
            )
            gates.setdefault(gate.job, []).append(Unit(STATIC, static_key(inputs), (gate.name,)))
        if request.tier >= Priority.QUEUE:
            manifest, contents = self._manifest(mirror, request), self._contents(mirror, head)
            suite = expand_tests.units(manifest, contents, head, json.loads(contents.get(DURATIONS, b"{}")), toolchain)
            gates.setdefault(UNIT_JOBS[0], []).extend(self._tests(mirror, request, grader, manifest, suite))
            if request.tier >= Priority.PROMOTION:
                gates.update(self._mutation(mirror, request, manifest, contents))
        tree = self._git(mirror, "rev-parse", f"{request.head}^{{tree}}").strip()
        return frame(request.tier, tree, grader, {job: tuple(units) for job, units in gates.items()})

    def _tests(
        self, mirror: Mirror, request: GradeRequest, grader: str, manifest: dict[str, Any], suite: tuple[Unit, ...]
    ) -> tuple[Unit, ...]:
        """Return every test batch of the head, or for a merge group head the batches that hold a test its entries
        invalidate, unless the interaction rule sends the group to the whole suite."""
        if request.tier != Priority.QUEUE:
            return suite
        cached = {test for unit in suite if self.verdicts(unit.kind, unit.key) is not None for test in unit.members}
        limit = interaction.entry_limit(_lab_block(self._git(mirror, "show", f"{grader}:{DELIVERY}")))
        chosen = interaction.decide(self._group(mirror, request), manifest, cached, limit)
        if chosen == interaction.FULL:
            return suite
        return tuple(unit for unit in suite if chosen.intersection(unit.members))

    def _manifest(self, mirror: Mirror, request: GradeRequest) -> dict[str, Any]:
        """Return the dependency manifest of the nearest commit on the base's first parent chain that has one stored."""
        ancestry = self._git(mirror, "rev-list", "--first-parent", request.base).split()
        manifest = self.manifests(request.repo, ancestry)
        if manifest is None:
            raise ValueError(f"no dependency manifest at or before {request.base}")
        return manifest

    def _mutation(
        self, mirror: Mirror, request: GradeRequest, manifest: dict[str, Any], contents: Mapping[str, bytes]
    ) -> dict[str, list[Unit]]:
        """Return one mutant unit per function the head changes against its base and one stats unit per test file
        present at the head that holds a test running such a function, built in a temporary checkout of the head;
        none when the head changes no measured Python line."""
        from scripts.ci_lab import executors, expand_mutation

        with self._checkout(mirror, request.head) as root:
            changes = scope.changes_since(root, request.base, request.head)
            if not changes:
                return {}
            covering = expand_mutation.covering(root, manifest, expand_tests.fingerprints(manifest, contents), changes)
            mutants = expand_mutation.mutant_units(root, changes, covering)
            owners = {executors.function_of(unit.members[0]) for unit in mutants}
            tests = {test.partition("::")[0] for name in owners for test in covering.get(name, {})}
            files = sorted(path for path in tests if (root / path).is_file())
            stats = expand_mutation.stats_units(root, files, executors.OPTIONS)
        return {job: list(units) for job, units in ((STATS_JOB, stats), (MUTANT_JOB, mutants)) if units}

    @contextmanager
    def _checkout(self, mirror: Mirror, sha: str) -> Generator[Path]:
        """Yield a temporary worktree of a commit cut from the mirror's store under the mirror root, removed on exit."""
        folder = Path(tempfile.mkdtemp(dir=self.root))
        try:
            self._git(mirror, "worktree", "add", str(folder), sha)
            yield folder
        finally:
            shutil.rmtree(folder)
            self._git(mirror, "worktree", "prune")

    def _group(self, mirror: Mirror, request: GradeRequest) -> interaction.Group:
        """Return a merge group head: its changes against the base and those of each entry, one first parent commit
        each."""
        span = f"{request.base}..{request.head}"
        entries = tuple(
            tuple(dep_manifest.changes(mirror.store, f"{entry}^", entry))
            for entry in self._git(mirror, "rev-list", "--first-parent", span).split()
        )
        changes = tuple(dep_manifest.changes(mirror.store, request.base, request.head))
        pyproject = ci_select.pyproject_reaches_tests(mirror.store, request.base, request.head)
        return interaction.Group(changes, entries, pyproject)

    def _contents(self, mirror: Mirror, files: Mapping[str, str]) -> dict[str, bytes]:
        """Return each file's bytes by path, read in one git cat-file batch keyed by the object ids it prints."""
        blobs = list(dict.fromkeys(files.values()))
        listing = "".join(f"{blob}\n" for blob in blobs).encode()
        argv = ["git", "-C", str(mirror.store), "cat-file", "--batch"]
        stream = io.BytesIO(subprocess.check_output(argv, input=listing))
        found: dict[str, bytes] = {}
        for _ in blobs:
            name, _kind, size = stream.readline().split()
            found[name.decode()] = stream.read(int(size))
            stream.read(1)
        return {path: found[blob] for path, blob in files.items()}


def build(
    environ: Mapping[str, str], client: Any, objects: Objects, consumer: Consumer, spending: budget.Budget
) -> Scheduler:
    """Return the service's scheduler over its clients and the day's budget, trusting the runner trust list of the
    repository's dev."""
    toolchain = Toolchain(sys.version.split()[0], {}, "", "", environ.get(IMAGE, ""), {})
    node, root = Path(environ[contract.GIT_MIRROR]), Path(environ[MIRROR_STORE])
    expander = MirrorExpander(node, root, toolchain, StoredManifests(objects), Store(client, objects).get)
    checks: dict[str, Poster] = {}

    def poster(repo: str) -> Poster:
        if repo not in checks:
            checks[repo] = Checks(repo)
        return checks[repo]

    ports = Ports(BusInbox(consumer), expander, poster, control.Priorities(client, _now_ms), worker_pods(environ))
    repository = environ[REPOSITORY]
    settings = Settings(expander.trusted(repository), retries=expander.retries(repository))
    return Scheduler(client, objects, ports, settings, spending)


def serve(
    scheduler: Scheduler, interval_s: float, passes: int, clock: Callable[[], float], sleep: Callable[[float], None]
) -> int:
    """Run passes every interval, forever when passes is zero, printing each action; return 0."""
    done = 0
    while passes == 0 or done < passes:
        for action in scheduler.tick(int(clock() * 1000)):
            print(action, flush=True)
        done += 1
        sleep(interval_s)
    return 0


def listen(
    scheduler: Scheduler,
    leases: Leases,
    routes: lease_api.ControlRoutes,
    spending: budget.Budget,
    clock: Callable[[], int],
    ports: tuple[int, int],
) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer]:
    """Bind the scheduler port's lease API, control, capacity and grades routes and the metrics port, both over one
    load whose budget fraction and shed classes come from the day's spending, the metrics port with the counters the
    scheduler's passes keep; the caller runs each serve_forever."""

    def snapshot() -> Load:
        return budget.observe(scheduler.load(clock()), spending.status())

    api, exposition = ports
    lease = partial(lease_api.answer, leases, Uploads(scheduler.objects), routes)
    port = service.Routes(lease, snapshot, scheduler.grades_of)
    return service.serve("", api, port), metrics.serve("", exposition, snapshot, scheduler.registry)


def _now_ms() -> int:
    """Return the wall clock in milliseconds."""
    return int(time.time() * 1000)


def _sleep_ms(ms: int) -> None:
    """Sleep the milliseconds."""
    time.sleep(ms / 1000)


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Run the scheduler service from its mounted runtime config: the lease API, capacity and grades on the scheduler
    port, metrics on the metrics port and a pass every interval."""
    import boto3  # pyright: ignore[reportMissingTypeStubs]
    import redis

    from scripts.events.config import BusConfig
    from scripts.events.factory import open_bus

    parser = argparse.ArgumentParser(prog="python -m scripts.ci_lab scheduler")
    parser.add_argument("--interval", type=float, default=INTERVAL_S)
    parser.add_argument("--passes", type=int, default=0)
    arguments = parser.parse_args(argv)
    runtime = load_runtime(Path(environ[contract.RUNTIME_CONFIG]))
    client: Any = redis.Redis.from_url(environ[contract.REDIS_URL], decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    s3: Any = boto3.client(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        "s3",
        endpoint_url=environ[MINIO_ENDPOINT],
        aws_access_key_id=environ[MINIO_ACCESS_KEY],
        aws_secret_access_key=environ[MINIO_SECRET_KEY],
    )
    objects = S3Objects(s3, environ[MINIO_BUCKET], "")
    consumer = open_bus(replace(BusConfig.from_environ(environ), provision=False)).lab_heads.consumer(DURABLE, SUBJECT)
    spending = budget.Budget(client, _now_ms, budget.policy(runtime))
    scheduler = build(environ, client, objects, consumer, spending)
    if environ.get(REINDEX_ON_START) == "true":
        print(f"reindexed {scheduler.store.reindex()} verdicts", flush=True)
    leases = Leases(scheduler.queue, LEASE_TTL_MS, _now_ms, _sleep_ms, LEASE_POLL_MS, scheduler.ports.pods)
    routes = lease_api.ControlRoutes(client, environ.get(contract.CONTROL_TOKEN, ""), _now_ms)
    ports = (contract.SCHEDULER_PORT, contract.METRICS_PORT)
    for server in listen(scheduler, leases, routes, spending, _now_ms, ports):
        Thread(target=server.serve_forever, daemon=True).start()
    return serve(scheduler, arguments.interval, arguments.passes, time.time, time.sleep)
