"""Executor adapters of the lab worker pod: each turns a lease into its executor's inputs, runs the executor on trees
the pod cuts from its git mirror and answers the worker with the unit's outcome, a JSON report and its log.

A static unit runs the gate it names from the grader tree's workflow over the head, the base and grader trees bound
read only. A test unit runs its batch on the venv of the head's lockfile digest with the pod's dependency manifest and
flake ledger. A stats or mutant unit runs the mutation runner in the sandbox from a private tree of the head in which
the functions the head changes against the lease's base sit behind trampolines, with the pod's stats cache, kill
memory, unit budget and lane cap; a mutant unit's tests are the ones the stats records of the test files its manifest
names show reaching its function. `agentihooks ci-lab-worker` builds the pod from the worker contract environment and
runs the worker until SIGTERM, serving its metrics registry on the contract metrics port.
"""

import argparse
import contextlib
import hashlib
import json
import os
import signal
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Any

from scripts import ci_cpu_quota, ci_wiring
from scripts.ci_lab import caches, contract, metrics, run_mutation, run_static, run_tests, venvs, worker, worktrees
from scripts.ci_lab.outcomes import FAILED, PASSED
from scripts.ci_lab.sandbox import PortAllocator, Sandbox
from scripts.ci_lab.static_gates import REGISTRY, SECRETS, WORKFLOW, StaticGate, discover
from scripts.ci_lab.units import Unit
from scripts.ci_lab.venvs import Build
from scripts.ci_lab.worker import Executor, Finished, Lease
from scripts.ci_lab.worktrees import Mirror, Worktrees
from scripts.ci_mutation import scope

__all__ = [
    "MUTANT",
    "STATIC",
    "STATS",
    "TEST",
    "Mutation",
    "Pod",
    "Suite",
    "Trees",
    "covering",
    "executors",
    "function_of",
    "gate",
    "lockfile_digest",
    "main",
    "mutation",
    "settings",
    "static",
    "tests",
]

STATIC = "static"
TEST = "test"
STATS = run_mutation.STATS
MUTANT = "mutant"
KILLED = "killed"
INPUTS_BASE = "inputs.base"
MASK = b"***"
MUTANT_SUFFIX = "__mutmut_"
OPTIONS = ("-q", "-p", "no:cacheprovider")
WHITELIST = {"LANG": "C.UTF-8"}
TREES = 8
FIRST_PORT = 20_000
LAST_PORT = 29_999
PORT_WIDTH = 10
UNIT_TIMEOUT_S = worker.UNIT_TIMEOUT_SECONDS


@dataclass
class Trees:
    """The pod's worktrees behind one lock, since units on several threads cut, share and drop them."""

    worktrees: Worktrees
    lock: threading.Lock = field(default_factory=threading.Lock)

    @contextlib.contextmanager
    def shared(self, *heads: str) -> Generator[tuple[list[Path], list[bool]]]:
        """Lease the read only tree of each revision for the block, with whether the store fetched each, and release
        every one it leased."""
        leased: list[str] = []
        try:
            trees: list[Path] = []
            fetched: list[bool] = []
            for head in heads:
                with self.lock:
                    tree, cut = self.worktrees.lease(head)
                trees.append(tree)
                fetched.append(cut)
                leased.append(head)
            yield trees, fetched
        finally:
            for head in leased:
                with self.lock:
                    self.worktrees.release(head)

    @contextlib.contextmanager
    def owned(self, head: str, base: str) -> Generator[tuple[Path, bool]]:
        """Cut a private writable tree of the revision for the block, with the base its unit compares it against in
        the store and whether the store fetched the revision, and drop it after."""
        with self.lock:
            tree, fetched = self.worktrees.own(head, base)
        try:
            yield tree, fetched
        finally:
            with self.lock:
                self.worktrees.drop(tree)


@dataclass(frozen=True)
class Pod:
    """What every adapter reads: the trees, the folder units run, report and keep venvs in, the sandbox ports and
    whitelist, the unit time limit, the clock that times a unit and the secrets static steps read, by name."""

    trees: Trees
    root: Path
    ports: PortAllocator
    whitelist: Mapping[str, str]
    timeout: float
    clock: Callable[[], float] = time.monotonic
    secrets: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def runs(self) -> Path:
        """The folder each unit's run, log and report folders sit in."""
        return self.root / "runs"

    @property
    def venvs(self) -> Path:
        """The folder of the venvs built per lockfile digest."""
        return self.root / "venvs"

    @property
    def build(self) -> Build:
        """The sandbox a head's venv build runs in: the pod's ports, whitelist and unit time limit."""
        return Build(self.ports, self.whitelist, self.timeout)


@dataclass(frozen=True)
class Suite:
    """The test adapter's inputs beyond the lease: the dependency manifest file and the flake ledger."""

    manifest: Path
    flakes: Path


@dataclass(frozen=True)
class Mutation:
    """The mutation adapter's inputs beyond the lease: the stats cache, the kill memory, a unit's time budget and the
    most mutants it grades at once."""

    cache: Path
    killers: Path
    budget: float
    cap: int


def lockfile_digest(tree: Path) -> str:
    """Return the sha256 of the tree's lockfile, the key of its venv."""
    return hashlib.sha256((tree / venvs.LOCKFILE).read_bytes()).hexdigest()


def _report(pod: Pod, record: Mapping[str, Any]) -> Path:
    """Write a unit's JSON report into a fresh folder under the pod's runs and return its path."""
    path = Path(tempfile.mkdtemp(dir=pod.runs)) / "report.json"
    path.write_text(json.dumps(record))
    return path


def gate(grader_tree: Path, name: str) -> StaticGate:
    """Return the gate named name that the grader tree's workflow runs, as the scheduler discovered it."""
    registry = json.loads((grader_tree / REGISTRY).read_text())
    workflow = (grader_tree / WORKFLOW).read_text()
    found = {item.name: item for item in discover(workflow, registry, ci_wiring.load(grader_tree))}
    return found[name]


def _context(lease: Lease, found: StaticGate, secrets: Mapping[str, str]) -> dict[str, str]:
    """The context a static unit's steps read: the head, base and job of the lease as their GitHub fields, the pull
    request event name, attempt and run number 1, and each secret the gate reads by its name, empty when the pod holds
    no secret of that name."""
    named = {name: secrets.get(name.removeprefix(SECRETS), "") for name in found.reads if name.startswith(SECRETS)}
    return {
        INPUTS_BASE: lease.base,
        "github.sha": lease.head,
        "github.event.pull_request.head.sha": lease.head,
        "github.event.pull_request.base.sha": lease.base,
        "github.event_name": "pull_request",
        "github.run_id": lease.job,
        "github.run_attempt": "1",
        "github.run_number": "1",
        **named,
    }


def _masked(text: bytes, secrets: Sequence[str]) -> bytes:
    """The text with each secret value replaced by ***, the longest first."""
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value.encode(), MASK)
    return text


def static(pod: Pod, lease: Lease) -> Finished:
    """Run the static gate the lease names over its head, with its base and grader trees and the context of its lease
    fields and the secrets its steps read, on the venv of the head's lockfile; a gate whose step rewrites its tree
    runs in a private tree of the head, dropped when it ends, and every other in the shared one; the report counts
    the git and uv cache lookups it made, and each secret value the steps printed reads *** in the log and the
    summary, as Actions masks it."""
    with (
        pod.trees.shared(lease.head, lease.base, lease.grader) as ((head, base, grader), fetched),
        contextlib.ExitStack() as private,
    ):
        found = gate(grader, lease.unit.members[0])
        venv, built = venvs.venv_for(lockfile_digest(head), head, pod.venvs, pod.build)
        context = _context(lease, found, pod.secrets)
        options = run_static.Settings(pod.runs, venv, pod.ports, pod.whitelist, pod.timeout, context)
        tree = private.enter_context(pod.trees.owned(lease.head, lease.base))[0] if found.rewrites_tree else head
        started = pod.clock()
        result = run_static.run(found, tree, base, grader, options)
        seconds = pod.clock() - started
    secrets = [value for name, value in context.items() if name.startswith(SECRETS) and value]
    if secrets:
        result.log.write_bytes(_masked(result.log.read_bytes(), secrets))
    summary = _masked(result.report.read_bytes(), secrets).decode()
    record = {"gate": result.gate, "code": result.code, "skipped": result.skipped, "summary": summary}
    lookups = caches.report(fetched, [built])
    report = _report(pod, {**record, "seconds": seconds, caches.KEY: lookups})
    return Finished(PASSED if result.green else FAILED, report, result.log)


def tests(pod: Pod, suite: Suite, lease: Lease) -> Finished:
    """Run the lease's test batch on its head with the venv of the head's lockfile digest, the manifest and the flake
    ledger, on as many workers as its slots; the report counts the git and uv cache lookups it made and carries the
    flake ledger rows of a batch its retry passed, with the unit key as run and the attempt count as run_attempt."""
    with pod.trees.shared(lease.head) as ((tree,), fetched):
        venv, built = venvs.venv_for(lockfile_digest(tree), tree, pod.venvs, pod.build)
        manifest = json.loads(lease.unit.manifest)
        lab = run_tests.Lab(pod.runs, pod.ports, pod.whitelist, manifest, suite.flakes, pod.timeout, False)
        started = pod.clock()
        verdict = run_tests.run(lease.unit.members, tree, venv, lease.slots, lab)
        seconds = pod.clock() - started
    record = {
        "flaky": verdict.flaky,
        "outside": list(verdict.outside),
        "durations": dict(verdict.durations),
        "attempts": verdict.attempts,
        "seconds": seconds,
        caches.KEY: caches.report(fetched, [built]),
        "flakes": [{**row, "run": lease.unit.key, "run_attempt": verdict.attempts} for row in verdict.flakes],
    }
    return Finished(verdict.outcome, _report(pod, record), verdict.log)


def _trampolined(tree: Path, base: str, head: str) -> list[str]:
    """Put the functions head changes against base behind their trampolines in the tree and return the names of the
    mutants written."""
    changes = scope.changes_since(tree, base, head)
    return [name for path in sorted(changes) for name in run_mutation.trampoline(tree, path, changes[path])]


def _traced(unit: Unit, written: Sequence[str]) -> list[str]:
    """Return the test files a mutant unit's manifest names for its stats, refusing a unit that carries none and one
    with a member the tree holds behind no trampoline."""
    if not unit.manifest:
        raise ValueError(f"mutant unit {unit.key} carries no manifest of the test files to trace")
    missing = sorted(set(unit.members).difference(written))
    if missing:
        raise ValueError(f"mutant unit {unit.key} names mutants its tree holds behind no trampoline: {missing}")
    return json.loads(unit.manifest)


def function_of(mutant: str) -> str:
    """Return the function a mutant name mutates."""
    return mutant.rpartition(MUTANT_SUFFIX)[0]


def covering(records: Sequence[Mapping[str, Any]], function: str) -> dict[str, float]:
    """Return each test a stats record shows reaching the function, with the seconds that record measured it."""
    return {test: record["durations"][test] for record in records for test in record["tests"].get(function, [])}


def _run(pod: Pod, grader: Path, venv: Path, request: Mapping[str, Any]) -> tuple[dict[str, Any] | None, Path]:
    """Run one mutation runner request in the sandbox from the grader tree, bound read only, so the grader's copy of
    the runner grades the tree the request names; the result is None when the runner failed."""
    folder = Path(tempfile.mkdtemp(dir=pod.runs))
    source, output = folder / "request.json", folder / "result.json"
    source.write_text(json.dumps(request))
    box = Sandbox(pod.runs, grader, pod.ports, pod.whitelist, (grader,))
    argv = [str(venv / "bin" / "python"), "-m", run_mutation.__name__, str(source), str(output)]
    outcome = box.run(argv, pod.timeout)
    return (json.loads(output.read_text()) if outcome.code == 0 else None), outcome.log


def mutation(pod: Pod, settings: Mutation, lease: Lease) -> Finished:
    """Run a stats unit with the tree as root and the stats cache, or a mutant unit with the kill memory, budget, cap
    and the tests the stats records of the test files its manifest names show reaching its function, in a private
    tree of the head that holds behind trampolines the functions the head changes against the lease's base,
    graded by the grader tree's copy of the mutation runner; the report counts the git and uv cache lookups it made."""
    unit = lease.unit
    with (
        pod.trees.shared(lease.grader) as ((grader,), shared),
        pod.trees.owned(lease.head, lease.base) as (tree, fetched),
    ):
        venv, built = venvs.venv_for(lockfile_digest(tree), tree, pod.venvs, pod.build)
        written = _trampolined(tree, lease.base, lease.head)
        files = list(unit.members) if unit.kind == STATS else _traced(unit, written)
        started = pod.clock()
        named = {"kind": unit.kind, "key": unit.key, "options": list(OPTIONS), "tree": str(tree)}
        stats = {**named, "kind": STATS, "root": str(tree), "cache": str(settings.cache)}
        result, log = _run(pod, grader, venv, {**stats, "members": files})
        if result is not None and unit.kind == MUTANT:
            tests = covering(result["stats"], function_of(unit.members[0]))
            graded = {"members": list(unit.members), "killers": str(settings.killers), "tests": tests}
            limits = {"budget": settings.budget, "cap": settings.cap}
            result, log = _run(pod, grader, venv, {**named, **graded, **limits})
        seconds = pod.clock() - started
    passed = result is not None and all(row["status"] == KILLED for row in result.get("rows", ()))
    lookups = caches.report([*shared, fetched], [built])
    return Finished(
        PASSED if passed else FAILED, _report(pod, {**(result or {}), "seconds": seconds, caches.KEY: lookups}), log
    )


def executors(pod: Pod, suite: Suite, settings: Mutation) -> dict[str, Executor]:
    """Return the adapter of every unit kind the pod runs."""
    graded = partial(mutation, pod, settings)
    return {STATIC: partial(static, pod), TEST: partial(tests, pod, suite), STATS: graded, MUTANT: graded}


def settings(environ: Mapping[str, str]) -> tuple[Pod, Suite, Mutation]:
    """Return the pod, suite and mutation settings the worker contract environment names."""
    allowed = worker.environment(environ)
    root = Path(allowed[contract.WORK_ROOT])
    node = Path(allowed[contract.GIT_MIRROR])
    mirror = Mirror(node, root / "store", str(node), worktrees.lab(allowed), allowed.get(contract.REPOSITORY, ""))
    trees = Trees(Worktrees(mirror, root / "trees", TREES))
    ports = PortAllocator(FIRST_PORT, LAST_PORT, PORT_WIDTH)
    prefix = contract.SECRET_PREFIX
    secrets = {name.removeprefix(prefix): value for name, value in allowed.items() if name.startswith(prefix)}
    pod = Pod(trees, root, ports, WHITELIST, UNIT_TIMEOUT_S, secrets=secrets)
    for folder in (pod.runs, pod.venvs):
        folder.mkdir(parents=True, exist_ok=True)
    suite = Suite(Path(allowed[contract.MANIFEST]), Path(allowed[contract.FLAKES]))
    budget, cap = float(allowed[contract.MUTATION_BUDGET]), int(allowed[contract.MUTATION_CAP])
    return pod, suite, Mutation(Path(allowed[contract.MUTATION_CACHE]), Path(allowed[contract.KILLERS]), budget, cap)


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Run the worker over the adapters until SIGTERM stops it, then let its running units drain; the contract
    metrics port serves the worker's registry until the drain ends. The work root is emptied first, so a restart on
    another image starts from an empty root as a new pod would."""
    argparse.ArgumentParser(prog="agentihooks ci-lab-worker").parse_args(argv)
    worktrees.empty(Path(worker.environment(environ)[contract.WORK_ROOT]))
    pod, suite, graded = settings(environ)
    cpus = os.cpu_count() or 1
    service = worker.build(
        environ, executors(pod, suite, graded), socket.gethostname(), cpus, ci_cpu_quota.quota_cpus()
    )

    def stop(signum: int, frame: FrameType | None) -> None:
        """Stop leasing once SIGTERM arrives."""
        service.stop()

    signal.signal(signal.SIGTERM, stop)
    with metrics.serve_registry("", contract.METRICS_PORT, metrics.Registry()) as exposition:
        threading.Thread(target=exposition.serve_forever).start()
        try:
            service.run()
        finally:
            exposition.shutdown()
    return 0
