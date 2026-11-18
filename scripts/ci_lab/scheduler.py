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
import json
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from threading import Thread
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import jsonschema
import yaml

from scripts.ci_lab import budget, contract, control, evidence, lease_api, metrics, service
from scripts.ci_lab.capacity import Load
from scripts.ci_lab.checks import Checks, Output
from scripts.ci_lab.delivery import CLASSES, DELIVERY, DELIVERY_SCHEMA, load_runtime
from scripts.ci_lab.digests import GradeInputs, StaticInputs, Toolchain, grade_key, static_key
from scripts.ci_lab.intake import GradeRequest, admit
from scripts.ci_lab.leases import Leases
from scripts.ci_lab.objects import Objects, S3Objects, Uploads, upload_path
from scripts.ci_lab.outcomes import CANNOT_RUN, DROPPED, FAILED, LOSSES, PASSED
from scripts.ci_lab.queue import Job, Queue
from scripts.ci_lab.retry import Policy, policy
from scripts.ci_lab.static_gates import REGISTRY, WORKFLOW, discover
from scripts.ci_lab.store import PREFIX, Store, Verdict
from scripts.ci_lab.units import Priority, Unit
from scripts.ci_lab.webhooks import HEAD_SUBJECT, HeadEvent
from scripts.ci_lab.worktrees import Mirror
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
ALWAYS_SKIPPABLE = frozenset({"unit-lost", "unit-rerun", "queue-baseline", "unit-selected"})
PUSH_SKIPPABLE = frozenset({"type-gate", "lint", "semgrep", "test-count"})
HEAD_FIELDS = tuple(item.name for item in fields(HeadEvent))
STATIC = "static"
WHOLE_TREE = "."
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

Needs = Mapping[str, Mapping[str, Any]]


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
    """Where check runs go: `Checks.post`."""

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        """Create or advance one check run and return its id."""
        ...


@dataclass(frozen=True)
class Ports:
    """The scheduler's outside world: heads in, plans, check runs out per repository, and the heads marked hotfix."""

    inbox: Inbox
    expander: Expander
    checks: Callable[[str], Poster]
    hotfixes: Callable[[], frozenset[str]]


@dataclass(frozen=True)
class Settings:
    """The trusted actors, the quiet window of a lane, the retries of a lost unit and whether check runs are shadow."""

    trusted: frozenset[str]
    quiet_ms: int = QUIET_MS
    retries: Policy = Policy()
    shadow: bool = True


@dataclass
class Slot:
    """One unit of a grade: the job grading it, if any, its outcome once final and whether a worker started it."""

    kind: str
    key: str
    members: list[str]
    job: str = ""
    outcome: str = ""
    started: bool = False


@dataclass
class State:
    """A grade's state: what it grades, its units by gate, whether a newer head superseded it and what it posted."""

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

    def __init__(self, client: Any, objects: Objects, ports: Ports, settings: Settings) -> None:
        self.client = client
        self.objects = objects
        self.store = Store(client, objects)
        self.queue = Queue(client)
        self.ports = ports
        self.settings = settings

    def tick(self, now_ms: int) -> list[str]:
        """Run one pass: intake, release quiet lanes, reap expired leases, then advance every live grade."""
        actions = [*self._intake(now_ms), *self._release(now_ms), *self._reap(now_ms)]
        for run in sorted(self.client.smembers(LIVE)):
            actions.extend(self._advance(run))
        return actions

    def grade(self, run: str) -> State | None:
        """Return a grade's state, or None for an unknown run."""
        raw = self.client.get(GRADES + run)
        return None if raw is None else _parse(raw)

    def load(self, now_ms: int) -> Load:
        """Return the queue and slots now: each pending job's estimate and each leased job per the most urgent class of
        the live grades holding it, verdict cache use per class of those grades, the slots leased jobs take, and the
        free slots workers offered on a lease poll within the ready window; a leased job frees its slots in one
        estimate."""
        classes, hits, misses = self._demand()
        pending: dict[str, list[float]] = {}
        for job in self._jobs(self.queue.pending()):
            if job.id in classes:
                pending.setdefault(CLASSES[classes[job.id]], []).append(UNIT_ESTIMATE_S * job.requirement.slots)
        running: Counter[str] = Counter()
        busy = 0
        for job in self._jobs(self.queue.leased()):
            busy += job.requirement.slots
            if job.id in classes:
                running[CLASSES[classes[job.id]]] += 1
        ready = now_ms - READY_MS
        self.queue.prune(ready)
        offered = sum(free for free, _ in self.queue.offers(ready).values())
        return Load(
            pending,
            dict(running),
            busy + offered,
            busy,
            cache_hits=dict(hits),
            cache_misses=dict(misses),
            frees_in=(UNIT_ESTIMATE_S,) * busy,
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
            except (subprocess.CalledProcessError, ValueError, KeyError, yaml.YAMLError) as error:
                actions.append(f"cannot expand {request.head}: {error}")
        return actions

    def _start(self, request: GradeRequest, lane: str, now_ms: int) -> str:
        """Open a grade: supersede the lane's older head, place every unit and enqueue the store's misses; a request
        that bypasses negative verdicts grades again under a run of its own."""
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
        self.queue.enqueue(misses, request.head, {}, request.base, plan.grader)
        self._save(state)
        self.client.sadd(LIVE, run)
        self.client.set(f"{LANES}{request.repo}|{lane}|{int(request.tier)}", run)
        return f"grading {run} with {len(misses)} of {len(state.units)} units enqueued"

    def _merge_group(self, request: GradeRequest, tree: str) -> State | None:
        """Return the passing merge group grade of the tree a push grade grades, if one exists."""
        if request.tier != Priority.PROMOTION:
            return None
        run = self.client.get(f"{TREES}{request.repo}|{tree}")
        return None if run is None else self.grade(run)

    def _supersede(self, request: GradeRequest, lane: str) -> dict[str, str]:
        """Drop the pending units of the lane's older head and return its unfinished jobs by unit."""
        previous = self.client.get(f"{LANES}{request.repo}|{lane}|{int(request.tier)}")
        old = None if previous is None else self.grade(previous)
        if old is None or old.head == request.head:
            return {}
        dropped = set(self.queue.supersede(old.head))
        old.superseded = True
        self._save(old)
        return {
            ref: slot.job
            for ref, slot in old.units.items()
            if slot.job and not slot.outcome and slot.job not in dropped
        }

    def _place(self, state: State, unit: Unit, bypass: bool, carried: Mapping[str, str], misses: list[Unit]) -> str:
        """Answer a unit from the store, a carried job or a new job, and return its ref."""
        ref = f"{unit.kind}:{unit.key}"
        if ref in state.units:
            return ref
        slot = Slot(unit.kind, unit.key, list(unit.members))
        verdict = self.store.get(unit.kind, unit.key, bypass_negative=bypass)
        if verdict is not None:
            slot.outcome = PASSED if verdict.passed else FAILED
        elif ref in carried:
            slot.job = carried[ref]
        else:
            slot.job = job_id(state.head, unit)
            misses.append(unit)
        state.units[ref] = slot
        return ref

    def _reap(self, now_ms: int) -> list[str]:
        """Requeue each expired lease while the lost retries allow, else mark its unit lost."""
        actions: list[str] = []
        for expired in self.queue.expired(now_ms):
            job = self.queue.job(expired)
            if job is not None and (state := self.queue.reap(job, now_ms, self.settings.retries.lost)):
                actions.append(f"reaped {expired} {state}")
        return actions

    def _advance(self, run: str) -> list[str]:
        """Collect a grade's finished jobs, post its check runs and close it once every unit is final."""
        state = self.grade(run)
        if state is None:
            self.client.srem(LIVE, run)
            return [f"forgot {run}"]
        for slot in state.units.values():
            self._collect(slot)
        actions = self._post(state)
        if all(slot.outcome for slot in state.units.values()):
            self.client.srem(LIVE, run)
            state.conclusion = str(state.posted[REQUIRED][1])
            if state.tier == Priority.QUEUE and state.conclusion == "success":
                self.client.set(f"{TREES}{state.repo}|{state.tree}", run)
            evidence.record(self.objects, _evidence(state))
            actions.append(f"completed {run} {state.conclusion}")
        self._save(state)
        return actions

    def _collect(self, slot: Slot) -> None:
        """Take a finished job's outcome into its slot, storing its verdict; a vanished job was dropped."""
        if slot.outcome or not slot.job:
            return
        job = self.queue.job(slot.job)
        if job is None:
            slot.outcome = DROPPED
        elif job.state in FINISHED:
            slot.outcome = job.outcome
            self._remember(job)
        else:
            slot.started = slot.started or job.state == "leased"

    def _remember(self, job: Job) -> None:
        """Store the verdict of a graded job from the report its worker uploaded; a lost or unrun job is no verdict."""
        report_path, log_path = upload_path(job.id, "report"), upload_path(job.id, "log")
        present = set(self.objects.keys(str(PurePosixPath(report_path).parent) + "/"))
        if job.outcome in UNGRADED or report_path not in present:
            return
        report = json.loads(self.objects.read(report_path))
        log = self.objects.read(log_path) if log_path in present else b""
        passed = job.outcome == PASSED
        verdict = Verdict(
            job.unit.kind, job.unit.key, passed, passed and bool(report.get("flaky")), float(report.get("seconds", 0.0))
        )
        self.store.put(verdict, report, log)

    def _post(self, state: State) -> list[str]:
        """Post each check run whose target moved since its last post; a completed check run never moves again."""
        moved = [
            (check, status, conclusion)
            for check, (status, conclusion) in _checks(state).items()
            if (posted := state.posted.get(check)) != [status, conclusion]
            and (posted is None or posted[0] != "completed")
        ]
        if not moved:
            return []
        poster = self.ports.checks(_slug(state.repo))
        for check, status, conclusion in moved:
            poster.post(state.head, check, status, conclusion, self._output(state, check, status), self.settings.shadow)
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
    """Lab head events read from the bus consumer of `ci.lab.head.>`, each acknowledged once read."""

    def __init__(self, consumer: Consumer, batch: int = BATCH, timeout_ms: int = FETCH_MS) -> None:
        self.consumer = consumer
        self.batch = batch
        self.timeout_ms = timeout_ms

    def heads(self) -> list[Head]:
        """Return the heads of the next batch of envelopes; a head's lane is its branch, else the head itself."""
        heads: list[Head] = []
        for delivery in self.consumer.fetch(self.batch, self.timeout_ms):
            data = cast(Mapping[str, Any], delivery.event.data)  # pragma: no mutate
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


class MirrorExpander:
    """Static gate units of a head read from a git mirror with ls-tree and show, never a checkout."""

    def __init__(self, node: Path, root: Path, toolchain: Toolchain) -> None:
        self.node = node
        self.root = root
        self.toolchain = toolchain

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
        document = yaml.safe_load(self._on_dev(repo, DELIVERY))
        jsonschema.validate(document, DELIVERY_SCHEMA)
        return policy(document["lab"])

    def expand(self, request: GradeRequest) -> Plan:
        """Return one unit per static gate the grader's workflow runs, keyed by the files it reads at head and base,
        framed by the request's tier."""
        mirror = self._mirror(request.repo)
        grader = request.head if request.tier >= Priority.PROMOTION else request.base
        for sha in dict.fromkeys((request.head, request.base, grader)):
            mirror.fetch(sha)
        head, base = self._files(mirror, request.head), self._files(mirror, request.base)
        workflow = self._git(mirror, "show", f"{grader}:{WORKFLOW.as_posix()}")
        registry = json.loads(self._git(mirror, "show", f"{grader}:{REGISTRY.as_posix()}"))
        toolchain = replace(self.toolchain, lockfile=head.get("uv.lock", ""), pyproject=head.get("pyproject.toml", ""))
        gates: dict[str, list[Unit]] = {}
        for gate in discover(workflow, registry):
            inputs = StaticInputs(
                gate.name, _within(head, gate.paths), _within(base, gate.base_paths), grader, toolchain
            )
            gates.setdefault(gate.job, []).append(Unit(STATIC, static_key(inputs), (gate.name,)))
        tree = self._git(mirror, "rev-parse", f"{request.head}^{{tree}}").strip()
        return frame(request.tier, tree, grader, {job: tuple(units) for job, units in gates.items()})


def build(environ: Mapping[str, str], client: Any, objects: Objects, consumer: Consumer) -> Scheduler:
    """Return the service's scheduler over its clients, trusting the runner trust list of the repository's dev."""
    toolchain = Toolchain(sys.version.split()[0], {}, "", "", environ.get(IMAGE, ""), {})
    expander = MirrorExpander(Path(environ[contract.GIT_MIRROR]), Path(environ[MIRROR_STORE]), toolchain)
    checks: dict[str, Poster] = {}

    def poster(repo: str) -> Poster:
        if repo not in checks:
            checks[repo] = Checks(repo)
        return checks[repo]

    ports = Ports(BusInbox(consumer), expander, poster, control.Priorities(client, _now_ms))
    repository = environ[REPOSITORY]
    settings = Settings(expander.trusted(repository), retries=expander.retries(repository))
    return Scheduler(client, objects, ports, settings)


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
    load whose budget fraction and shed classes come from the day's spending; the caller runs each serve_forever."""

    def snapshot() -> Load:
        return budget.observe(scheduler.load(clock()), spending.status())

    api, exposition = ports
    lease = partial(lease_api.answer, leases, Uploads(scheduler.objects), routes)
    port = service.Routes(lease, snapshot, scheduler.grades_of)
    return service.serve("", api, port), metrics.serve("", exposition, snapshot, metrics.Registry())


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
    consumer = open_bus(BusConfig.from_environ(environ)).lab_heads.consumer(DURABLE, SUBJECT)
    scheduler = build(environ, client, objects, consumer)
    leases = Leases(scheduler.queue, LEASE_TTL_MS, _now_ms, _sleep_ms, LEASE_POLL_MS)
    routes = lease_api.ControlRoutes(client, environ.get(contract.CONTROL_TOKEN, ""), _now_ms)
    spending = budget.Budget(client, _now_ms, budget.policy(runtime))
    ports = (contract.SCHEDULER_PORT, contract.METRICS_PORT)
    for server in listen(scheduler, leases, routes, spending, _now_ms, ports):
        Thread(target=server.serve_forever, daemon=True).start()
    return serve(scheduler, arguments.interval, arguments.passes, time.time, time.sleep)
