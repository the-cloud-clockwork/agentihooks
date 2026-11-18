"""Executor adapters of the lab worker pod: each turns a lease into its executor's inputs, runs the executor on trees
the pod cuts from its git mirror and answers the worker with the unit's outcome, a JSON report and its log.

A static unit runs the gate it names from the grader tree's workflow over the head, the base and grader trees bound
read only. A test unit runs its batch on the venv of the head's lockfile digest with the pod's dependency manifest and
flake ledger. A stats or mutant unit runs the mutation runner in the sandbox from a private tree of the head whose
mutated sources sit behind trampolines, with the pod's stats cache, kill memory, unit budget and lane cap; a mutant
unit's tests are the ones the stats records of that tree show reaching its function. `agentihooks ci-lab-worker` builds
the pod from the worker contract environment and runs the worker until SIGTERM.
"""

import argparse
import contextlib
import hashlib
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import FrameType
from typing import Any

from scripts import ci_cpu_quota
from scripts.ci_lab import contract, run_mutation, run_static, run_tests, venvs, worker
from scripts.ci_lab.outcomes import FAILED, PASSED
from scripts.ci_lab.sandbox import PortAllocator, Sandbox
from scripts.ci_lab.static_gates import REGISTRY, WORKFLOW, StaticGate, discover
from scripts.ci_lab.worker import Executor, Finished, Lease
from scripts.ci_lab.worktrees import Mirror, Worktrees

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
LOCKFILE = "uv.lock"
PYPROJECT = "pyproject.toml"
INPUTS_BASE = "inputs.base"
MUTANT_SUFFIX = "__mutmut_"
TEST_FOLDER = "tests/"
TEST_PREFIX = "test_"
PYTHON = ".py"
OPTIONS = ("-q", "-p", "no:cacheprovider")
WHITELIST = {"LANG": "C.UTF-8"}
TREES = 8
FIRST_PORT = 20_000
LAST_PORT = 29_999
PORT_WIDTH = 10
UNIT_TIMEOUT_S = 1_800.0


@dataclass
class Trees:
    """The pod's worktrees behind one lock, since units on several threads cut, share and drop them."""

    worktrees: Worktrees
    lock: threading.Lock = field(default_factory=threading.Lock)

    @contextlib.contextmanager
    def shared(self, *heads: str) -> Generator[list[Path]]:
        """Lease the read only tree of each revision for the block and release every one it leased."""
        leased: list[str] = []
        try:
            trees: list[Path] = []
            for head in heads:
                with self.lock:
                    trees.append(self.worktrees.lease(head))
                leased.append(head)
            yield trees
        finally:
            for head in leased:
                with self.lock:
                    self.worktrees.release(head)

    @contextlib.contextmanager
    def owned(self, head: str) -> Generator[Path]:
        """Cut a private writable tree of the revision for the block and drop it after."""
        with self.lock:
            tree = self.worktrees.own(head)
        try:
            yield tree
        finally:
            with self.lock:
                self.worktrees.drop(tree)


@dataclass(frozen=True)
class Pod:
    """What every adapter reads: the trees, the folder units run, report and keep venvs in, the sandbox ports and
    whitelist, the unit time limit and the clock that times a unit."""

    trees: Trees
    root: Path
    ports: PortAllocator
    whitelist: Mapping[str, str]
    timeout: float
    clock: Callable[[], float] = time.monotonic

    @property
    def runs(self) -> Path:
        """The folder each unit's run, log and report folders sit in."""
        return self.root / "runs"

    @property
    def venvs(self) -> Path:
        """The folder of the venvs built per lockfile digest."""
        return self.root / "venvs"


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
    return hashlib.sha256((tree / LOCKFILE).read_bytes()).hexdigest()


def _report(pod: Pod, record: Mapping[str, Any]) -> Path:
    """Write a unit's JSON report into a fresh folder under the pod's runs and return its path."""
    path = Path(tempfile.mkdtemp(dir=pod.runs)) / "report.json"
    path.write_text(json.dumps(record))
    return path


def gate(grader_tree: Path, name: str) -> StaticGate:
    """Return the gate named name that the grader tree's workflow runs, as the scheduler discovered it."""
    registry = json.loads((grader_tree / REGISTRY).read_text())
    found = {item.name: item for item in discover((grader_tree / WORKFLOW).read_text(), registry)}
    return found[name]


def static(pod: Pod, lease: Lease) -> Finished:
    """Run the static gate the lease names over its head, with its base and grader trees and its base revision as
    inputs.base, on the venv of the head's lockfile."""
    with pod.trees.shared(lease.head, lease.base, lease.grader) as (head, base, grader):
        found = gate(grader, lease.unit.members[0])
        venv = venvs.venv_for(lockfile_digest(head), head, pod.venvs)
        context = {INPUTS_BASE: lease.base}
        options = run_static.Settings(pod.runs, venv, pod.ports, pod.whitelist, pod.timeout, context)
        started = pod.clock()
        result = run_static.run(found, head, base, grader, options)
        seconds = pod.clock() - started
    summary = result.report.read_text()
    record = {"gate": result.gate, "code": result.code, "skipped": result.skipped, "summary": summary}
    return Finished(PASSED if result.green else FAILED, _report(pod, {**record, "seconds": seconds}), result.log)


def tests(pod: Pod, suite: Suite, lease: Lease) -> Finished:
    """Run the lease's test batch on its head with the venv of the head's lockfile digest, the manifest and the flake
    ledger, on as many workers as its slots."""
    with pod.trees.shared(lease.head) as (tree,):
        venv = venvs.venv_for(lockfile_digest(tree), tree, pod.venvs)
        manifest = json.loads(suite.manifest.read_text())
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
    }
    return Finished(verdict.outcome, _report(pod, record), verdict.log)


def _tracked(tree: Path) -> list[str]:
    """List the Python files git tracks in the tree."""
    listing = subprocess.run(["git", "-C", str(tree), "ls-files", "-z"], capture_output=True, check=True)
    return [path for path in os.fsdecode(listing.stdout).split("\0") if path.endswith(PYTHON)]


def _trampolined(tree: Path) -> None:
    """Rewrite every Python file under the tree's mutmut source paths behind its trampolines."""
    roots: list[str] = tomllib.loads((tree / PYPROJECT).read_text())["tool"]["mutmut"]["source_paths"]
    for path in _tracked(tree):
        if path.startswith(tuple(roots)):
            run_mutation.trampoline(tree, path)


def _test_files(tree: Path) -> list[str]:
    """List the tracked test modules of the tree."""
    return [path for path in _tracked(tree) if path.startswith(TEST_FOLDER) and Path(path).name.startswith(TEST_PREFIX)]


def function_of(mutant: str) -> str:
    """Return the function a mutant name mutates."""
    return mutant.rpartition(MUTANT_SUFFIX)[0]


def covering(records: Sequence[Mapping[str, Any]], function: str) -> dict[str, float]:
    """Return each test a stats record shows reaching the function, with the seconds that record measured it."""
    return {test: record["durations"][test] for record in records for test in record["tests"].get(function, [])}


def _run(pod: Pod, tree: Path, venv: Path, request: Mapping[str, Any]) -> tuple[dict[str, Any] | None, Path]:
    """Run one mutation runner request in the sandbox from the tree; the result is None when the runner failed."""
    folder = Path(tempfile.mkdtemp(dir=pod.runs))
    source, output = folder / "request.json", folder / "result.json"
    source.write_text(json.dumps(request))
    box = Sandbox(pod.runs, tree, pod.ports, pod.whitelist)
    argv = [str(venv / "bin" / "python"), "-m", run_mutation.__name__, str(source), str(output)]
    outcome = box.run(argv, pod.timeout)
    return (json.loads(output.read_text()) if outcome.code == 0 else None), outcome.log


def mutation(pod: Pod, settings: Mutation, lease: Lease) -> Finished:
    """Run a stats unit with the tree as root and the stats cache, or a mutant unit with the kill memory, budget, cap
    and the tests the tree's stats records show reaching its function, in a private trampolined tree of the head."""
    unit = lease.unit
    named = {"kind": unit.kind, "key": unit.key, "options": list(OPTIONS)}
    with pod.trees.owned(lease.head) as tree:
        venv = venvs.venv_for(lockfile_digest(tree), tree, pod.venvs)
        _trampolined(tree)
        started = pod.clock()
        stats = {**named, "kind": STATS, "root": str(tree), "cache": str(settings.cache)}
        files = list(unit.members) if unit.kind == STATS else _test_files(tree)
        result, log = _run(pod, tree, venv, {**stats, "members": files})
        if result is not None and unit.kind == MUTANT:
            tests = covering(result["stats"], function_of(unit.members[0]))
            graded = {"members": list(unit.members), "killers": str(settings.killers), "tests": tests}
            limits = {"budget": settings.budget, "cap": settings.cap}
            result, log = _run(pod, tree, venv, {**named, **graded, **limits})
        seconds = pod.clock() - started
    passed = result is not None and all(row["status"] == KILLED for row in result.get("rows", ()))
    return Finished(PASSED if passed else FAILED, _report(pod, {**(result or {}), "seconds": seconds}), log)


def executors(pod: Pod, suite: Suite, settings: Mutation) -> dict[str, Executor]:
    """Return the adapter of every unit kind the pod runs."""
    graded = partial(mutation, pod, settings)
    return {STATIC: partial(static, pod), TEST: partial(tests, pod, suite), STATS: graded, MUTANT: graded}


def settings(environ: Mapping[str, str]) -> tuple[Pod, Suite, Mutation]:
    """Return the pod, suite and mutation settings the worker contract environment names."""
    allowed = worker.environment(environ)
    root = Path(allowed[contract.WORK_ROOT])
    node = Path(allowed[contract.GIT_MIRROR])
    mirror = Mirror(node, root / "store", str(node))
    trees = Trees(Worktrees(mirror, root / "trees", TREES))
    ports = PortAllocator(FIRST_PORT, LAST_PORT, PORT_WIDTH)
    pod = Pod(trees, root, ports, WHITELIST, UNIT_TIMEOUT_S)
    for folder in (pod.runs, pod.venvs):
        folder.mkdir(parents=True, exist_ok=True)
    suite = Suite(Path(allowed[contract.MANIFEST]), Path(allowed[contract.FLAKES]))
    budget, cap = float(allowed[contract.MUTATION_BUDGET]), int(allowed[contract.MUTATION_CAP])
    return pod, suite, Mutation(Path(allowed[contract.MUTATION_CACHE]), Path(allowed[contract.KILLERS]), budget, cap)


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Run the worker over the adapters until SIGTERM stops it, then let its running units drain."""
    argparse.ArgumentParser(prog="agentihooks ci-lab-worker").parse_args(argv)
    pod, suite, graded = settings(environ)
    cpus = os.cpu_count() or 1
    service = worker.build(
        environ, executors(pod, suite, graded), socket.gethostname(), cpus, ci_cpu_quota.quota_cpus()
    )

    def stop(signum: int, frame: FrameType | None) -> None:
        """Stop leasing once SIGTERM arrives."""
        service.stop()

    signal.signal(signal.SIGTERM, stop)
    service.run()
    return 0
