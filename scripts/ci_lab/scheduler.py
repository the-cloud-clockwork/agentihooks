"""The CI lab scheduler: `Scheduler.tick` takes admitted heads from intake to completed lab check runs.

One pass reads new head events, admits trusted actors, holds each lane through its quiet window (a hotfix waits for
none), expands a head into units per gate from the git mirror, answers units from the verdict store, enqueues the
misses, reaps expired leases (one requeue, then lost), collects finished jobs into the store, folds each gate's units
and then the gates by the tier's `Gate — Required` rules, and moves the check runs: queued on receipt, in_progress on
the first unit, completed on the last. A grade's state is the JSON document at `agentihooks-ci:grade:<run>`.

A newer head of a lane drops the older head's pending units, carries its leased and finished jobs and cancels its
checks; a push grade of a tree a merge group grade passed carries that grade's gates. A head or lane the control
routes marked hotfix grades in the hotfix class, and a paused class holds its heads but the excepted ones until
resume. A plant head grades in the pull request class on its merge base with dev, resolved at intake once its actor
is trusted. `agentihooks ci-lab-scheduler` runs the service: the lease API with its control routes, capacity and a
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
from collections import Counter, defaultdict
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import MISSING, asdict, dataclass, field, fields, replace
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from threading import Thread
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import jsonschema
import yaml

from hooks.secrets import redact
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
    shadow,
    shadow_report,
    stale_heads,
    static_exclusions,
)
from scripts.ci_lab.capacity import Load
from scripts.ci_lab.checks import Checks, Output, PostRefused
from scripts.ci_lab.delivery import CLASSES, DELIVERY, DELIVERY_SCHEMA, load_runtime
from scripts.ci_lab.digests import LOCKFILE, GradeInputs, StaticInputs, Toolchain, digest, grade_key, static_key
from scripts.ci_lab.intake import GradeRequest, admit
from scripts.ci_lab.leases import Leases, release
from scripts.ci_lab.manifests import Background, Fetcher, FetchPending, FetchRefused, FetchUnanswered
from scripts.ci_lab.objects import Objects, S3Objects, Uploads, flakes_path, manifest_folder, unit_path, upload_path
from scripts.ci_lab.outcomes import CANNOT_RUN, DROPPED, FAILED, LOSSES, PASSED
from scripts.ci_lab.pods import Pods, pod_images, unmanaged, worker_pods
from scripts.ci_lab.queue import DEFAULT, Job, Queue
from scripts.ci_lab.remote.post_receive import GRADE_REFS
from scripts.ci_lab.retry import Policy, UnitResult, decide, policy
from scripts.ci_lab.static_gates import REGISTRY, WORKFLOW, discover
from scripts.ci_lab.store import PREFIX, Store, Verdict
from scripts.ci_lab.units import Priority, Unit
from scripts.ci_lab.webhooks import CHECK_COMPLETED, CLOSED, HEAD_SUBJECT, PLANT, HeadEvent
from scripts.ci_lab.worktrees import Lab, Mirror, lab
from scripts.ci_mutation import scope
from scripts.ci_runner_trust import ACTORS, validate_actors
from scripts.events.bus import Consumer, Delivery
from scripts.github_app import GitAuthRefused
from scripts.swarm import metrics_outbox

__all__ = [
    "GRADES",
    "ORIGINS",
    "BusInbox",
    "Flags",
    "Head",
    "MirrorExpander",
    "Plan",
    "Ports",
    "READY_MS",
    "RECORDS",
    "REFRESH_MS",
    "REVISIONS",
    "Scheduler",
    "Settings",
    "StoredManifests",
    "UNIT_ESTIMATE_S",
    "authoritative_gates",
    "build",
    "fold",
    "frame",
    "keep_manifests",
    "listen",
    "main",
    "serve",
    "stage_grade",
    "storage",
    "stored_manifest",
    "summary",
]

GRADES = f"{PREFIX}grade:"
LIVE = f"{PREFIX}grades"
REVISIONS = f"{PREFIX}revisions"
RECORDS = f"{PREFIX}records"
LANES = f"{PREFIX}lane:"
WAITING = f"{PREFIX}waiting"
TREES = f"{PREFIX}tree:"
ORIGINS = f"{PREFIX}origins"
MERGE_GROUPS = "refs/heads/gh-readonly-queue/*"
GROUPS_TIMEOUT_S = 30.0
UNGRADED = frozenset({*LOSSES, CANNOT_RUN, DROPPED})
FINISHED = frozenset({"done", "lost"})
REAPED = {"pending": contract.UNITS_REDELIVERED_TOTAL, "lost": contract.UNITS_LOST_TOTAL}
REQUIRED = "required"
EVENTS = ("pull_request", "pull_request", "merge_group", "push", "workflow_dispatch")
GRADED_EVENTS = frozenset({"merge_group", "pull_request"})
QUEUE_REQUIRED = ("select", "split", "shard-check", "test-count", "collect")
PULL_REQUIRED = (
    "select",
    "collect",
    "test-count",
    "lint",
    "lint-actions",
    "type-gate",
    "docs",
    "semgrep",
    "sonar",
    "writer-lock",
)
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
MANIFEST_KINDS = (expand_tests.KIND, "mutant")
ALWAYS_SKIPPABLE = frozenset({"unit-lost", "unit-rerun", "queue-baseline", "unit-selected"})
PUSH_SKIPPABLE = frozenset({"type-gate", "lint", "lint-actions", "semgrep", "test-count", "collect"})
HEAD_FIELDS = tuple(item.name for item in fields(HeadEvent) if item.default is MISSING)
STATIC = "static"
WHOLE_TREE = "."
DURATIONS = ".test_durations"
NOTES = "outside/"
DEV = "dev"
DURABLE = "ci-lab-scheduler"
SUBJECT = f"{HEAD_SUBJECT}.>"
CHECKS = f"{HEAD_SUBJECT}.{CHECK_COMPLETED}"
BATCH = 50
FETCH_MS = 100
INTAKE_SECONDS = 10
QUIET_MS = 30_000
STARTS_PER_PASS = 8
LOOKED_AT_PER_PASS = 24
UNANSWERED_FETCHES = 10
POST_HOLD_MS = 60_000
INTERVAL_S = 2.0
LEASE_TTL_MS = 120_000
LEASE_POLL_MS = 500
UNIT_ESTIMATE_S = 60.0
READY_MS = 2 * lease_api.MAX_WAIT_MS
REFRESH_MS = 15_000
MINIO_ENDPOINT = f"{contract.MINIO_PREFIX}ENDPOINT"
MINIO_BUCKET = f"{contract.MINIO_PREFIX}BUCKET"
MINIO_ACCESS_KEY = f"{contract.MINIO_PREFIX}ACCESS_KEY"
MINIO_SECRET_KEY = f"{contract.MINIO_PREFIX}SECRET_KEY"
MIRROR_STORE = "CI_LAB_MIRROR_STORE"
IMAGE = "CI_LAB_IMAGE"
REPOSITORY = "CI_LAB_REPOSITORY"
REINDEX_ON_START = "CI_LAB_REINDEX_ON_START"
GITHUB_APP_SETTINGS = ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY")
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
    """One head the inbox read: its event, its lane, the branch whose newer head supersedes it, and the grade ref a
    lab remote push wrote it to, empty for a head GitHub delivered."""

    event: HeadEvent
    lane: str
    ref: str = ""


@dataclass(frozen=True)
class Plan:
    """How to grade one head: the tree and grader revision it reads, the units of each gate, the jobs the tier skips,
    the job outputs and flags the `Gate — Required` rules read, the dev commit of the dependency manifest its test
    units are keyed on, empty when it keys none, the authoritative gates a hotfix or pull request grade runs no unit
    of, each posted as a skipped check, the gates the grader's delivery.yaml lists as authoritative for the head's
    class, and the merge base of the head and its base that its static units diff against, empty when the base is
    that merge base."""

    tree: str
    grader: str
    gates: Mapping[str, tuple[Unit, ...]]
    skipped: frozenset[str] = frozenset()
    outputs: Mapping[str, Mapping[str, str]] = field(default_factory=dict[str, Mapping[str, str]])
    flags: Flags = Flags()
    manifest: str = ""
    idle: frozenset[str] = frozenset()
    authoritative: frozenset[str] = frozenset()
    merge_base: str = ""


class Inbox(Protocol):
    """Where new heads come from."""

    def heads(self) -> list[Head]:
        """Return the heads that arrived since the last call."""
        ...


class Expander(Protocol):
    """What expands a grade request into units, bases a plant head on dev and says whether a node mirror can hold a
    head."""

    def expand(self, request: GradeRequest) -> Plan:
        """Return the plan of one grade request."""
        ...

    def merge_base(self, repo: str, head: str) -> str:
        """Return the merge base of a head with the repository's dev branch head."""
        ...

    def reachable(self, repo: str, head: str) -> bool:
        """Return whether a ref every node mirror fetches reaches the head."""
        ...

    def groups(self, repo: str) -> frozenset[str]:
        """Return the head of every merge group ref the repository lists; git fails when it cannot list them in
        time."""
        ...


class Poster(Protocol):
    """Where check runs go: `Checks.post`, seeded by `Checks.seed` with the check runs a grade already holds."""

    def seed(self, head: str, gate: str, run: str, run_id: int, status: str, shadow: bool) -> None:
        """Remember a check run posted before this poster existed, so its next post advances it in place."""
        ...

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        """Create or advance one check run and return its id."""
        ...


class Relaying(Protocol):
    """What the service ticks after each pass: `relay.Relay.tick`."""

    def tick(self) -> list[str]:
        """Relay what the closed grades answer and return the actions taken."""
        ...


@dataclass(frozen=True)
class Ports:
    """The scheduler's outside world: heads in, plans, check runs out per repository, the heads marked hotfix, the
    worker pods whose deletion cost follows their leases and, when the service holds one, the listing of the heads a
    repository still wants graded, which raises an OSError when it cannot name them all."""

    inbox: Inbox
    expander: Expander
    checks: Callable[[str], Poster]
    hotfixes: Callable[[], frozenset[str]]
    pods: Pods = unmanaged
    wanted: Callable[[str], frozenset[str]] | None = None


@dataclass(frozen=True)
class Settings:
    """The trusted actors, the quiet window of a lane and the retries of a lost unit."""

    trusted: frozenset[str]
    quiet_ms: int = QUIET_MS
    retries: Policy = Policy()


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
    manifest_digest: str = ""


@dataclass
class State:
    """A grade's state: what it grades, its units by gate, whether a newer head superseded it, what it posted, the
    id of each check run it posted, the grade ref a lab remote push wrote its head to, if any, the dev commit of the
    dependency manifest its test units are keyed on, if any, the authoritative gates it runs no unit of, the gates it
    posts as lab/<gate> rather than lab-shadow/<gate>, the clock before which it posts nothing after GitHub refused
    one of its posts, and the merge base of its head and base that a worker checks out as its base, empty when the
    base is that merge base."""

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
    ref: str = ""
    manifest: str = ""
    idle: list[str] = field(default_factory=list[str])
    authoritative: list[str] = field(default_factory=list[str])
    hold: int = 0
    merge_base: str = ""


def _parse(raw: str | bytes) -> State:
    """Read a grade state document, without the batch manifest a slot stored before manifests moved to the object
    store."""
    data = json.loads(raw)
    data["flags"] = Flags(**data["flags"])
    for slot in data["units"].values():
        slot.pop("manifest", None)
    data["units"] = {ref: Slot(**slot) for ref, slot in data["units"].items()}
    return State(**data)


@dataclass(frozen=True)
class _Demand:
    tier: int
    hits: int
    jobs: tuple[str, ...]


def _grade_demand(raw: str | bytes) -> _Demand:
    state = _parse(raw)
    jobs = tuple(slot.job for slot in state.units.values() if slot.job)
    return _Demand(state.tier, len(state.units) - len(jobs), jobs)


def _record_demand(record: str | bytes) -> tuple[str, _Demand]:
    """Read a stored demand record: the revision it was written at, then its grade's tier, the units the verdict store
    answered and the queue jobs of the rest."""
    data = json.loads(record)
    return str(data["revision"]), _Demand(data["tier"], data["hits"], tuple(data["jobs"]))


def _records(client: Any, runs: Sequence[str]) -> list[str | None]:
    """Return each run's stored demand record, or None for a run with none, read with one HMGET per run in a pipeline
    that opens no transaction."""
    pipe = client.pipeline(transaction=False)
    for run in runs:
        pipe.hmget(RECORDS, run)
    return [record for (record,) in pipe.execute()]


def _document(client: Any, run: str) -> str | None:
    """Return a grade's stored state document, or None for a run with none."""
    return client.get(GRADES + run)


def stage_grade(pipe: Any, document: dict[str, Any]) -> int | None:
    """Queue, on a pipeline watching the grade's key, one transaction storing the grade document and, while the grade
    is live, its next revision and the demand record of that revision, or for a closed grade the removal of both, and
    return the revision it stores, None for a closed grade."""
    run = document["run"]
    revision = None if document.get("conclusion") else int(pipe.hget(REVISIONS, run) or 0) + 1
    pipe.multi()
    pipe.set(GRADES + run, json.dumps(document))
    if revision is None:
        pipe.hdel(REVISIONS, run)
        pipe.hdel(RECORDS, run)
        return
    jobs = [slot["job"] for slot in document["units"].values() if slot.get("job")]
    hits = len(document["units"]) - len(jobs)
    pipe.hincrby(REVISIONS, run)
    pipe.hset(RECORDS, run, json.dumps({"revision": revision, "tier": document["tier"], "hits": hits, "jobs": jobs}))
    return revision


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
        plant=bool(data.get("plant", False)),
    )


def _origin(tier: int, repo: str, head: str, ref: str) -> str:
    """Return the origin of a live grade as its stored document: its class, repository, head and grade ref."""
    return json.dumps({"tier": tier, "repo": repo, "head": head, "ref": ref})


def _urgency(entry: tuple[str, str]) -> tuple[int, int, str]:
    """Return a waiting head's place in release order: its class, then when it began waiting, then its lane."""
    held = json.loads(entry[1])
    return held["request"]["tier"], held["since"], entry[0]


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
    """Return each check run's target: one per gate with units, one skipped per authoritative gate the grade runs no
    unit of, and the required check folding the gates."""
    targets = {gate: _target(state, refs) for gate, refs in state.gates.items() if refs}
    targets.update(dict.fromkeys(state.idle, ("completed", "skipped")))
    status, conclusion = _target(state, list(state.units))
    if status == "completed" and not state.superseded:
        conclusion = "success" if fold(EVENTS[state.tier], _needs(state), state.flags) else "failure"
    targets[REQUIRED] = (status, conclusion)
    return targets


def _moved(state: State) -> list[tuple[str, str, str | None]]:
    """Return each check run whose target moved since its last post; a completed check run never moves again."""
    return [
        (check, status, conclusion)
        for check, (status, conclusion) in _checks(state).items()
        if (posted := state.posted.get(check)) != [status, conclusion] and (posted is None or posted[0] != "completed")
    ]


@dataclass(frozen=True)
class _Quiet:
    """What can move a live grade without its document, taken at the revision it was read or stored at: its tier and
    repository, whether a slot waits for the budget, the jobs of its open slots not yet started and started, whether a
    check run target moved, whether it records posts for the relay, its post hold and whether every slot is final."""

    revision: int
    tier: int
    repo: str
    held: bool
    waiting: tuple[str, ...]
    started: tuple[str, ...]
    moved: bool
    relayed: bool
    hold: int
    final: bool


def _quiet(state: State, revision: int) -> _Quiet:
    """Return what can move a grade without its document, at the revision it was read or stored at."""
    slots = state.units.values()
    open_slots = [slot for slot in slots if slot.job and not slot.outcome and not slot.held]
    return _Quiet(
        revision=revision,
        tier=state.tier,
        repo=state.repo,
        held=any(slot.held for slot in slots),
        waiting=tuple(slot.job for slot in open_slots if not slot.started),
        started=tuple(slot.job for slot in open_slots if slot.started),
        moved=bool(_moved(state)),
        relayed=state.ref.startswith(GRADE_REFS),
        hold=state.hold,
        final=all(slot.outcome for slot in slots),
    )


def _revision(revisions: Mapping[str, str], run: str) -> int:
    """Return a live grade's stored revision, a missing one reading as 0."""
    return int(revisions.get(run, 0))


def _still(quiet: _Quiet, states: Sequence[str | None], now_ms: int, repo_hold: int) -> bool:
    """Return whether advancing a grade would change nothing: no open job is gone or finished and none waiting to
    start is leased, given their states in waiting then started order, and either a moved check run waits out its
    hold or nothing moved and the grade cannot close."""
    if any(state is None or state in FINISHED for state in states) or "leased" in states[: len(quiet.waiting)]:
        return False
    if quiet.moved:
        return not quiet.relayed and now_ms < max(quiet.hold, repo_hold)
    return not quiet.final


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

    _swept = False

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
        self.shed: tuple[str, ...] = ()
        self.registry = metrics.Registry()
        self.held: defaultdict[str, int] = defaultdict(int)
        self._demand_cache: dict[str, tuple[str, _Demand]] = {}
        self._quiets: dict[str, _Quiet] = {}
        self._said: set[tuple[str, str, str]] = set()

    def tick(self, now_ms: int) -> list[str]:
        """Run one pass: on the first pass retire the pull request grades whose head nothing wants, then retire the
        merge group grades whose merge group is gone, intake, release quiet lanes, reap expired leases, refresh the
        live grade summaries, enqueue the held units the budget admits, then advance every live grade, counting each
        finished job in the most urgent tier of the live grades holding it. A pass begins with no refusal said."""
        self._said.clear()
        swept = self._sweep()
        actions = [*self._retire_groups(), *self._intake(now_ms), *self._release(now_ms), *self._reap(now_ms)]
        self._survey()
        actions.extend(self._admit(now_ms))
        tiers, _, _ = self._demand()
        for run in sorted(self.client.smembers(LIVE)):
            actions.extend(self._advance(run, tiers, now_ms))
        return swept + actions

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
        for job, (slots, _) in self.queue.slots(self.queue.pending()).items():
            if job in classes:
                name = CLASSES[classes[job]]
                pending.setdefault(name, []).append(UNIT_ESTIMATE_S * slots)
                pending_slots[name] += slots
        running: Counter[str] = Counter()
        running_slots: Counter[str] = Counter()
        busy = overdue = 0
        since = now_ms - int(UNIT_ESTIMATE_S * 1000)
        for job, (slots, leased_ms) in self.queue.slots(self.queue.leased()).items():
            busy += slots
            overdue += bool(leased_ms) and leased_ms < since
            if job in classes:
                name = CLASSES[classes[job]]
                running[name] += 1
                running_slots[name] += slots
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
        for contribution in self._contributions(sorted(self.client.smembers(LIVE))):
            name = CLASSES[contribution.tier]
            if contribution.hits:
                hits[name] += contribution.hits
            if contribution.jobs:
                misses[name] += len(contribution.jobs)
            for job in contribution.jobs:
                tiers[job] = min(tiers.get(job, contribution.tier), contribution.tier)
        return tiers, hits, misses

    def _contributions(self, live: list[str]) -> list[_Demand]:
        """Return each live run's demand contribution: the kept one while the stored revision is unchanged, else the
        one read again; keep those that carry a revision for the next build."""
        revisions = self.client.hgetall(REVISIONS)
        kept = {
            run: entry
            for run in live
            if (entry := self._demand_cache.get(run)) is not None and entry[0] == revisions.get(run)
        }
        recorded, parsed = self._read_demand([run for run in live if run not in kept])
        self._demand_cache = kept | recorded
        return [demand for _, demand in self._demand_cache.values()] + parsed

    def _read_demand(self, runs: list[str]) -> tuple[dict[str, tuple[str, _Demand]], list[_Demand]]:
        """Return the runs' demand read again: each stored demand record with the revision it carries, and the parsed
        grade document of each run without a record, which no later build reuses; a run with neither is left out."""
        recorded: dict[str, tuple[str, _Demand]] = {}
        parsed: list[_Demand] = []
        for run, record in zip(runs, _records(self.client, runs)):
            if record is not None:
                recorded[run] = _record_demand(record)
            elif (raw := _document(self.client, run)) is not None:
                parsed.append(_grade_demand(raw))
        return recorded, parsed

    def grades_of(self, head: str) -> list[dict[str, Any]]:
        """Return every grade of a commit by run: its live grades and the closed ones its evidence markers name."""
        prefix = evidence.head_prefix(head)
        runs = {key.removeprefix(prefix) for key in self.objects.keys(prefix)}
        runs.update(state.run for state in self._live() if state.head == head)
        return [summary(state) for run in sorted(runs) if (state := self.grade(run)) is not None]

    def _live(self) -> Generator[State]:
        """Yield the state of every live grade that still has one, reading one grade at a time."""
        for run in sorted(self.client.smembers(LIVE)):
            if (state := self.grade(run)) is not None:
                yield state

    def save(self, state: State) -> None:
        """Store a grade's state, so the next pass and the relay read it back, with a live grade's next revision and
        demand record in the same transaction, retried while another writer changes the grade."""
        self.client.transaction(partial(stage_grade, document=asdict(state)), GRADES + state.run)

    def _keep(self, state: State) -> None:
        """Store a grade the pass changed, as save does, and keep a live grade's summary at the revision it stored; a
        closed grade's summary goes with the next survey, which keeps live grades only."""
        stage = partial(stage_grade, document=asdict(state))
        revision = self.client.transaction(stage, GRADES + state.run, value_from_callable=True)
        if revision is not None:
            self._quiets[state.run] = _quiet(state, revision)

    def _drop(self, run: str) -> None:
        """Take a grade out of the live set and remove its revision and demand record in one transaction."""
        with self.client.pipeline() as pipe:
            pipe.srem(LIVE, run)
            pipe.hdel(REVISIONS, run)
            pipe.hdel(RECORDS, run)
            pipe.execute()

    def _survey(self) -> None:
        """Refresh the summary of every live grade that has a document, reading only a grade whose stored revision is
        not the one its summary was taken at, a missing revision reading as 0, and forget the summaries of grades no
        longer live."""
        runs = sorted(self.client.smembers(LIVE))
        pipe = self.client.pipeline(transaction=False)
        pipe.hgetall(REVISIONS)
        for run in runs:
            pipe.exists(GRADES + run)
        revisions, *present = pipe.execute()
        quiets: dict[str, _Quiet] = {}
        for run in (run for run, exists in zip(runs, present) if exists):
            revision = _revision(revisions, run)
            if (quiet := self._quiets.get(run)) is not None and quiet.revision == revision:
                quiets[run] = quiet
            elif (state := self.grade(run)) is not None:
                quiets[run] = _quiet(state, revision)
        self._quiets = quiets

    def _settled(self, quiet: _Quiet, now_ms: int) -> bool:
        """Return whether a summarised grade's open jobs, check runs and slots leave nothing to advance now."""
        states = self.queue.states([*quiet.waiting, *quiet.started])
        return _still(quiet, states, now_ms, self.held[quiet.repo])

    def _intake(self, now_ms: int) -> list[str]:
        heads = self.ports.inbox.heads()
        fixes = self.ports.hotfixes() if heads else frozenset[str]()
        actions: list[str] = []
        for head in heads:
            if head.event.action == CLOSED:
                actions.append(self._close(head.event, head.lane))
                continue
            request = admit(head.event, self.settings.trusted)
            if request is None:
                actions.append(f"untrusted {head.event.actor} head {head.event.head}")
                continue
            if head.event.event == PLANT:
                request = self._plant(request, actions)
                if request is None:
                    continue
            if head.lane in fixes or head.event.head in fixes:
                request = replace(request, tier=Priority.HOTFIX)
            if stale_heads.branchless(request.tier, head.lane, request.head, head.ref):
                actions.append(f"ignored {request.head}: a push that named no branch asks for no grade")
                continue
            actions.append(self._hold(request, head.lane, head.ref, now_ms))
        return actions

    def _plant(self, request: GradeRequest, actions: list[str]) -> GradeRequest | None:
        """Return a plant head's request based on the head's merge base with dev, resolved before the head is held or
        keyed, or none after naming in the actions why git could not resolve one."""
        try:
            return replace(request, base=self.ports.expander.merge_base(request.repo, request.head))
        except subprocess.CalledProcessError as error:
            actions.append(f"cannot resolve the dev merge base of plant head {request.head}: {error}")
            return None

    def _hold(self, request: GradeRequest, lane: str, ref: str, now_ms: int) -> str:
        """Hold a head and its grade ref in its lane's quiet window; a new head of the lane restarts the window, and the
        held head arriving again with a grade ref keeps its window and takes that ref."""
        name = f"{request.repo}|{lane}|{int(request.tier)}"
        held = self.client.hget(WAITING, name)
        waiting = None if held is None else json.loads(held)
        if waiting is not None and waiting["request"]["head"] == request.head:
            if ref:
                self.client.hset(WAITING, name, json.dumps({**waiting, "ref": ref}))
            return f"holding {request.head}"
        waiting = {"request": asdict(request), "lane": lane, "ref": ref, "since": now_ms}
        self.client.hset(WAITING, name, json.dumps(waiting))
        return f"received {request.head} on {lane}"

    def _close(self, closed: HeadEvent, lane: str) -> str:
        """End what a closed pull request holds in its lane of the pull request class, told by its base, which
        GitHub sets at a pull request's last head and repeats when it closes: the waiting head of that base and the
        live grades of the lane's head of that base, with the pending jobs of that head unless another live grade of
        any repository grades it, since a job names its head alone. Nothing is posted. A waiting head or a grade
        pushed to a grade ref stays, so does the same lane of the hotfix class, and so does what carries another
        base, the work of another pull request of the same head branch: while the lane holds live grades of one
        head against two bases, the lane also keeps its waiting head."""
        repo = closed.repo
        name = f"{repo}|{lane}|{int(Priority.PULL_REQUEST)}"
        origins = self._origins()
        run = self._pivot(name, closed, lane, origins)
        if run is None:
            ended = self._unwait(name, closed.base)
        else:
            ended = self._end(name, run, self._bases(run, lane, origins, closed.base), closed.base, origins)
        return f"closed {lane} of {repo}: {', '.join(ended) or 'nothing held'}"

    def _pivot(self, name: str, closed: HeadEvent, lane: str, origins: Mapping[str, Mapping[str, Any]]) -> str | None:
        """Return the live grade a close starts from: the run the lane names while it is live and pushed to no
        grade ref, else the first live pull request grade of the closed head, pushed to no grade ref, that names
        the lane, since the lane may name a grade that finished or was pushed to a grade ref while an older grade
        of that head still runs; none when the lane holds neither."""
        named = self.client.get(f"{LANES}{name}")
        origin = None if named is None else origins.get(named)
        if named is not None and origin is not None and not origin["ref"]:
            return named
        head = {"repo": closed.repo, "head": closed.head}
        grades = ((run, self.grade(run)) for run in sorted(origins) if stale_heads.twin(head, origins[run]))
        return next((run for run, grade in grades if grade is not None and grade.lane == lane), None)

    def _bases(self, run: str, lane: str, origins: Mapping[str, Mapping[str, Any]], lost: str) -> dict[str, str]:
        """Return the base of a lane's run and of every other live pull request grade of its head that names the
        lane, by run; the lane's run counts as of the given base when its grade document is gone."""
        same = [name for name in sorted(origins) if name == run or stale_heads.twin(origins[run], origins[name])]
        grades = {name: self.grade(name) for name in same}
        return {
            name: lost if grade is None else grade.base
            for name, grade in grades.items()
            if name == run or (grade is not None and grade.lane == lane)
        }

    def _end(
        self, name: str, run: str, bases: Mapping[str, str], base: str, origins: Mapping[str, Mapping[str, Any]]
    ) -> list[str]:
        """End, of the live grades a lane holds of its head, those of the closed pull request's base, and the
        waiting head of that base unless the lane holds grades of two bases. A grade of another base that stays
        keeps the lane; when none stays, the pending jobs of the head go unless another live grade grades it."""
        gone, stay = stale_heads.closing(bases, base)
        ended = [] if len(set(bases.values())) > 1 else self._unwait(name, base)
        if not gone:
            return ended
        head = origins[run]["head"]
        kept = {origin["head"] for live, origin in origins.items() if live not in gone}
        dropped = [] if head in kept else self.queue.drop(head)
        for each in gone:
            self._forget(each)
        if not stay:
            return [*ended, f"retired {', '.join(gone)}, dropped {len(dropped)} pending units"]
        if self.client.get(f"{LANES}{name}") in gone:
            self.client.set(f"{LANES}{name}", stay[0])
        return [f"retired {', '.join(gone)}, {stay[0]} still grades {head}"]

    def _unwait(self, name: str, base: str) -> list[str]:
        """Drop a lane's waiting head when it carries the given base and was pushed to no grade ref, and name what
        was dropped."""
        held = self.client.hget(WAITING, name)
        waiting = None if held is None else json.loads(held)
        if waiting is None or waiting.get("ref") or waiting["request"]["base"] != base:
            return []
        self.client.hdel(WAITING, name)
        return [f"dropped the waiting head {waiting['request']['head']}"]

    def _release(self, now_ms: int) -> list[str]:
        """Look at up to `LOOKED_AT_PER_PASS` held heads whose quiet window ended, except one a pause of its class
        holds: the most urgent class first and the longest waiting first within a class. A head still quiet or paused
        takes no place among those looked at, and the rest wait for a later pass, so every pass reaches admission.
        A head whose manifest fetch still runs off the pass keeps its place and its window for the next pass; one
        whose fetch GitHub never answered or answered with a server error waits out the window again, its count of
        such fetches in a row kept in its waiting entry, until the `UNANSWERED_FETCHES`th one. Neither takes one of
        the `STARTS_PER_PASS` places, only one of the `LOOKED_AT_PER_PASS` looked at, so the pass moves on to the
        next waiting head instead of stalling on it. Starting a head, retiring it or dropping it after its last
        expansion failure each take one of those `STARTS_PER_PASS` places, since each is the pass's last word on
        that head."""
        actions: list[str] = []
        pauses = control.paused(self.client)
        started = 0
        looked_at = 0
        for name, raw in sorted(self.client.hgetall(WAITING).items(), key=_urgency):
            held = json.loads(raw)
            request = _request(held["request"])
            quiet = 0 if request.tier == Priority.HOTFIX else self.settings.quiet_ms
            if now_ms - held["since"] < quiet or control.holds(
                pauses, CLASSES[request.tier], request.head, held["lane"]
            ):
                continue
            looked_at += 1
            self.client.hdel(WAITING, name)
            ref = held.get("ref", "")
            try:
                actions.append(self._start(request, held["lane"], ref, now_ms))
                started += 1
            except FetchPending as error:
                self.client.hset(WAITING, name, raw)
                actions.append(f"holding {request.head}: {error}")
            except FetchUnanswered as error:
                fetches = held.get("unanswered", 0) + 1
                if fetches < UNANSWERED_FETCHES:
                    self.client.hset(WAITING, name, json.dumps({**held, "since": now_ms, "unanswered": fetches}))
                    actions.append(f"holding {request.head}: {error}")
                else:
                    started += 1
                    refusal = FetchRefused(
                        f"{fetches} manifest fetches in a row got no answer or a server error, the last: {error}"
                    )
                    actions.append(f"cannot expand {request.head}: {refusal}")
                    actions.extend(self._refuse(request, ref, refusal, now_ms))
            except (
                subprocess.CalledProcessError,
                ValueError,
                KeyError,
                SyntaxError,
                yaml.YAMLError,
                jsonschema.ValidationError,
            ) as error:
                started += 1
                actions.append(f"cannot expand {request.head}: {error}")
                actions.extend(self._refuse(request, ref, error, now_ms))
            if started == STARTS_PER_PASS or looked_at == LOOKED_AT_PER_PASS:
                break
        return actions

    def _refuse(self, request: GradeRequest, ref: str, error: Exception, now_ms: int) -> list[str]:
        """Post a completed required check run with conclusion failure on a head that cannot expand, naming the error,
        so the head shows a lab status; a head pushed to a grade ref posts nothing, since GitHub does not hold it."""
        if ref.startswith(GRADE_REFS):
            return []
        reason = f"cannot expand {request.head} against {request.base}: {error}"
        return self._fail(request, f"{CLASSES[request.tier]} grade cannot expand", reason, f"unexpanded-{now_ms}")

    def _fail(self, request: GradeRequest, title: str, reason: str, external: str) -> list[str]:
        """Post a completed required check run with conclusion failure and the reason as its summary on a head with no
        plan; such a head has no authoritative gates, so the check posts as lab-shadow/required. The post is tried
        once, outside the post holds, and a post the App token, GitHub or the network refuses is recorded and not
        retried, since the head keeps no grade. A pass says one run of one head once: a second waiting request of
        the head, failed in the same pass under the same run id, posts nothing, since the poster refuses a second
        completed post of a check run."""
        said = (request.repo, request.head, external)
        if said in self._said:
            return []
        self._said.add(said)
        output = Output(title, reason, (), external, "unknown", f"{request.repo}/commit/{request.head}")
        try:
            self.ports.checks(_slug(request.repo)).post(request.head, REQUIRED, "completed", "failure", output, True)
        except (GitAuthRefused, PostRefused, OSError) as refusal:
            return [f"cannot post the failed {REQUIRED} check on {request.head}: {refusal}"]
        return []

    def _retire(self, request: GradeRequest, now_ms: int) -> str:
        """Retire a head no ref every node mirror fetches reaches, placing none of its units since no worker could
        fetch it; a pull request head also gets the failed required check run naming that reason, and a refused post
        of it is recorded in the same action."""
        reason = f"no branch or pull request head ref of {request.repo} carries {request.head}"
        refusals: list[str] = []
        if request.tier == Priority.PULL_REQUEST:
            refusals = self._fail(request, f"{CLASSES[request.tier]} grade retired", reason, f"uncarried-{now_ms}")
        return "; ".join([f"retiring {request.head}: {reason}", *refusals])

    def _origins(self) -> dict[str, dict[str, Any]]:
        """Return the origin of every live grade by run: its class, repository, head and grade ref. A live grade with
        no origin recorded gets one from its grade document, and the origin of a run that is no longer live is
        removed, so a pass reads a grade document here once in the life of a grade at most."""
        live = self.client.smembers(LIVE)
        recorded = {run: json.loads(raw) for run, raw in self.client.hgetall(ORIGINS).items()}
        for run in sorted(recorded.keys() - live):
            self.client.hdel(ORIGINS, run)
        origins = {run: origin for run, origin in recorded.items() if run in live}
        for run in sorted(live - recorded.keys()):
            raw = self.client.get(GRADES + run)
            if raw is None:
                continue
            grade = json.loads(raw)
            origin = _origin(grade["tier"], grade["repo"], grade["head"], grade.get("ref", ""))
            self.client.hset(ORIGINS, run, origin)
            origins[run] = json.loads(origin)
        return origins

    def _forget(self, run: str) -> None:
        """Delete a run's grade document and origin, then take it out of the live set with its revision and demand
        record; a pass that ends in between leaves a live run with no document, which the next pass retires again
        while its origin is still recorded and its merge group is still gone, and forgets once its origin is gone."""
        self.client.delete(GRADES + run)
        self.client.hdel(ORIGINS, run)
        self._drop(run)

    def _retire_groups(self) -> list[str]:
        """Close every live merge group grade whose head no merge group ref of its repository carries, listing the
        refs of each such repository once: the run is forgotten and the pending jobs of its head are dropped unless
        another live grade of any repository grades that head, since a job names its head alone. Nothing is posted,
        recorded as evidence or counted as graded. A grade pushed to a grade ref is never retired, and a listing git
        cannot produce retires nothing of that repository and is named in the actions."""
        origins = self._origins()
        groups = {
            run: origin for run, origin in origins.items() if origin["tier"] == Priority.QUEUE and not origin["ref"]
        }
        actions: list[str] = []
        gone: dict[str, tuple[str, str]] = {}
        for repo in sorted({origin["repo"] for origin in groups.values()}):
            try:
                carried = self.ports.expander.groups(repo)
            except subprocess.SubprocessError as error:
                actions.append(f"cannot list the merge groups of {repo}: {error}")
                continue
            for run, origin in groups.items():
                if origin["repo"] == repo and origin["head"] not in carried:
                    gone[run] = (repo, origin["head"])
        kept = {origin["head"] for run, origin in origins.items() if run not in gone}
        for run, (repo, head) in sorted(gone.items()):
            dropped = [] if head in kept else self.queue.drop(head)
            self._forget(run)
            reason = f"no merge group ref of {repo} carries {head}"
            actions.append(f"retired {run}: {reason}, dropped {len(dropped)} pending units")
        return actions

    def _sweep(self) -> list[str]:
        """Retire, once, on the first pass of a scheduler, the promotion work of pushes that named no branch, then the
        stale pull request grades when the scheduler holds a listing of the heads still wanted; the listing port is
        read on that pass, so a scheduler built with no ports builds."""
        swept, self._swept = self._swept, True
        if swept:
            return []
        wanted = self.ports.wanted
        return [*self._retire_pushes(), *([] if wanted is None else self._retire_heads(wanted))]

    def _retire_heads(self, wanted: Callable[[str], frozenset[str]]) -> list[str]:
        """Close every live pull request grade with no grade ref whose head is neither an open pull request head nor
        a plant branch head, listing the wanted heads of each such repository once and never asking about one head:
        the run is forgotten and the pending jobs of its head are dropped unless another live grade of any repository
        grades that head. Nothing is posted. A listing that fails retires nothing of that repository and is named in
        the actions, and each listed repository gets one line counting the heads listed, its live pull request
        grades, the grades retired and the pending units dropped."""
        origins = self._origins()
        actions: list[str] = []
        heads: dict[str, frozenset[str]] = {}
        for repo in stale_heads.repositories(origins):
            try:
                heads[repo] = wanted(repo)
            except OSError as error:
                actions.append(f"cannot list the wanted heads of {repo}: {error}")
        gone = stale_heads.stale(origins, heads)
        kept = {origin["head"] for run, origin in origins.items() if run not in gone}
        units: defaultdict[str, int] = defaultdict(int)
        for run, (repo, head) in sorted(gone.items()):
            dropped = [] if head in kept else self.queue.drop(head)
            self._forget(run)
            units[repo] += len(dropped)
            reason = f"{head} is no open pull request head or plant head of {repo}"
            actions.append(f"retired {run}: {reason}, dropped {len(dropped)} pending units")
        return [*actions, *(stale_heads.listed(repo, len(heads[repo]), origins, gone, units[repo]) for repo in heads)]

    def _retire_pushes(self) -> list[str]:
        """Drop every waiting promotion head and close every live promotion grade a push that named no branch asked
        for, which holds its own head as its lane and no grade ref: the run is forgotten and the pending jobs of its
        head are dropped unless another live grade grades that head. Nothing is posted. Each promotion grade
        document is read alone, once: of a grade it retires only the head is kept, and of a grade it keeps the summary
        at the revision stored before the read. One line counts the waiting heads dropped, the grades retired and the
        pending units dropped; a pass that finds none adds no line."""
        held = {name: json.loads(raw) for name, raw in self.client.hgetall(WAITING).items()}
        waiting = sorted(
            name
            for name, entry in held.items()
            if stale_heads.branchless(
                entry["request"]["tier"], entry["lane"], entry["request"]["head"], entry.get("ref", "")
            )
        )
        if waiting:
            self.client.hdel(WAITING, *waiting)
        origins = self._origins()
        revisions = self.client.hgetall(REVISIONS)
        gone: dict[str, str] = {}
        for run in sorted(origins):
            grade = self.grade(run) if origins[run]["tier"] == Priority.PROMOTION else None
            if grade is not None and stale_heads.branchless(grade.tier, grade.lane, grade.head, grade.ref):
                gone[run] = grade.head
            elif grade is not None:
                self._quiets[run] = _quiet(grade, _revision(revisions, run))
        kept = {origin["head"] for run, origin in origins.items() if run not in gone}
        dropped = 0
        for run, head in sorted(gone.items()):
            dropped += 0 if head in kept else len(self.queue.drop(head))
            self._forget(run)
        return [stale_heads.unbranched(len(waiting), len(gone), dropped)] if waiting or gone else []

    def _start(self, request: GradeRequest, lane: str, ref: str, now_ms: int) -> str:
        """Open a grade of a head and the grade ref it was pushed to: supersede the lane's older head, place every unit
        and hold the store's misses for the budget; a request that bypasses negative verdicts grades again under a run
        of its own. A head no ref every node mirror fetches reaches is retired before expansion, unless it was pushed
        to a grade ref, which the lab remote serves."""
        if not ref.startswith(GRADE_REFS) and not self.ports.expander.reachable(request.repo, request.head):
            return self._retire(request, now_ms)
        plan = self.ports.expander.expand(request)
        inputs = GradeInputs(request.repo, request.head, request.base, CLASSES[request.tier], plan.grader)
        run = grade_key(inputs)
        if self.client.exists(GRADES + run):
            if not request.bypass_negative:
                self._take_lane(request, lane, run)
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
            ref=ref,
            manifest=plan.manifest,
            idle=sorted(plan.idle),
            authoritative=sorted(plan.authoritative),
            merge_base=plan.merge_base,
        )
        source = self._merge_group(request, plan.tree)
        misses: list[Unit] = []
        for gate, units in plan.gates.items():
            if source is not None and gate in source.gates:
                state.gates[gate] = list(source.gates[gate])
                state.units.update({ref: replace(source.units[ref]) for ref in source.gates[gate]})
                continue
            state.gates[gate] = [self._place(state, unit, request.bypass_negative, carried, misses) for unit in units]
        keep_manifests(self.objects, misses)
        self.save(state)
        self.client.hset(ORIGINS, run, _origin(state.tier, state.repo, state.head, state.ref))
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
        self.save(old)
        return {
            ref: slot.job
            for ref, slot in old.units.items()
            if slot.job and not slot.outcome and slot.job not in dropped
        }

    def _take_lane(self, request: GradeRequest, lane: str, run: str) -> None:
        """Give a head whose grade already exists its lane: the lane's older head is superseded and the lane names
        the head's run, so a head first graded outside its pull request lane still holds that lane."""
        self._supersede(request, lane)
        self.client.set(f"{LANES}{request.repo}|{lane}|{int(request.tier)}", run)

    def _place(self, state: State, unit: Unit, bypass: bool, carried: Mapping[str, str], misses: list[Unit]) -> str:
        """Answer a unit from the store, a carried job or a new job held for the budget, counting the store lookup, and
        return its ref."""
        ref = f"{unit.kind}:{unit.key}"
        if ref in state.units:
            return ref
        slot = Slot(unit.kind, unit.key, list(unit.members), manifest_digest=unit.manifest_digest)
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
        and those admitted in this pass included, since the queue leases the most urgent class first and in order
        inside a class, which each unit is enqueued under by its grade's tier. A class the budget newly sheds is named
        first. The held grades and their classes come from the live grade summaries, and a held grade is read only
        when the budget admits its class at its turn, so the pass holds one at a time and reads none it would admit
        nothing of."""
        actions = self._shedding()
        held = sorted((quiet.tier, run) for run, quiet in self._quiets.items() if quiet.held)
        if not held:
            return actions
        load = self.load(now_ms)
        waiting = sum(load.pending_slots.values())
        admissible = (
            run
            for tier, run in held
            if budget.admits(CLASSES[tier], DEFAULT.slots, load.slots_free, waiting, self.shed)
        )
        for state in filter(None, map(self.grade, admissible)):
            admitted: list[Unit] = []
            for slot in state.units.values():
                if slot.held and budget.admits(CLASSES[state.tier], DEFAULT.slots, load.slots_free, waiting, self.shed):
                    slot.held = False
                    admitted.append(
                        Unit(slot.kind, slot.key, tuple(slot.members), manifest_digest=slot.manifest_digest)
                    )
                    waiting += DEFAULT.slots
            if admitted:
                self.queue.enqueue(admitted, state.head, {}, state.merge_base or state.base, state.grader, state.tier)
                self._keep(state)
                actions.append(f"enqueued {len(admitted)} units of {state.run}")
        return actions

    def _shedding(self) -> list[str]:
        """Read the classes the day's budget sheds and name each class shed since the last pass with the share of the
        day's core minutes spent."""
        if self.spending is None:
            return []
        spent = self.spending.spent()
        shed = budget.assess(spent, self.spending.policy).shed
        share = spent / self.spending.policy.core_minutes_per_day
        newly = [
            f"shed {name} with {share:.2f} of the day's core budget spent" for name in shed if name not in self.shed
        ]
        self.shed = shed
        return newly

    def _advance(self, run: str, tiers: Mapping[str, int], now_ms: int) -> list[str]:
        """Collect a grade's finished jobs, each counted in its tier, post its check runs and close it once every unit
        is final and every check run is posted, counting the closed grade. A grade whose summary leaves nothing to
        advance is neither read nor written, and a grade the pass left unchanged is written only to give it its first
        revision."""
        quiet = self._quiets.get(run)
        if quiet is not None and quiet.revision and self._settled(quiet, now_ms):
            return []
        raw = self.client.get(GRADES + run)
        if raw is None:
            self._drop(run)
            return [f"forgot {run}"]
        state = _parse(raw)
        queued = [slot for slot in state.units.values() if not (slot.outcome or slot.held) and slot.job]
        for slot, job in zip(queued, self.queue.jobs([slot.job for slot in queued])):
            self._collect(slot, job, tiers, state)
        actions = self._post(state, now_ms)
        if all(slot.outcome for slot in state.units.values()) and not _moved(state):
            self._drop(run)
            state.conclusion = str(state.posted[REQUIRED][1])
            if state.tier == Priority.QUEUE and state.conclusion == "success":
                self.client.set(f"{TREES}{state.repo}|{state.tree}", run)
            evidence.record(self.objects, _evidence(state))
            graded = {"class": CLASSES[state.tier], "conclusion": state.conclusion}
            self.registry.inc(contract.HEADS_GRADED_TOTAL, 1.0, graded)
            actions.append(f"completed {run} {state.conclusion}")
        if quiet is None or not quiet.revision or json.dumps(asdict(state)) != raw:
            self._keep(state)
        return actions

    def _collect(self, slot: Slot, job: Job | None, tiers: Mapping[str, int], state: State) -> None:
        """Take a finished job read for an open slot into it in the class of its tier; a vanished job was dropped."""
        if job is None:
            slot.outcome = DROPPED
        elif job.state in FINISHED:
            self._finished(slot, job, tiers[job.id], state)
        else:
            slot.started = slot.started or job.state == "leased"

    def _finished(self, slot: Slot, job: Job, tier: int, state: State) -> None:
        """Count the finished attempt once in the tier's class from its report and charge its core time to the day's
        budget, then take its outcome into the slot, storing its verdict, unless the retry policy runs it again."""
        uploaded = self._uploaded(job)
        if uploaded is not None and self.queue.tally(job):
            metrics.observe_unit(self.registry, CLASSES[tier], job.unit.kind, job.requirement.slots, uploaded[0])
            if self.spending is not None:
                self.spending.charge(job.requirement.slots, float(uploaded[0].get("seconds") or 0.0))
        if not self._rerun(job):
            slot.outcome = job.outcome
            self._remember(job, uploaded, state)

    def _rerun(self, job: Job) -> bool:
        """Move a finished job back to pending when the retry policy allows its outcome a rerun; return whether it
        moved."""
        result = UnitResult(job.outcome, job.attempts)
        return decide(result, self.settings.retries) == "retry" and self.queue.retry(job)

    def _remember(self, job: Job, uploaded: tuple[dict[str, Any], bytes] | None, state: State) -> None:
        """Store the verdict of a graded job from the report and log its worker uploaded, write a flaky verdict's flake
        rows once as JSON lines in its flakes object, and note the files its batch opened outside the repository's
        manifest under the manifest commit the grade keyed on; a lost or unrun job is no verdict."""
        if job.outcome in UNGRADED or uploaded is None:
            return
        report, log = uploaded
        passed = job.outcome == PASSED
        verdict = Verdict(
            job.unit.kind, job.unit.key, passed, passed and bool(report.get("flaky")), float(report.get("seconds", 0.0))
        )
        self.store.put(verdict, report, log)
        flakes = report.get("flakes")
        if verdict.flaky and flakes:
            rows = "".join(json.dumps(row) + "\n" for row in flakes)
            self.objects.write_once(flakes_path(verdict.kind, verdict.key), rows.encode())
        if report.get("outside") and state.manifest:
            note_outside(self.objects, state.repo, state.manifest, job.unit.members, report["outside"])

    def _uploaded(self, job: Job) -> tuple[dict[str, Any], bytes] | None:
        """Return the report and log a finished job's worker uploaded, the log empty when it uploaded none; None
        without a report."""
        report_path, log_path = upload_path(job.id, "report"), upload_path(job.id, "log")
        present = set(self.objects.keys(str(PurePosixPath(report_path).parent) + "/"))
        if report_path not in present:
            return None
        log = self.objects.read(log_path) if log_path in present else b""
        return json.loads(self.objects.read(report_path)), log

    def _post(self, state: State, now_ms: int) -> list[str]:
        """Post each check run whose target moved since its last post; a completed check run never moves again. A gate
        the grade lists as authoritative posts as lab/<gate> and every other check as lab-shadow/<gate>. A check run
        the grade already holds seeds the poster first, so a restarted scheduler advances it in place. A grade of a
        head pushed to a grade ref records each move for the relay and posts nothing, since GitHub does not hold the
        head before the relay pushes it. A post the App token or the network refuses holds every post of the
        repository for POST_HOLD_MS, and a post GitHub refuses holds the grade's posts for POST_HOLD_MS; either is
        recorded and its check run waits for the first pass after the hold."""
        moved = _moved(state)
        if not moved:
            return []
        if state.ref.startswith(GRADE_REFS):
            for check, status, conclusion in moved:
                state.posted[check] = [status, conclusion]
            return [f"left {check} {status} on {state.head} to the relay" for check, status, _ in moved]
        if now_ms < max(state.hold, self.held[state.repo]):
            return []
        poster = self.ports.checks(_slug(state.repo))
        actions: list[str] = []
        for check, status, conclusion in moved:
            shadow = check not in state.authoritative
            if check in state.runs:
                poster.seed(state.head, check, state.run, state.runs[check], str(state.posted[check][0]), shadow)
            output = self._output(state, check, status)
            try:
                state.runs[check] = poster.post(state.head, check, status, conclusion, output, shadow)
            except (GitAuthRefused, PostRefused, OSError) as error:
                until = now_ms + POST_HOLD_MS
                if isinstance(error, PostRefused):
                    state.hold = until
                else:
                    self.held[state.repo] = until
                actions.append(f"held {check} {status} on {state.head} for a later pass: {error}")
                break
            state.posted[check] = [status, conclusion]
            actions.append(f"posted {check} {status} on {state.head}")
        return actions

    def _output(self, state: State, check: str, status: str) -> Output:
        """Return a check run's output: its units, none for a skipped authoritative gate, the grade run, the grader
        revision, the head's commit page and, on a gate's check run, the stored log of each failed static unit."""
        refs = list(state.units) if check == REQUIRED else state.gates.get(check, [])
        units = tuple(Unit(slot.kind, slot.key, tuple(slot.members)) for slot in (state.units[ref] for ref in refs))
        title = f"{CLASSES[state.tier]} grade {status}"
        summary = f"{len(units)} units of {check} on {state.head} against {state.base}"
        logs = () if check == REQUIRED else self._logs([state.units[ref] for ref in refs])
        return Output(title, summary, units, state.run, state.grader, f"{state.repo}/commit/{state.head}", logs)

    def _logs(self, slots: Sequence[Slot]) -> tuple[tuple[Unit, str], ...]:
        """Return each failed static slot's unit with its stored log, secret patterns redacted; a unit whose log is
        empty or not stored adds none."""
        logs: list[tuple[Unit, str]] = []
        for slot in [slot for slot in slots if slot.kind == STATIC and slot.outcome == FAILED]:
            path = unit_path(slot.kind, slot.key, "log")
            if path in self.objects.keys(str(PurePosixPath(path).parent) + "/") and (log := self.objects.read(path)):
                text = redact(log.decode(errors="replace"), mode="strict")
                logs.append((Unit(slot.kind, slot.key, tuple(slot.members)), text))
        return tuple(logs)


def _unobserved(check: Mapping[str, Any]) -> None:
    """Leave a completed check unrecorded: the observer of an inbox that feeds no shadow recorder."""


class BusInbox:
    """Lab head events read from the bus consumer of `ci.lab.head.>`, each acknowledged once read, or terminated."""

    def __init__(
        self,
        consumer: Consumer,
        batch: int = BATCH,
        timeout_ms: int = FETCH_MS,
        observe: Callable[[Mapping[str, Any]], object] = _unobserved,
    ) -> None:
        self.consumer = consumer
        self.batch = batch
        self.timeout_ms = timeout_ms
        self.observe = observe

    def heads(self) -> list[Head]:
        """Return heads from successive batches until a short fetch or the intake deadline. A head's lane is the lane
        its envelope names, else its branch, else the head itself, and its ref the grade ref a lab remote notice names.
        An envelope missing a required head field is terminated with a logged warning. Completed checks reach the
        observer in delivery order; heads are acknowledged after the whole intake succeeds, so a later fetch or
        observer failure leaves them replayable.
        """
        heads: list[Head] = []
        read: list[Delivery] = []
        deadline = time.monotonic() + INTAKE_SECONDS
        while time.monotonic() < deadline:
            deliveries = self.consumer.fetch(self.batch, self.timeout_ms)
            for delivery in deliveries:
                data = cast(Mapping[str, Any], delivery.event.data)  # pragma: no mutate
                if delivery.event.type == CHECKS:
                    self._observe(delivery, data)
                    continue
                missing = [name for name in HEAD_FIELDS if name not in data]
                if missing:
                    logging.getLogger(__name__).warning(
                        "terminated lab head %s missing %s", delivery.event.id, ", ".join(missing)
                    )
                    delivery.term()
                    continue
                event = HeadEvent(*(str(data[name]) for name in HEAD_FIELDS))
                lane = str(data.get("lane") or data.get("branch") or event.head)
                heads.append(Head(event, lane, str(data.get("ref") or "")))
                read.append(delivery)
            if len(deliveries) < self.batch:
                break
        for delivery in read:
            delivery.ack()
        return heads

    def _observe(self, delivery: Delivery, check: Mapping[str, Any]) -> None:
        """Hand one completed check to the observer and acknowledge it; terminate one it cannot read with a logged
        reason."""
        try:
            self.observe(check)
        except (KeyError, TypeError) as error:
            logging.getLogger(__name__).warning(
                "terminated lab check %s unreadable: %s", delivery.event.id, type(error).__name__
            )
            delivery.term()
            return
        delivery.ack()


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


def authoritative_gates(block: Mapping[str, Any], tier: Priority) -> frozenset[str]:
    """Return the gates a delivery.yaml lab block lists as authoritative for a hotfix, pull request or merge group
    grade: lab.queue.authoritative for a merge group grade and lab.authoritative for the others."""
    if tier == Priority.QUEUE:
        queue: Mapping[str, Any] = block.get("queue", {})
        return frozenset(queue.get("authoritative", ()))
    return frozenset(block["authoritative"])


def frame(
    tier: Priority,
    tree: str,
    grader: str,
    gates: Mapping[str, tuple[Unit, ...]],
    authoritative: Sequence[str] = (),
    plant: bool = False,
) -> Plan:
    """Return a tier's plan: the lab's gates its `Gate — Required` rules require, and every other job they require as
    a gate without units, which passes, so only what the lab grades can turn the grade red.

    A pull request or hotfix grade runs with no mutation and no test scope. Only a plant pull request runs test
    units, so it is framed as a run of the full suite: its select scope reads full and it carries the jobs that
    scope requires, which the rules then demand of it. Each grades under each required job the units
    of every reusable workflow job it calls, which the expander files under the job, a slash and the called job, and
    names each authoritative gate it runs no unit of, which it posts as skipped; a merge group grade runs only the
    queue suite with no lost unit rerun; both keep the authoritative gates, which post as lab/<gate>; a promotion or
    backfill grade takes every lab gate, since those rules require no job by name, and lists none.
    """
    if tier >= Priority.PROMOTION:
        return Plan(tree, grader, dict(gates))
    listed = frozenset(authoritative)
    if tier == Priority.QUEUE:
        required = (*QUEUE_REQUIRED, FAIL_FAST, UNIT_JOBS[0])
        queued = {job: gates.get(job, ()) for job in required}
        return Plan(tree, grader, queued, frozenset(UNIT_JOBS[1:]), authoritative=listed)
    scope = "full" if tier == Priority.PULL_REQUEST and plant else "none"
    required = (*PULL_REQUIRED, *SCOPED.get(scope, ()))
    framed = {
        job: tuple(unit for key, units in gates.items() if key == job or key.startswith(f"{job}/") for unit in units)
        for job in required
    }
    idle = frozenset(gate for gate in authoritative if not framed.get(gate))
    return Plan(tree, grader, framed, outputs={"select": {"scope": scope}}, idle=idle, authoritative=listed)


@dataclass(frozen=True)
class StoredManifests:
    """Packed dependency manifests in the lab object store, one per dev commit under `objects.manifest_folder`; one of
    the newest `MANIFEST_DEPTH` commits with none stored is fetched from its dev push Tests run, off the scheduler pass
    unless another fetch is given, and stored once. The notes of the files test batches opened outside them sit under
    its `outside/<commit>/` folder, by the manifest commit their grade keyed on."""

    objects: Objects
    fetch: Fetch = field(default_factory=lambda: Background(Fetcher()))

    def __call__(self, repo: str, commits: Sequence[str]) -> dict[str, Any] | None:
        """Return the manifest of the first commit that has one stored, with the notes of its commit merged into its
        test records, or among the newest one fetched, which no note names yet; None when none has. Every call first
        removes the notes of commits with no stored manifest."""
        slug = _slug(repo)
        folder = manifest_folder(slug)
        stored = frozenset(self.objects.keys(folder))
        self._prune(folder, stored)
        for index, commit in enumerate(commits):
            path = f"{folder}{commit}.json"
            if path in stored:
                return self._merged(UNPACK(json.loads(self.objects.read(path))), folder, stored)
            packed = self.fetch(slug, commit) if index < MANIFEST_DEPTH else None
            if packed is not None:
                self.objects.write_once(path, packed)
                return UNPACK(json.loads(packed))
        return None

    def _prune(self, folder: str, stored: frozenset[str]) -> None:
        """Remove each note whose commit folder names no stored manifest, a note written without a commit folder
        included."""
        notes = folder + NOTES
        for name in [path for path in stored if path.startswith(notes)]:
            if f"{folder}{PurePosixPath(name).parent.name}.json" not in stored:
                self.objects.remove(name)

    def _merged(self, manifest: dict[str, Any], folder: str, stored: frozenset[str]) -> dict[str, Any]:
        """Return the manifest with the files of each note of its commit added to the outside list of every test record
        the note names, and of every test module the note names that no record holds to that module's list under
        `expand_tests.UNLISTED`, present only when a note names such a module."""
        records: dict[str, dict[str, Any]] = manifest["tests"]
        unlisted: dict[str, list[str]] = {}
        notes = f"{folder}{NOTES}{manifest['commit']}/"
        for name in sorted(path for path in stored if path.startswith(notes)):
            note = json.loads(self.objects.read(name))
            for test in note["tests"]:
                if test in records:
                    records[test]["outside"] = sorted({*records[test].get("outside", ()), *note["outside"]})
                elif "::" not in test:
                    unlisted[test] = sorted({*unlisted.get(test, ()), *note["outside"]})
        if unlisted:
            manifest[expand_tests.UNLISTED] = unlisted
        return manifest


def note_outside(objects: Objects, repo: str, commit: str, tests: Sequence[str], outside: Sequence[str]) -> None:
    """Write a batch's tests and the files it opened outside the manifest as one note of the repository's manifest
    commit, named by its content so the same batch and files write it once."""
    record: dict[str, object] = {"tests": tests, "outside": sorted(outside)}
    path = f"{manifest_folder(_slug(repo))}{NOTES}{commit}/{digest(record)}.json"
    objects.write_once(path, json.dumps(record).encode())


def keep_manifests(objects: Objects, units: Iterable[Unit]) -> int:
    """Store the batch manifest of each unit that carries one under its unit key in the object store, once, so its
    job's lease reads it there and no Redis record holds it; return how many this call wrote."""
    return sum(
        objects.write_once(unit_path(unit.kind, unit.key, "manifest"), unit.manifest.encode())
        for unit in units
        if unit.manifest
    )


def stored_manifest(objects: Objects, unit: Unit) -> str:
    """Return the manifest a test or mutant unit's lease carries, a test unit's batch manifest or a mutant unit's
    traced test files, read from its unit key's place in the object store; empty for another kind of unit or a unit
    with none stored."""
    if unit.kind not in MANIFEST_KINDS:
        return ""
    path = unit_path(unit.kind, unit.key, "manifest")
    if path not in objects.keys(str(PurePosixPath(path).parent) + "/"):
        return ""
    return objects.read(path).decode()


class MirrorExpander:
    """Units of a head read from a git mirror with ls-tree and show, the mirror fetching a sha GitHub lacks from the
    lab remote. Only the mutation units use a temporary checkout of the head, since their builders read files from
    disk."""

    def __init__(
        self,
        node: Path,
        root: Path,
        toolchain: Toolchain,
        manifests: Manifests,
        verdicts: Verdicts,
        lab: Lab | None = None,
    ) -> None:
        self.node = node
        self.root = root
        self.toolchain = toolchain
        self.manifests = manifests
        self.verdicts = verdicts
        self.lab = lab

    def _mirror(self, repo: str) -> Mirror:
        self.root.mkdir(parents=True, exist_ok=True)
        return Mirror(self.node, self.root / _slug(repo).replace("/", "--"), repo, self.lab)

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

    def _dev_head(self, repo: str, mirror: Mirror) -> str:
        """Return the repository's dev branch head as the remote lists it, fetched into the mirror; git fails when the
        remote lists no dev branch."""
        listing = subprocess.run(
            ["git", "ls-remote", "--exit-code", repo, f"refs/heads/{DEV}"], capture_output=True, text=True, check=True
        )
        head = listing.stdout.split()[0]
        mirror.fetch(head)
        return head

    def _on_dev(self, repo: str, path: str) -> str:
        """Return one file of the repository's dev branch head."""
        mirror = self._mirror(repo)
        return self._git(mirror, "show", f"{self._dev_head(repo, mirror)}:{path}")

    def merge_base(self, repo: str, head: str) -> str:
        """Return the merge base of a head with the repository's dev branch head, both fetched into the mirror; git
        fails when the head shares no history with dev."""
        mirror = self._mirror(repo)
        dev = self._dev_head(repo, mirror)
        mirror.fetch(head)
        return self._git(mirror, "merge-base", head, dev).strip()

    def reachable(self, repo: str, head: str) -> bool:
        """Return whether a ref every node mirror fetches reaches the head: a branch or pull request head ref of the
        repository names it, or the dev branch head, fetched into the mirror, descends from it. The head itself is
        never fetched, since GitHub may no longer serve a head no ref reaches."""
        listing = subprocess.run(
            ["git", "ls-remote", repo, "refs/heads/*", "refs/pull/*/head"], capture_output=True, text=True, check=True
        )
        if head in {line.split()[0] for line in listing.stdout.splitlines()}:
            return True
        mirror = self._mirror(repo)
        dev = self._dev_head(repo, mirror)
        return (
            subprocess.run(["git", "-C", str(mirror.store), "merge-base", "--is-ancestor", head, dev]).returncode == 0
        )

    def groups(self, repo: str) -> frozenset[str]:
        """Return the head of every merge group ref the repository lists; git fails when it cannot list them within
        GROUPS_TIMEOUT_S."""
        argv = ["git", "ls-remote", repo, MERGE_GROUPS]
        listing = subprocess.run(argv, capture_output=True, text=True, check=True, timeout=GROUPS_TIMEOUT_S)
        return frozenset(line.split()[0] for line in listing.stdout.splitlines())

    def trusted(self, repo: str) -> frozenset[str]:
        """Return the runner trust list on the repository's dev branch."""
        return frozenset(validate_actors(json.loads(self._on_dev(repo, ACTORS))))

    def retries(self, repo: str) -> Policy:
        """Return the retry policy of the lab block of delivery.yaml on the repository's dev branch."""
        return policy(_lab_block(self._on_dev(repo, DELIVERY)))

    def static_jobs(self, repo: str) -> frozenset[str]:
        """Return the gate id of every static job of the workflow on the repository's dev branch whose steps gate,
        read as `shadow.static_jobs` reads a tree."""
        mirror = self._mirror(repo)
        dev = self._dev_head(repo, mirror)
        workflow = self._git(mirror, "show", f"{dev}:{WORKFLOW.as_posix()}")
        return shadow.gate_ids(discover(workflow, reusable=self._workflows(mirror, dev)))

    def _exclusions(self, mirror: Mirror, grader: str, files: Mapping[str, str]) -> dict[str, str]:
        if static_exclusions.PATH not in files:
            return {}
        return static_exclusions.read(self._git(mirror, "show", f"{grader}:{static_exclusions.PATH}"))

    def _diffed(self, mirror: Mirror, request: GradeRequest, base: dict[str, str]) -> tuple[str, dict[str, str]]:
        """Return what a head's static units diff against: the merge base of the request's head and base with the
        files of its tree, or no merge base and the base's own files when the base is that merge base. Git fails
        when head and base share no history."""
        found = self._git(mirror, "merge-base", request.head, request.base).strip()
        if found == request.base:
            return "", base
        return found, self._files(mirror, found)

    def expand(self, request: GradeRequest) -> Plan:
        """Return one unit per supported static gate outside the protected exclusion catalog, with Actions retaining
        authority for jobs with exclusions, keyed by the files each gate reads at head and at the merge base of head
        and base, the unit job's test
        units of a plant pull request, merge group, promotion or backfill head and the mutation stats and mutant units of a promotion or
        backfill head, framed by the request's tier with the authoritative gates of the grader's delivery.yaml for a
        hotfix or pull request head, with the dev commit of the manifest its test units are keyed on. The plan names
        the merge base when it is not the base itself.
        """
        mirror = self._mirror(request.repo)
        grader = request.head if request.tier >= Priority.PROMOTION else request.base
        for sha in dict.fromkeys((request.head, request.base, grader)):
            mirror.fetch(sha)
        head, base = self._files(mirror, request.head), self._files(mirror, request.base)
        merge_base, diffed = self._diffed(mirror, request, base)
        workflow = self._git(mirror, "show", f"{grader}:{WORKFLOW.as_posix()}")
        registry = json.loads(self._git(mirror, "show", f"{grader}:{REGISTRY.as_posix()}"))
        excluded = self._exclusions(mirror, grader, head if grader == request.head else base)
        toolchain = replace(self.toolchain, lockfile=head.get(LOCKFILE, ""), pyproject=head.get("pyproject.toml", ""))
        gates: dict[str, list[Unit]] = {}
        for gate in discover(workflow, registry, self._workflows(mirror, grader)):
            if gate.missing or gate.name in excluded:
                continue
            inputs = StaticInputs(
                gate.name, _within(head, gate.paths), _within(diffed, gate.base_paths), grader, toolchain
            )
            gates.setdefault(gate.job, []).append(Unit(STATIC, static_key(inputs), (gate.name,)))
        commit = ""
        if request.tier >= Priority.QUEUE or request.tier == Priority.PULL_REQUEST and request.plant:
            manifest, contents = self._manifest(mirror, request), self._contents(mirror, head)
            commit = manifest["commit"]
            suite = expand_tests.units(manifest, contents, head, json.loads(contents.get(DURATIONS, b"{}")), toolchain)
            gates.setdefault(UNIT_JOBS[0], []).extend(
                self._tests(mirror, request, grader, manifest, suite, contents, toolchain)
            )
            if request.tier >= Priority.PROMOTION:
                gates.update(self._mutation(mirror, request, manifest, contents))
        listed = frozenset[str]()
        if request.tier < Priority.PROMOTION:
            listed = authoritative_gates(_lab_block(self._git(mirror, "show", f"{grader}:{DELIVERY}")), request.tier)
            listed = frozenset(static_exclusions.authority(listed, excluded))
        tree = self._git(mirror, "rev-parse", f"{request.head}^{{tree}}").strip()
        plan = frame(
            request.tier,
            tree,
            grader,
            {job: tuple(units) for job, units in gates.items()},
            sorted(listed),
            request.plant,
        )
        return replace(plan, manifest=commit, merge_base=merge_base)

    def _tests(
        self,
        mirror: Mirror,
        request: GradeRequest,
        grader: str,
        manifest: dict[str, Any],
        suite: tuple[Unit, ...],
        contents: Mapping[str, bytes],
        toolchain: Toolchain,
    ) -> tuple[Unit, ...]:
        """Return every test batch of the head, or for a merge group head the batches that hold a test its entries
        invalidate, unless the interaction rule sends the group to the whole suite, and one unit per test module its
        entries change that the manifest lists no test of."""
        if request.tier == Priority.PULL_REQUEST:
            return self._plant_tests(mirror, request, manifest, suite, contents, toolchain)
        if request.tier != Priority.QUEUE:
            return suite
        group = self._group(mirror, request)
        cached = {test for unit in suite if self.verdicts(unit.kind, unit.key) is not None for test in unit.members}
        limit = interaction.entry_limit(_lab_block(self._git(mirror, "show", f"{grader}:{DELIVERY}")))
        chosen = interaction.decide(group, manifest, cached, limit)
        changed = (path for entry in group.entries for path, _, _ in entry)
        unlisted = expand_tests.unlisted_units(manifest, contents, changed, toolchain)
        if chosen == interaction.FULL:
            return suite + unlisted
        return tuple(unit for unit in suite if chosen.intersection(unit.members)) + unlisted

    def _plant_tests(
        self,
        mirror: Mirror,
        request: GradeRequest,
        manifest: dict[str, Any],
        suite: tuple[Unit, ...],
        contents: Mapping[str, bytes],
        toolchain: Toolchain,
    ) -> tuple[Unit, ...]:
        changed = {
            path for path, _, _ in dep_manifest.changes(mirror.store, request.base, request.head) if path in contents
        }
        listed = tuple(unit for unit in suite if any(test.partition("::")[0] in changed for test in unit.members))
        return listed + expand_tests.unlisted_units(manifest, contents, changed, toolchain)

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
        its mutant units trace, built in a temporary checkout of the head: the files present at the head that hold a
        manifest test running such a function, the test modules the manifest lists no test of and the test modules
        the head changes against its base. Every mutant unit carries those files as its manifest, a JSON list, and
        the hash of each such changed test module enters its key with the unlisted test hashes; none when the head
        changes no function of a measured Python file."""
        from scripts.ci_lab import executors, expand_mutation

        with self._checkout(mirror, request.head) as root:
            changes = scope.changes_since(root, request.base, request.head)
            if not changes:
                return {}
            covering = expand_mutation.covering(root, manifest, expand_tests.fingerprints(manifest, contents), changes)
            changed = expand_mutation.changed_tests(root, request.base, request.head)
            keyed = expand_tests.unlisted_test_hashes(manifest, contents)
            keyed.update({path: expand_mutation.file_digest(root / path) for path in changed})
            mutants = expand_mutation.mutant_units(root, changes, covering, keyed, timeout=executors.UNIT_TIMEOUT_S)
            if not mutants:
                return {}
            owners = {executors.function_of(unit.members[0]) for unit in mutants}
            tests = {test.partition("::")[0] for name in owners for test in covering.get(name, {})}
            tests.update(expand_tests.unlisted_modules(manifest, contents))
            tests.update(changed)
            files = sorted(path for path in tests if (root / path).is_file())
            stats = expand_mutation.stats_units(root, files, executors.OPTIONS)
        traced = json.dumps(files)
        named = [replace(unit, manifest=traced) for unit in mutants]
        return {job: list(units) for job, units in ((STATS_JOB, stats), (MUTANT_JOB, named)) if units}

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


class _Unshipped(metrics.Sink):
    """The table sink of the scheduler's shadow recorder: the scheduler runs no metrics outbox, so lab_shadow table rows
    are dropped while each shadow row still lands in Redis and the object store."""

    def append(self, table: metrics_outbox.Table, rows: list[dict[str, object]]) -> None:
        """Drop the rows."""


def _shadow_observer(
    scheduler: Scheduler, sources: shadow.Sources, repository: str
) -> Callable[[Mapping[str, Any]], None]:
    """Return the observer that folds each completed check of the repository into a shadow recorder on the
    scheduler's Redis, objects and registry; a check of another repository is left unrecorded."""
    recorder = shadow.Shadow(scheduler.client, scheduler.objects, _Unshipped(), scheduler.registry, sources)
    compared = _slug(repository).removesuffix(".git")

    def observe(check: Mapping[str, Any]) -> None:
        if _slug(check["repository"]["html_url"]) == compared:
            recorder.observe(check)

    return observe


def build(
    environ: Mapping[str, str], client: Any, objects: Objects, consumer: Consumer, spending: budget.Budget
) -> Scheduler:
    """Return the service's scheduler over its clients and the day's budget, trusting the runner trust list of the
    repository's dev, its mirrors falling back to the lab remote the environment names. Once `AGENTIHOOKS_LAB_APP`
    names the lab's GitHub App, its bus inbox hands each completed check of the repository to a shadow recorder on
    the scheduler's registry; without it completed checks are acknowledged unrecorded."""
    toolchain = Toolchain(sys.version.split()[0], {}, "", "", environ.get(IMAGE, ""), {})
    node, root = Path(environ[contract.GIT_MIRROR]), Path(environ[MIRROR_STORE])
    expander = MirrorExpander(node, root, toolchain, StoredManifests(objects), Store(client, objects).get, lab(environ))
    checks: dict[str, Poster] = {}

    def poster(repo: str) -> Poster:
        if repo not in checks:
            checks[repo] = Checks(repo, registry=scheduler.registry)
        return checks[repo]

    inbox = BusInbox(consumer)
    hotfixes, pods = control.Priorities(client, _now_ms), worker_pods(environ)
    ports = Ports(inbox, expander, poster, hotfixes, pods, stale_heads.OpenHeads())
    repository = environ[REPOSITORY]
    settings = Settings(expander.trusted(repository), retries=expander.retries(repository))
    scheduler = Scheduler(client, objects, ports, settings, spending)
    app = environ.get(shadow_report.LAB_APP)
    if app:
        sources = shadow.Sources(expander.static_jobs(repository), app)
        inbox.observe = _shadow_observer(scheduler, sources, repository)
    return scheduler


def serve(
    scheduler: Scheduler,
    relaying: Relaying,
    interval_s: float,
    passes: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> int:
    """Run passes every interval, forever when passes is zero, ticking the relay after each pass and printing each
    action of both; a Redis error ends only its pass, which prints one line naming the pass and the error, and the next
    pass runs on schedule; return 0."""
    from redis import RedisError

    done = 0
    while passes == 0 or done < passes:
        try:
            for action in scheduler.tick(int(clock() * 1000)):
                print(action, flush=True)
            for action in relaying.tick():
                print(action, flush=True)
        except RedisError as error:
            print(f"pass {done + 1} skipped on {type(error).__name__}: {error}", flush=True)
        done += 1
        sleep(interval_s)
    return 0


def listen(
    scheduler: Scheduler,
    leases: Leases,
    routes: lease_api.ControlRoutes,
    spending: budget.Budget,
    clock: Callable[[], int],
    ports: tuple[int, int, int],
    mirror: Path | None = None,
) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer, ThreadingHTTPServer]:
    """Bind the lease, capacity, grades, control and metrics routes, then start the first load build.
    Both capacity ports serve the same cached budgeted load, refreshed by the metrics
    serve loop after REFRESH_MS; no read waits for a build, so before the first one
    finishes capacity answers 503 and a scrape the registry alone. Grade lookups remain
    per request. Each scrape observes load and mirror ages. The caller runs each serve_forever.
    """

    def snapshot(now_ms: int) -> Load:
        return budget.observe(scheduler.load(now_ms), spending.status())

    latest = metrics.Latest(snapshot, clock, REFRESH_MS)

    def scrape() -> Load | None:
        if mirror is not None:
            metrics.observe_mirror(scheduler.registry, mirror, clock())
        built = latest.peek()
        if built is None:
            return None
        load, built_ms = built
        metrics.observe_age(scheduler.registry, built_ms, clock())
        return load

    def capacity() -> Load | None:
        built = latest.peek()
        return None if built is None else built[0]

    api, steering, exposition = ports
    lease = partial(lease_api.answer, leases, Uploads(scheduler.objects))
    port = service.Routes(lease, capacity, scheduler.grades_of)
    servers = (
        service.serve("", api, port),
        lease_api.serve_control("", steering, routes),
        metrics.serve("", exposition, scrape, scheduler.registry, latest.refresh),
    )
    latest.start()
    return servers


def _now_ms() -> int:
    """Return the wall clock in milliseconds."""
    return int(time.time() * 1000)


def _sleep_ms(ms: int) -> None:
    """Sleep the milliseconds."""
    time.sleep(ms / 1000)


def storage(environ: Mapping[str, str]) -> S3Objects:
    """Return the lab objects in the MinIO bucket the environment names, through an S3 client that signs worker
    upload URLs with SigV4 over the host alone, so a worker PUT may carry any Content-Type."""
    import boto3  # pyright: ignore[reportMissingTypeStubs]
    from botocore.config import Config  # pyright: ignore[reportMissingTypeStubs]

    s3: Any = boto3.client(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        "s3",
        endpoint_url=environ[MINIO_ENDPOINT],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        aws_access_key_id=environ[MINIO_ACCESS_KEY],
        aws_secret_access_key=environ[MINIO_SECRET_KEY],
    )
    return S3Objects(s3, environ[MINIO_BUCKET], "")


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Run the scheduler service from its mounted runtime config: the lease API, capacity and grades on the scheduler
    port, the control routes on the control port, metrics on the metrics port and a pass every interval."""
    import redis

    from scripts.ci_lab import relay
    from scripts.events.config import BusConfig
    from scripts.events.factory import open_bus

    parser = argparse.ArgumentParser(prog="python -m scripts.ci_lab scheduler")
    parser.add_argument("--interval", type=float, default=INTERVAL_S)
    parser.add_argument("--passes", type=int, default=0)
    arguments = parser.parse_args(argv)
    missing = [name for name in GITHUB_APP_SETTINGS if not environ.get(name)]
    if missing:
        print(f"ci-lab scheduler refuses to start: {', '.join(missing)} not set", file=sys.stderr)
        return 1
    runtime = load_runtime(Path(environ[contract.RUNTIME_CONFIG]))
    client: Any = redis.Redis.from_url(environ[contract.REDIS_URL], decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    objects = storage(environ)
    heads = open_bus(replace(BusConfig.from_environ(environ), provision=False)).lab_heads
    spending = budget.Budget(client, _now_ms, budget.policy(runtime))
    scheduler = build(environ, client, objects, heads.consumer(DURABLE, SUBJECT), spending)
    relaying = relay.build(environ, scheduler, heads.consumer(relay.DURABLE, relay.SUBJECT))
    if environ.get(REINDEX_ON_START) == "true":
        print(f"reindexed {scheduler.store.reindex()} verdicts", flush=True)
    manifests = partial(stored_manifest, scheduler.objects)
    leases = Leases(
        scheduler.queue,
        LEASE_TTL_MS,
        _now_ms,
        _sleep_ms,
        LEASE_POLL_MS,
        scheduler.ports.pods,
        manifests,
        environ.get(IMAGE, ""),
        pod_images(scheduler.ports.pods),
    )
    routes = lease_api.ControlRoutes(client, environ.get(contract.CONTROL_TOKEN, ""), _now_ms)
    ports = (contract.SCHEDULER_PORT, contract.CONTROL_PORT, contract.METRICS_PORT)
    for server in listen(scheduler, leases, routes, spending, _now_ms, ports, Path(environ[contract.GIT_MIRROR])):
        Thread(target=server.serve_forever, daemon=True).start()
    return serve(scheduler, relaying, arguments.interval, arguments.passes, time.time, time.sleep)
