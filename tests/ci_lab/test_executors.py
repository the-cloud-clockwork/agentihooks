import dataclasses
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts import ci_wiring
from scripts.ci_lab import (
    contract,
    executors,
    expand_mutation,
    metrics,
    run_static,
    run_tests,
    static_gates,
    venvs,
    worker,
)
from scripts.ci_lab.outcomes import FAILED, PASSED
from scripts.ci_lab.sandbox import CannotRun, Outcome, PortAllocator, PortRange
from scripts.ci_lab.static_gates import StaticGate
from scripts.ci_lab.units import Unit
from scripts.ci_lab.worker import Finished, Lease
from scripts.ci_lab.worktrees import Mirror, Worktrees
from scripts.ci_mutation import scope

WORKFLOW = """
on: pull_request
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint check
        run: python -m ruff check scripts/
      - name: Format check
        run: python -m ruff format --check scripts/
"""
REGISTRY = {"lint/lint-check": {"paths": ["scripts"], "tools": ["ruff"]}}
OPTIONS = ["-q", "-p", "no:cacheprovider"]
WHITELIST = {"LANG": "C"}
LOCK = ".github/test-requirements.lock"
EXCLUDES = ".github/test-excludes.txt"
FAKE_UV = """#!{python}
import json, pathlib, sys
with open({record!r}, "a") as record:
    record.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "venv":
    pathlib.Path(sys.argv[-1]).mkdir(parents=True, exist_ok=True)
"""
GIT = ("git", "-c", "user.name=lab", "-c", "user.email=lab@example.invalid")
RUNNER = """import json
import sys
from pathlib import Path

answers = json.loads(Path(__file__).with_name("answers.json").read_text())
kind = json.loads(Path(sys.argv[1]).read_text())["kind"]
Path(sys.argv[2]).write_text(json.dumps(answers[kind]))
sys.exit(answers["code"])
"""
MUTANTS = ("scripts.alpha.x_first__mutmut_1", "scripts.alpha.x_first__mutmut_2")
REACHING = {
    "status": 0,
    "tests": {"scripts.alpha.x_first": ["tests/test_alpha.py::test_a"]},
    "durations": {"tests/test_alpha.py::test_a": 0.5},
    "cpu": 0.0,
}
TRAMPOLINED = {
    "hooks/pkg/gamma.py": ["hooks.pkg.gamma.x_third__mutmut_1"],
    "scripts/alpha.py": ["scripts.alpha.x_first__mutmut_1", "scripts.alpha.x_first__mutmut_2"],
}


CACHED = {"git": {"hits": 1, "misses": 0}, "uv": {"hits": 1, "misses": 0}}
CACHED_WITH_GRADER = {"git": {"hits": 2, "misses": 0}, "uv": {"hits": 1, "misses": 0}}
ROOT = Path(__file__).resolve().parents[2]
LEASE_CONTEXT = {
    "inputs.base": "b1",
    "github.sha": "h1",
    "github.event.pull_request.head.sha": "h1",
    "github.event.pull_request.base.sha": "b1",
    "github.event_name": "pull_request",
    "github.run_id": "job",
    "github.run_attempt": "1",
    "github.run_number": "1",
}
REFUSED: dict[str, str] = {}
SECRET_WORKFLOW = r"""
jobs:
  lint:
    steps:
      - name: Keep keywords out of the docs
        env:
          KEYWORDS: ${{ secrets.KEYWORDS }}
          BETA: ${{ secrets.BETA }}
          ABSENT: x${{ secrets.ABSENT }}x
        run: python -m scripts.docs_sanity
"""


class FakeWorktrees:
    def __init__(self, trees: dict[str, Path], broken: str = "", fetched: frozenset[str] = frozenset()) -> None:
        self.trees = trees
        self.broken = broken
        self.fetched = fetched
        self.calls: list[tuple[object, ...]] = []

    def lease(self, head: str) -> tuple[Path, bool]:
        if head == self.broken:
            raise RuntimeError(head)
        self.calls.append(("lease", head))
        return self.trees[head], head in self.fetched

    def release(self, head: str) -> None:
        self.calls.append(("release", head))

    def own(self, head: str, base: str) -> tuple[Path, bool]:
        self.calls.append(("own", head, base))
        return self.trees[head], head in self.fetched

    def drop(self, tree: Path) -> None:
        self.calls.append(("drop", tree))


class Recorder:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> object:
        self.calls.append(args)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Box:
    def __init__(self, scripted: list[tuple[int | None, dict[str, Any] | None]], logs: Path) -> None:
        self.scripted = scripted
        self.logs = logs
        self.made: list[tuple[object, ...]] = []
        self.requests: list[dict[str, Any]] = []
        self.runs: list[tuple[list[str], float]] = []

    def __call__(
        self, root: Path, cwd: Path, ports: PortAllocator, whitelist: dict[str, str], readonly: tuple[Path, ...] = ()
    ) -> "Box":
        self.made.append((root, cwd, ports, whitelist, readonly))
        return self

    def run(self, argv: list[str], timeout: float) -> Outcome:
        self.requests.append(json.loads(Path(argv[3]).read_text()))
        self.runs.append((argv, timeout))
        code, result = self.scripted.pop(0)
        if result is not None:
            Path(argv[4]).write_text(json.dumps(result))
        return Outcome(code=code, log=self.logs / f"log{len(self.runs)}.txt")


def ticking(*values: float) -> Callable[[], float]:
    readings: Iterator[float] = iter(values)
    return lambda: next(readings)


def pod_of(root: Path, trees: FakeWorktrees) -> executors.Pod:
    pod = executors.Pod(
        executors.Trees(cast("Worktrees", trees)),
        root / "pod",
        PortAllocator(1, 10, 10),
        WHITELIST,
        30.0,
        ticking(10, 12.5),
    )
    pod.runs.mkdir(parents=True)
    return pod


def lease(kind: str, key: str, members: tuple[str, ...], slots: int = 1, manifest: str = "") -> Lease:
    return Lease("job", "tok", "h1", Unit(kind, key, members, manifest), slots, 1500, "b1", "g1")


def folder(root: Path, name: str, files: dict[str, str]) -> Path:
    tree = root / name
    for path, text in files.items():
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / path).write_text(text)
    return tree


def report(finished: Finished, pod: executors.Pod) -> dict[str, Any]:
    assert finished.report.name == "report.json"
    assert finished.report.parent.parent == pod.runs
    return json.loads(finished.report.read_text())


@pytest.fixture
def venv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Recorder:
    recorder = Recorder((tmp_path / "venv", False))
    monkeypatch.setattr(executors.venvs, "venv_for", recorder)
    return recorder


@pytest.fixture
def static_trees(tmp_path: Path) -> FakeWorktrees:
    head = folder(tmp_path, "head", {LOCK: "lock-h"})
    base = folder(tmp_path, "base", {LOCK: "lock-b"})
    grader = folder(
        tmp_path,
        "grader",
        {".github/workflows/test.yml": WORKFLOW, ".github/input-keys.json": json.dumps(REGISTRY), LOCK: "lock-g"},
    )
    return FakeWorktrees({"h1": head, "b1": base, "g1": grader})


@pytest.mark.feature("lab-worker-executors")
def test_static_runs_the_gate_the_grader_tree_names_over_the_head_with_its_base_and_grader_trees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, venv: Recorder, static_trees: FakeWorktrees
) -> None:
    gates = {found.name: found for found in static_gates.discover(WORKFLOW, REGISTRY)}
    name = sorted(gates)[1]
    summary = folder(tmp_path, "run", {"summary.md": "ok\n"}) / "summary.md"
    ran = Recorder(run_static.Result(name, 0, tmp_path / "log.txt", summary))
    monkeypatch.setattr(executors.run_static, "run", ran)
    static_trees.fetched = frozenset({"b1"})
    venv.answer = (tmp_path / "venv", True)
    pod = pod_of(tmp_path, static_trees)

    finished = executors.static(pod, lease("static", "k", (name,)))

    head, base, grader = (static_trees.trees[sha] for sha in ("h1", "b1", "g1"))
    assert len(gates) == 2
    assert venv.calls == [
        (hashlib.sha256(b"lock-h").hexdigest(), head, pod.root / "venvs", venvs.Build(pod.ports, WHITELIST, 30.0))
    ]
    settings = run_static.Settings(pod.runs, tmp_path / "venv", pod.ports, WHITELIST, 30.0, LEASE_CONTEXT)
    assert ran.calls == [(gates[name], head, base, grader, settings)]
    assert set(LEASE_CONTEXT) == static_gates.LEASE_FIELDS
    assert finished == Finished(PASSED, finished.report, tmp_path / "log.txt")
    assert report(finished, pod) == {
        "gate": name,
        "code": 0,
        "skipped": False,
        "summary": "ok\n",
        "seconds": 2.5,
        "caches": {"git": {"hits": 2, "misses": 1}, "uv": {"hits": 0, "misses": 1}},
    }
    assert static_trees.calls == [
        ("lease", "h1"),
        ("lease", "b1"),
        ("lease", "g1"),
        ("release", "h1"),
        ("release", "b1"),
        ("release", "g1"),
    ]


@pytest.mark.feature("lab-venv-tracked-test-lock")
def test_a_head_without_a_uv_lock_builds_its_venv_from_the_tracked_test_lock_and_runs_its_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gates = {found.name: found for found in static_gates.discover(WORKFLOW, REGISTRY)}
    name = sorted(gates)[1]
    head = folder(tmp_path, "head", {LOCK: "lock-h", EXCLUDES: "excluded\n", "pyproject.toml": "[project]\n"})
    base = folder(tmp_path, "base", {})
    grader = folder(
        tmp_path, "grader", {".github/workflows/test.yml": WORKFLOW, ".github/input-keys.json": json.dumps(REGISTRY)}
    )
    record = tmp_path / "uv.jsonl"
    uv = folder(tmp_path, "bin", {"uv": FAKE_UV.format(python=sys.executable, record=str(record))}) / "uv"
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{uv.parent}{os.pathsep}{os.environ['PATH']}")
    summary = folder(tmp_path, "run", {"summary.md": "ok\n"}) / "summary.md"
    ran = Recorder(run_static.Result(name, 0, tmp_path / "log.txt", summary))
    monkeypatch.setattr(executors.run_static, "run", ran)
    pod = pod_of(tmp_path, FakeWorktrees({"h1": head, "b1": base, "g1": grader}))

    finished = executors.static(pod, lease("static", "k", (name,)))

    venv = pod.venvs / hashlib.sha256(b"lock-h").hexdigest()
    install = ["--constraint", str(head / LOCK), "--excludes", str(head / EXCLUDES), "-r", str(head / "pyproject.toml")]
    assert not (head / "uv.lock").exists()
    assert [json.loads(line) for line in record.read_text().splitlines()] == [
        ["venv", "--clear", "--python", sys.executable, str(venv)],
        ["pip", "install", "--python", str(venv / "bin" / "python"), *install, "--extra", "dev", "--extra", "all"],
    ]
    settings = run_static.Settings(pod.runs, venv, pod.ports, WHITELIST, 30.0, LEASE_CONTEXT)
    assert ran.calls == [(gates[name], head, base, grader, settings)]
    assert finished.outcome == PASSED


@pytest.mark.feature("lab-worker-executors")
@pytest.mark.parametrize(
    ("code", "skipped", "outcome"), [(1, False, FAILED), (None, False, FAILED), (None, True, PASSED)]
)
def test_static_answers_the_gates_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    venv: Recorder,
    static_trees: FakeWorktrees,
    code: int | None,
    skipped: bool,
    outcome: str,
) -> None:
    summary = folder(tmp_path, "run", {"summary.md": ""}) / "summary.md"
    result = run_static.Result("lint/lint-check", code, tmp_path / "log.txt", summary, skipped)
    monkeypatch.setattr(executors.run_static, "run", Recorder(result))
    pod = pod_of(tmp_path, static_trees)

    finished = executors.static(pod, lease("static", "k", ("lint/lint-check",)))

    assert finished.outcome == outcome
    assert (report(finished, pod)["code"], report(finished, pod)["skipped"]) == (code, skipped)


@pytest.mark.feature("lab-worker-executors")
def test_static_releases_every_tree_it_leased_when_a_lease_or_the_gate_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, venv: Recorder, static_trees: FakeWorktrees
) -> None:
    monkeypatch.setattr(executors.run_static, "run", Recorder(RuntimeError("gate")))
    pod = pod_of(tmp_path, static_trees)
    with pytest.raises(RuntimeError, match="^gate$"):
        executors.static(pod, lease("static", "k", ("lint/lint-check",)))
    assert [call for call in static_trees.calls if call[0] == "release"] == [
        ("release", "h1"),
        ("release", "b1"),
        ("release", "g1"),
    ]

    static_trees.calls.clear()
    static_trees.broken = "b1"
    with pytest.raises(RuntimeError, match="^b1$"):
        executors.static(pod, lease("static", "k", ("lint/lint-check",)))
    assert static_trees.calls == [("lease", "h1"), ("release", "h1")]


BASELINE_WORKFLOW = """
on: pull_request
jobs:
  semgrep:
    runs-on: ubuntu-latest
    steps:
      - name: New findings
        run: semgrep scan --error --config rules.yml --baseline-commit "$BASE" tests
      - name: Every finding
        run: semgrep scan --error --config rules.yml tests
"""
LEASED = [("lease", "h1"), ("lease", "b1"), ("lease", "g1")]
RELEASED = [("release", "h1"), ("release", "b1"), ("release", "g1")]


class PrivateWorktrees(FakeWorktrees):
    def own(self, head: str, base: str) -> tuple[Path, bool]:
        super().own(head, base)
        return self.trees[f"own {head}"], False


@pytest.fixture
def baseline_trees(tmp_path: Path) -> PrivateWorktrees:
    head = folder(tmp_path, "head", {LOCK: "lock-h"})
    private = folder(tmp_path, "private", {LOCK: "lock-h"})
    base = folder(tmp_path, "base", {LOCK: "lock-b"})
    files = {".github/workflows/test.yml": BASELINE_WORKFLOW, ".github/input-keys.json": "{}", LOCK: "lock-g"}
    grader = folder(tmp_path, "grader", files)
    return PrivateWorktrees({"h1": head, "own h1": private, "b1": base, "g1": grader})


@pytest.mark.feature("lab-static-baseline-gate-own-tree")
@pytest.mark.parametrize(("name", "tree"), [("semgrep/new-findings", "own h1"), ("semgrep/every-finding", "h1")])
def test_static_runs_only_a_gate_that_rewrites_its_tree_in_a_private_tree_of_the_head_and_drops_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    venv: Recorder,
    baseline_trees: PrivateWorktrees,
    name: str,
    tree: str,
) -> None:
    gates = {found.name: found for found in static_gates.discover(BASELINE_WORKFLOW)}
    summary = folder(tmp_path, "run", {"summary.md": "ok\n"}) / "summary.md"
    ran = Recorder(run_static.Result(name, 0, tmp_path / "log.txt", summary))
    monkeypatch.setattr(executors.run_static, "run", ran)
    pod = pod_of(tmp_path, baseline_trees)

    finished = executors.static(pod, lease("static", "k", (name,)))

    trees = baseline_trees.trees
    settings = run_static.Settings(pod.runs, tmp_path / "venv", pod.ports, WHITELIST, 30.0, LEASE_CONTEXT)
    assert ran.calls == [(gates[name], trees[tree], trees["b1"], trees["g1"], settings)]
    assert [call[1] for call in venv.calls] == [trees["h1"]]
    assert finished.outcome == PASSED
    private = [("own", "h1", "b1"), ("drop", trees["own h1"])] if tree == "own h1" else []
    assert baseline_trees.calls == [*LEASED, *private, *RELEASED]


@pytest.mark.feature("lab-static-baseline-gate-own-tree")
def test_static_drops_the_private_tree_of_a_gate_that_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, venv: Recorder, baseline_trees: PrivateWorktrees
) -> None:
    monkeypatch.setattr(executors.run_static, "run", Recorder(RuntimeError("gate")))
    pod = pod_of(tmp_path, baseline_trees)

    with pytest.raises(RuntimeError, match="^gate$"):
        executors.static(pod, lease("static", "k", ("semgrep/new-findings",)))

    private = baseline_trees.trees["own h1"]
    assert baseline_trees.calls == [*LEASED, ("own", "h1", "b1"), ("drop", private), *RELEASED]


@pytest.fixture
def bwrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "bwrap"
    script.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")


def live_pod(tmp_path: Path, trees: FakeWorktrees, secrets: dict[str, str]) -> executors.Pod:
    pod = executors.Pod(
        executors.Trees(cast("Worktrees", trees)),
        tmp_path / "pod",
        PortAllocator(41100, 41999, 10),
        WHITELIST,
        30.0,
        secrets=secrets,
    )
    pod.runs.mkdir(parents=True)
    return pod


def _graded_on_a_clean_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job: str
) -> tuple[dict[str, str], set[str]]:
    registry = json.loads((ROOT / static_gates.REGISTRY).read_text())
    workflow = (ROOT / static_gates.WORKFLOW).read_text()
    discovered = static_gates.discover(workflow, registry, ci_wiring.load(ROOT))
    gates = {gate.name: dataclasses.replace(gate, run="true") for gate in discovered if gate.job == job}

    def found(grader: Path, name: str) -> StaticGate:
        return gates[name]

    monkeypatch.setattr(executors, "gate", found)
    head = folder(tmp_path, "head", {LOCK: "lock\n"})
    subprocess.run([*GIT, "init", "-q", str(head)], check=True)
    subprocess.run([*GIT, "-C", str(head), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(head), "commit", "-q", "-m", "tree"], check=True)
    subprocess.run([*GIT, "-C", str(head), "worktree", "add", "-q", "--detach", str(tmp_path / "base")], check=True)
    trees = FakeWorktrees({"h1": head, "b1": tmp_path / "base", "g1": ROOT})
    pod = live_pod(tmp_path, trees, {})
    graded: dict[str, str] = {}
    skipped: set[str] = set()
    for name in gates:
        try:
            finished = executors.static(pod, lease("static", "k", (name,)))
        except ValueError as refused:
            graded[name] = str(refused)
            continue
        graded[name] = finished.outcome
        if report(finished, pod)["skipped"]:
            skipped.add(name)
    return graded, skipped


@pytest.mark.feature("lab-static-step-env")
def test_static_grades_every_lint_gate_of_the_real_workflow_on_a_clean_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, venv: Recorder, bwrap: None
) -> None:
    graded, _ = _graded_on_a_clean_tree(monkeypatch, tmp_path, "lint")
    assert graded == {**dict.fromkeys(graded, PASSED), **REFUSED}


@pytest.mark.feature("lab-static-test-count-units")
def test_static_grades_every_test_count_gate_of_the_real_workflow_on_a_clean_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, venv: Recorder, bwrap: None
) -> None:
    graded, skipped = _graded_on_a_clean_tree(monkeypatch, tmp_path, "test-count")
    assert graded == {"test-count/hold-the-base-revision-s-test-count": PASSED}
    assert skipped == set()


@pytest.mark.feature("lab-static-step-env")
def test_static_gives_a_step_each_secret_it_reads_by_name_and_writes_no_value_to_its_report_or_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, venv: Recorder, bwrap: None
) -> None:
    (keywords,) = static_gates.discover(SECRET_WORKFLOW)
    probe = dataclasses.replace(
        keywords,
        run=(
            'printf "%s %s" "$KEYWORDS" "$ABSENT" > "$GITHUB_WORKSPACE/seen"\n'
            'echo "keywords $KEYWORDS"\n'
            'echo "summary $KEYWORDS" >> "$GITHUB_STEP_SUMMARY"\n'
            "echo ok\n"
        ),
    )
    contexts: list[Mapping[str, str]] = []
    real = run_static.run

    def found(grader: Path, name: str) -> StaticGate:
        return probe

    def spied(
        gate: StaticGate, head: Path, base: Path, grader: Path, settings: run_static.Settings
    ) -> run_static.Result:
        contexts.append(settings.context)
        return real(gate, head, base, grader, settings)

    monkeypatch.setattr(executors, "gate", found)
    monkeypatch.setattr(executors.run_static, "run", spied)
    head = folder(tmp_path, "head", {LOCK: "lock\n"})
    trees = FakeWorktrees({"h1": head, "b1": folder(tmp_path, "base", {}), "g1": folder(tmp_path, "grader", {})})
    secrets = {"KEYWORDS": "s3cr3t-alpha,s3cr3t-beta", "BETA": "s3cr3t-beta", "UNREAD": "s3cr3t-gamma"}
    pod = live_pod(tmp_path, trees, secrets)

    finished = executors.static(pod, lease("static", "k", (probe.name,)))

    assert finished.outcome == PASSED
    assert (head / "seen").read_text() == "s3cr3t-alpha,s3cr3t-beta xx"
    read = {"secrets.ABSENT": "", "secrets.BETA": "s3cr3t-beta", "secrets.KEYWORDS": "s3cr3t-alpha,s3cr3t-beta"}
    assert contexts == [{**LEASE_CONTEXT, **read}]
    assert finished.log.read_text() == "keywords ***\nok\n"
    assert report(finished, pod)["summary"] == "summary ***\n"
    assert "s3cr3t" not in finished.report.read_text()


def test_gate_refuses_a_name_the_grader_workflow_does_not_run(static_trees: FakeWorktrees) -> None:
    with pytest.raises(KeyError):
        executors.gate(static_trees.trees["g1"], "lint/missing")


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.feature("lab-reusable-workflow-gates")
def test_gate_returns_a_gate_of_a_reusable_workflow_the_real_tests_workflow_calls() -> None:
    registry = json.loads((ROOT / static_gates.REGISTRY).read_text())
    gates = static_gates.discover((ROOT / static_gates.WORKFLOW).read_text(), registry, ci_wiring.load(ROOT))
    semgrep = next(gate for gate in gates if gate.job.startswith("semgrep/"))
    assert executors.gate(ROOT, semgrep.name) == semgrep


@pytest.mark.feature("lab-worker-executors")
def test_tests_runs_the_batch_on_the_lockfile_venv_with_the_manifest_and_flake_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, venv: Recorder
) -> None:
    tree = folder(tmp_path, "head", {LOCK: "lock-h"})
    manifest = {"tests": {"tests/a.py::t": {"files": ["scripts/a.py"]}}}
    suite = executors.Suite(
        folder(tmp_path, "in", {"manifest.json": json.dumps(manifest)}) / "manifest.json", tmp_path / "flakes.jsonl"
    )
    verdict = run_tests.Verdict(
        PASSED, True, ("x.txt",), {"tests/a.py::t": 0.5}, 2, tmp_path / "log.txt", tmp_path / "cov"
    )
    ran = Recorder(verdict)
    monkeypatch.setattr(executors.run_tests, "run", ran)
    trees = FakeWorktrees({"h1": tree})
    pod = pod_of(tmp_path, trees)

    finished = executors.tests(
        pod,
        suite,
        Lease("job", "tok", "h1", Unit("test", "k", ("tests/a.py::t",), json.dumps(manifest)), 3, 1500, "b1", "g1"),
    )

    assert venv.calls == [
        (hashlib.sha256(b"lock-h").hexdigest(), tree, pod.root / "venvs", venvs.Build(pod.ports, WHITELIST, 30.0))
    ]
    lab = run_tests.Lab(pod.runs, pod.ports, WHITELIST, manifest, tmp_path / "flakes.jsonl", 30.0, False)
    assert ran.calls == [(("tests/a.py::t",), tree, tmp_path / "venv", 3, lab)]
    assert finished == Finished(PASSED, finished.report, tmp_path / "log.txt")
    assert report(finished, pod) == {
        "flaky": True,
        "outside": ["x.txt"],
        "durations": {"tests/a.py::t": 0.5},
        "attempts": 2,
        "seconds": 2.5,
        "caches": CACHED,
        "flakes": [],
    }
    assert trees.calls == [("lease", "h1"), ("release", "h1")]


def report_of_tests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verdict: run_tests.Verdict) -> dict[str, Any]:
    tree = folder(tmp_path, "head", {LOCK: "lock-h"})
    suite = executors.Suite(
        folder(tmp_path, "in", {"manifest.json": "{}"}) / "manifest.json", tmp_path / "flakes.jsonl"
    )
    monkeypatch.setattr(executors.run_tests, "run", Recorder(verdict))
    pod = pod_of(tmp_path, FakeWorktrees({"h1": tree}))
    unit = Unit("test", "unit-key", ("tests/a.py::t", "tests/a.py::u"), "{}")
    return report(executors.tests(pod, suite, Lease("job", "tok", "h1", unit, 1, 1500, "b1", "g1")), pod)


@pytest.mark.feature("lab-report-flake-rows")
def test_tests_reports_a_flaky_batchs_rows_with_the_unit_key_as_run_and_its_attempts_as_run_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, venv: Recorder
) -> None:
    rows = (
        {
            "test": "tests/a.py::t",
            "run": None,
            "run_attempt": None,
            "attempts": 2,
            "outcome": "flaky",
            "errors": ["first attempt fails"],
            "tracebacks": ["Traceback"],
        },
        {
            "test": "tests/a.py::u",
            "errors": ["pytest exited with code 2"],
            "tracebacks": [],
            "attempts": 2,
            "outcome": "flaky",
        },
    )
    verdict = run_tests.Verdict(PASSED, True, (), {}, 2, tmp_path / "log.txt", tmp_path / "cov", rows)

    flakes = report_of_tests(monkeypatch, tmp_path, verdict)["flakes"]

    assert flakes == [
        {
            "test": "tests/a.py::t",
            "run": "unit-key",
            "run_attempt": 2,
            "attempts": 2,
            "outcome": "flaky",
            "errors": ["first attempt fails"],
            "tracebacks": ["Traceback"],
        },
        {
            "test": "tests/a.py::u",
            "errors": ["pytest exited with code 2"],
            "tracebacks": [],
            "attempts": 2,
            "outcome": "flaky",
            "run": "unit-key",
            "run_attempt": 2,
        },
    ]


@pytest.mark.feature("lab-report-flake-rows")
def test_tests_reports_no_flake_rows_for_a_red_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, venv: Recorder
) -> None:
    verdict = run_tests.Verdict(FAILED, False, (), {}, 2, tmp_path / "log.txt", tmp_path / "cov")

    assert report_of_tests(monkeypatch, tmp_path, verdict)["flakes"] == []


@pytest.fixture
def mutation_tree(tmp_path: Path) -> Path:
    tree = folder(
        tmp_path,
        "mutation",
        {
            "pyproject.toml": '[tool.mutmut]\nsource_paths = ["scripts/", "hooks/pkg"]\n',
            "hooks/pkg/gamma.py": "def third():\n    return 3\n",
            "hooks/pkgold/delta.py": "",
            LOCK: "lock-m",
            "scripts/alpha.py": "def first():\n    return 1\n\n\ndef second():\n    return 2\n",
            "scripts/pkg/__init__.py": "",
            "scripts/notes.txt": "",
            "hooks/beta.py": "",
            "hooks/test_hook.py": "",
            "tests/helper.py": "",
            "tests/test_alpha.py": "",
            "tests/unit/test_beta.py": "",
            "tests/unit/test_data.json": "",
        },
    )
    subprocess.run([*GIT, "init", "-q", "-b", "b1", str(tree)], check=True)
    subprocess.run([*GIT, "-C", str(tree), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(tree), "commit", "-q", "-m", "base"], check=True)
    subprocess.run([*GIT, "-C", str(tree), "switch", "-q", "-c", "h1"], check=True)
    folder(
        tmp_path,
        "mutation",
        {
            "hooks/pkg/gamma.py": "def third():\n    return 5\n",
            "scripts/alpha.py": "def first():\n    return 4\n\n\ndef second():\n    return 2\n",
            "scripts/notes.txt": "note\n",
            "hooks/beta.py": "LIMIT = 1\n",
            "tests/test_alpha.py": "def test_a():\n    pass\n",
        },
    )
    subprocess.run([*GIT, "-C", str(tree), "commit", "-q", "-a", "-m", "head"], check=True)
    return tree


@pytest.fixture
def trampolines(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str, set[int]]]:
    calls: list[tuple[Path, str, set[int]]] = []

    def trampoline(tree: Path, path: str, changed: set[int]) -> list[str]:
        calls.append((tree, path, set(changed)))
        return TRAMPOLINED.get(path, [])

    monkeypatch.setattr(executors.run_mutation, "trampoline", trampoline)
    return calls


def mutation_settings(tmp_path: Path) -> executors.Mutation:
    return executors.Mutation(tmp_path / "cache", tmp_path / "killers.json", 60.0, 4)


def answering(tree: Path, code: int, status: str) -> Path:
    rows = [{"name": mutant, "status": status, "seconds": 0.0} for mutant in MUTANTS]
    answers = {
        "code": code,
        "stats": {"stats": [REACHING], "runner": tree.name},
        "mutant": {"collection_seconds": 0.0, "rows": rows, "runner": tree.name},
    }
    files = {"scripts/__init__.py": "", "scripts/ci_lab/__init__.py": "", "scripts/ci_lab/run_mutation.py": RUNNER}
    return folder(tree.parent, tree.name, {**files, "scripts/ci_lab/answers.json": json.dumps(answers)})


def python_in(venv: Path) -> None:
    python = venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)


@pytest.mark.feature("lab-worker-executors")
def test_mutation_runs_a_stats_unit_with_its_tree_as_root_and_the_stats_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    venv: Recorder,
    mutation_tree: Path,
    trampolines: list[tuple[Path, str, set[int]]],
) -> None:
    stats = {"stats": [{"status": 0, "tests": {}, "durations": {}, "cpu": 1.0}]}
    box = Box([(0, stats)], tmp_path)
    monkeypatch.setattr(executors, "Sandbox", box)
    grader = tmp_path / "grader"
    trees = FakeWorktrees({"h1": mutation_tree, "g1": grader}, fetched=frozenset({"h1"}))
    pod = pod_of(tmp_path, trees)

    finished = executors.mutation(pod, mutation_settings(tmp_path), lease("stats", "k1", ("tests/test_alpha.py",)))

    assert venv.calls == [
        (
            hashlib.sha256(b"lock-m").hexdigest(),
            mutation_tree,
            pod.root / "venvs",
            venvs.Build(pod.ports, WHITELIST, 30.0),
        )
    ]
    assert trampolines == [
        (mutation_tree, "hooks/beta.py", {1}),
        (mutation_tree, "hooks/pkg/gamma.py", {2}),
        (mutation_tree, "scripts/alpha.py", {2}),
    ]
    assert box.requests == [
        {
            "kind": "stats",
            "key": "k1",
            "options": OPTIONS,
            "tree": str(mutation_tree),
            "root": str(mutation_tree),
            "cache": str(tmp_path / "cache"),
            "members": ["tests/test_alpha.py"],
        }
    ]
    [(argv, timeout)] = box.runs
    assert argv[:3] == [str(tmp_path / "venv" / "bin" / "python"), "-m", "scripts.ci_lab.run_mutation"]
    assert (Path(argv[3]).name, Path(argv[4]).name, Path(argv[3]).parent.parent) == (
        "request.json",
        "result.json",
        pod.runs,
    )
    assert timeout == 30.0
    assert box.made == [(pod.runs, grader, pod.ports, WHITELIST, (grader,))]
    assert finished == Finished(PASSED, finished.report, tmp_path / "log1.txt")
    assert report(finished, pod) == {
        **stats,
        "seconds": 2.5,
        "caches": {"git": {"hits": 1, "misses": 1}, "uv": {"hits": 1, "misses": 0}},
    }
    assert trees.calls == [("lease", "g1"), ("own", "h1", "b1"), ("drop", mutation_tree), ("release", "g1")]


@pytest.mark.feature("lab-worker-executors")
@pytest.mark.usefixtures("venv", "trampolines")
def test_mutation_grades_a_mutant_unit_with_the_tests_its_trees_stats_show_reaching_its_function(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation_tree: Path
) -> None:
    durations = {"tests/test_alpha.py::test_a": 0.5, "tests/unit/test_beta.py::test_b": 0.25}
    reached = {"scripts.alpha.x_first": ["tests/test_alpha.py::test_a"], "scripts.alpha.x_second": ["x"]}
    stats = {"stats": [{"status": 0, "tests": reached, "durations": durations, "cpu": 1.0}]}
    members = ("scripts.alpha.x_first__mutmut_1", "scripts.alpha.x_first__mutmut_2")
    graded = {
        "collection_seconds": 0.1,
        "rows": [{"name": name, "status": "killed", "seconds": 0.2} for name in members],
    }
    box = Box([(0, stats), (0, graded)], tmp_path)
    monkeypatch.setattr(executors, "Sandbox", box)
    grader = tmp_path / "grader"
    pod = pod_of(tmp_path, FakeWorktrees({"h1": mutation_tree, "g1": grader}))

    traced = ["tests/unit/test_beta.py", "tests/unit/test_new.py"]

    finished = executors.mutation(
        pod, mutation_settings(tmp_path), lease("mutant", "m1", members, manifest=json.dumps(traced))
    )

    assert box.requests == [
        {
            "kind": "stats",
            "key": "m1",
            "options": OPTIONS,
            "tree": str(mutation_tree),
            "root": str(mutation_tree),
            "cache": str(tmp_path / "cache"),
            "members": traced,
        },
        {
            "kind": "mutant",
            "key": "m1",
            "options": OPTIONS,
            "tree": str(mutation_tree),
            "members": list(members),
            "killers": str(tmp_path / "killers.json"),
            "tests": {"tests/test_alpha.py::test_a": 0.5},
            "budget": 60.0,
            "cap": 4,
        },
    ]
    assert box.made == [(pod.runs, grader, pod.ports, WHITELIST, (grader,))] * 2
    assert finished == Finished(PASSED, finished.report, tmp_path / "log2.txt")
    assert report(finished, pod) == {**graded, "seconds": 2.5, "caches": CACHED_WITH_GRADER}


@pytest.mark.feature("lab-worker-executors")
@pytest.mark.parametrize(
    ("scripted", "runs", "log"),
    [
        ([(1, None)], 1, "log1.txt"),
        ([(None, None)], 1, "log1.txt"),
        ([(0, {"stats": []}), (1, None)], 2, "log2.txt"),
        ([(0, {"stats": []}), (0, {"rows": [{"name": "m", "status": "survived", "seconds": 0.1}]})], 2, "log2.txt"),
        ([(0, {"stats": []}), (0, {"rows": [{"name": "m", "status": "no tests", "seconds": 0.0}]})], 2, "log2.txt"),
    ],
)
@pytest.mark.usefixtures("venv", "trampolines")
def test_mutation_fails_a_mutant_unit_whose_runner_fails_or_whose_mutant_lives(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation_tree: Path,
    scripted: list[tuple[int | None, dict[str, Any] | None]],
    runs: int,
    log: str,
) -> None:
    results = [result for _, result in scripted]
    box = Box(list(scripted), tmp_path)
    monkeypatch.setattr(executors, "Sandbox", box)
    trees = FakeWorktrees({"h1": mutation_tree, "g1": tmp_path / "grader"})
    pod = pod_of(tmp_path, trees)

    unit = lease("mutant", "m1", ("scripts.alpha.x_first__mutmut_1",), manifest='["tests/test_alpha.py"]')

    finished = executors.mutation(pod, mutation_settings(tmp_path), unit)

    assert finished == Finished(FAILED, finished.report, tmp_path / log)
    assert len(box.runs) == runs
    assert report(finished, pod) == {**(results[-1] or {}), "seconds": 2.5, "caches": CACHED_WITH_GRADER}
    assert trees.calls == [("lease", "g1"), ("own", "h1", "b1"), ("drop", mutation_tree), ("release", "g1")]


@pytest.mark.feature("lab-worker-executors")
@pytest.mark.usefixtures("venv", "trampolines")
def test_mutation_drops_its_private_tree_when_the_runner_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation_tree: Path
) -> None:
    monkeypatch.setattr(executors, "Sandbox", Recorder(CannotRun("no bwrap")))
    trees = FakeWorktrees({"h1": mutation_tree, "g1": tmp_path / "grader"})

    with pytest.raises(CannotRun, match="^no bwrap$"):
        executors.mutation(
            pod_of(tmp_path, trees), mutation_settings(tmp_path), lease("stats", "k", ("tests/test_alpha.py",))
        )

    assert trees.calls == [("lease", "g1"), ("own", "h1", "b1"), ("drop", mutation_tree), ("release", "g1")]


@pytest.mark.feature("lab-mutation-protected-runner")
@pytest.mark.parametrize(
    ("code", "status", "verdict"), [(0, "killed", PASSED), (0, "survived", FAILED), (3, "killed", FAILED)]
)
@pytest.mark.usefixtures("bwrap", "trampolines", "venv")
def test_mutation_answers_the_verdict_and_rows_of_the_runner_process_it_starts(
    tmp_path: Path, mutation_tree: Path, code: int, status: str, verdict: str
) -> None:
    python_in(tmp_path / "venv")
    head, grader = answering(mutation_tree, code, status), answering(tmp_path / "grader", code, status)
    pod = pod_of(tmp_path, FakeWorktrees({"h1": head, "g1": grader}))

    finished = executors.mutation(
        pod, mutation_settings(tmp_path), lease("mutant", "m1", MUTANTS, manifest='["tests/test_alpha.py"]')
    )

    rows = [{"name": mutant, "status": status, "seconds": 0.0} for mutant in MUTANTS]
    assert finished.outcome == verdict
    assert report(finished, pod).get("rows") == (None if code else rows)


@pytest.mark.feature("lab-mutation-protected-runner")
@pytest.mark.parametrize(
    ("grader", "head", "verdict"),
    [
        ((0, "survived"), (0, "killed"), FAILED),
        ((0, "killed"), (3, "survived"), PASSED),
        ((3, "killed"), (0, "killed"), FAILED),
    ],
)
@pytest.mark.usefixtures("bwrap", "trampolines", "venv")
def test_the_grader_trees_runner_decides_a_mutant_unit_whatever_the_head_runner_answers(
    tmp_path: Path, mutation_tree: Path, grader: tuple[int, str], head: tuple[int, str], verdict: str
) -> None:
    python_in(tmp_path / "venv")
    trees = FakeWorktrees({"h1": answering(mutation_tree, *head), "g1": answering(tmp_path / "grader", *grader)})
    pod = pod_of(tmp_path, trees)

    finished = executors.mutation(
        pod, mutation_settings(tmp_path), lease("mutant", "m1", MUTANTS, manifest='["tests/test_alpha.py"]')
    )

    code, status = grader
    rows = [{"name": mutant, "status": status, "seconds": 0.0} for mutant in MUTANTS]
    graded = {} if code else {"collection_seconds": 0.0, "rows": rows, "runner": "grader"}
    assert finished.outcome == verdict
    assert report(finished, pod) == {**graded, "seconds": 2.5, "caches": CACHED_WITH_GRADER}
    assert trees.calls == [("lease", "g1"), ("own", "h1", "b1"), ("drop", mutation_tree), ("release", "g1")]


@pytest.mark.feature("lab-mutation-protected-runner")
@pytest.mark.usefixtures("bwrap", "trampolines", "venv")
def test_the_grader_trees_runner_answers_a_stats_unit_whatever_the_head_runner_answers(
    tmp_path: Path, mutation_tree: Path
) -> None:
    python_in(tmp_path / "venv")
    trees = FakeWorktrees(
        {"h1": answering(mutation_tree, 3, "killed"), "g1": answering(tmp_path / "grader", 0, "killed")}
    )
    pod = pod_of(tmp_path, trees)

    finished = executors.mutation(pod, mutation_settings(tmp_path), lease("stats", "k1", ("tests/test_alpha.py",)))

    assert finished.outcome == PASSED
    assert report(finished, pod) == {
        "stats": [REACHING],
        "runner": "grader",
        "seconds": 2.5,
        "caches": CACHED_WITH_GRADER,
    }


@pytest.mark.feature("lab-mutation-units-work-on-the-change")
@pytest.mark.usefixtures("venv", "trampolines")
@pytest.mark.parametrize(
    ("members", "manifest", "refusal"),
    [
        (
            ("scripts.alpha.x_first__mutmut_1",),
            "",
            "^mutant unit m1 carries no manifest of the test files to trace$",
        ),
        (
            ("scripts.alpha.x_third__mutmut_1", "scripts.alpha.x_first__mutmut_2", "scripts.alpha.x_second__mutmut_1"),
            "[]",
            "^mutant unit m1 names mutants its tree holds behind no trampoline: "
            r"\['scripts.alpha.x_second__mutmut_1', 'scripts.alpha.x_third__mutmut_1'\]$",
        ),
    ],
)
def test_mutation_refuses_a_mutant_unit_with_no_manifest_or_a_member_its_tree_holds_behind_no_trampoline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation_tree: Path,
    members: tuple[str, ...],
    manifest: str,
    refusal: str,
) -> None:
    box = Box([], tmp_path)
    monkeypatch.setattr(executors, "Sandbox", box)
    trees = FakeWorktrees({"h1": mutation_tree, "g1": tmp_path / "grader"})

    with pytest.raises(ValueError, match=refusal):
        executors.mutation(
            pod_of(tmp_path, trees), mutation_settings(tmp_path), lease("mutant", "m1", members, manifest=manifest)
        )

    assert box.runs == []
    assert trees.calls == [("lease", "g1"), ("own", "h1", "b1"), ("drop", mutation_tree), ("release", "g1")]


@pytest.mark.feature("lab-mutation-units-work-on-the-change")
@pytest.mark.parametrize("timeout", [math.inf, expand_mutation.MUTANT_SECONDS])
def test_the_worker_trampolines_the_mutants_the_scheduler_names_for_a_change(tmp_path: Path, timeout: float) -> None:
    kept = "def keep(amount):\n    return amount + 5\n"
    sample = (
        "import functools\n\n\n"
        "def first(amount):\n    return amount + 1\n\n\n"
        "def second(amount):\n    return amount - 2\n\n\n"
        "@functools.cache\ndef third(amount):\n    return amount + 3\n\n\n"
        "if True:\n\n    def guarded(amount):\n        return amount + 4\n\n\n"
        "class Meter:\n    def read(self, amount):\n        return amount * 3\n\n"
        "    class Inner:\n        def deep(self, amount):\n            return amount + 6\n"
    )
    files = {
        "pyproject.toml": (
            '[project]\nname = "change"\n\n[project.optional-dependencies]\ndev = ["mutmut==3.6.0"]\n\n'
            '[tool.mutmut]\nsource_paths = ["scripts/", "hooks/"]\n'
        ),
        "scripts/sample.py": sample,
        "scripts/plain.py": "LIMIT = 1\n",
        "hooks/other.py": kept,
    }
    tree = folder(tmp_path, "change", files)
    subprocess.run([*GIT, "init", "-q", "-b", "b1", str(tree)], check=True)
    subprocess.run([*GIT, "-C", str(tree), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(tree), "commit", "-q", "-m", "base"], check=True)
    subprocess.run([*GIT, "-C", str(tree), "switch", "-q", "-c", "h1"], check=True)
    edited = sample
    for old, new in (("+ 1", "+ 7"), ("+ 3", "+ 8"), ("+ 4", "+ 9"), ("* 3", "* 9"), ("+ 6", "+ 5")):
        edited = edited.replace(f"amount {old}", f"amount {new}")
    folder(tmp_path, "change", {"scripts/sample.py": edited, "scripts/plain.py": "LIMIT = 2\n"})
    subprocess.run([*GIT, "-C", str(tree), "commit", "-q", "-a", "-m", "head"], check=True)
    units = expand_mutation.mutant_units(tree, scope.changes_since(tree, "b1", "h1"), {}, {}, timeout=timeout)

    names = executors._trampolined(tree, "b1", "h1")  # pyright: ignore[reportPrivateUsage]

    assert names == [member for unit in units for member in unit.members]
    assert (len(units) == len(names)) is (timeout != math.inf)
    assert len(names) > len({executors.function_of(name) for name in names}) > 1
    assert any("first" in name for name in names) and any("Meter" in name and "read" in name for name in names)
    assert not any(function in name for name in names for function in ("second", "guarded", "deep"))
    written = (tree / "scripts/sample.py").read_text()
    assert "x_first__mutmut_1" in written
    assert "x_second" not in written
    assert (tree / "scripts/plain.py").read_text() == "LIMIT = 2\n"
    assert (tree / "hooks/other.py").read_text() == kept


@pytest.mark.feature("lab-worker-executors")
def test_covering_takes_each_test_a_stats_record_shows_reaching_the_function_with_its_seconds() -> None:
    records = [
        {"tests": {"f": ["a", "b"], "g": ["c"]}, "durations": {"a": 1.0, "b": 2.0, "c": 3.0}},
        {"tests": {"f": ["d"]}, "durations": {"d": 4.0}},
        {"tests": {}, "durations": {}},
    ]

    assert executors.covering(records, "f") == {"a": 1.0, "b": 2.0, "d": 4.0}
    assert executors.covering(records, "h") == {}


@pytest.mark.feature("lab-worker-executors")
def test_function_of_names_the_function_a_mutant_mutates() -> None:
    assert executors.function_of("scripts.alpha.xǁQueueǁenqueue__mutmut_12") == "scripts.alpha.xǁQueueǁenqueue"
    assert executors.function_of("scripts.alpha.x_a__mutmut_b__mutmut_3") == "scripts.alpha.x_a__mutmut_b"


@pytest.mark.feature("lab-worker-executors")
def test_executors_name_the_adapter_of_every_unit_kind(tmp_path: Path) -> None:
    pod = pod_of(tmp_path, FakeWorktrees({}))
    suite = executors.Suite(tmp_path / "manifest.json", tmp_path / "flakes.jsonl")
    graded = mutation_settings(tmp_path)

    found = executors.executors(pod, suite, graded)

    assert {kind: parts(adapter) for kind, adapter in found.items()} == {
        "static": (executors.static, (pod,)),
        "test": (executors.tests, (pod, suite)),
        "stats": (executors.mutation, (pod, graded)),
        "mutant": (executors.mutation, (pod, graded)),
    }


def parts(adapter: object) -> tuple[object, tuple[object, ...]]:
    assert isinstance(adapter, partial)
    return adapter.func, adapter.args


def environ_of(tmp_path: Path) -> dict[str, str]:
    return {
        contract.SCHEDULER_URL: "http://scheduler:8800",
        contract.GIT_MIRROR: str(tmp_path / "node"),
        contract.WORK_ROOT: str(tmp_path / "work"),
        contract.MANIFEST: str(tmp_path / "manifest.json"),
        contract.FLAKES: str(tmp_path / "flakes.jsonl"),
        contract.MUTATION_CACHE: str(tmp_path / "cache"),
        contract.KILLERS: str(tmp_path / "killers.json"),
        contract.MUTATION_BUDGET: "90.5",
        contract.MUTATION_CAP: "3",
    }


@pytest.mark.feature("lab-worker-executors")
def test_settings_read_the_worker_contract_environment(tmp_path: Path) -> None:
    pod, suite, graded = executors.settings({**environ_of(tmp_path), "OTHER": "x"})

    work = tmp_path / "work"
    assert pod.trees.worktrees.mirror == Mirror(tmp_path / "node", work / "store", str(tmp_path / "node"), None, "")
    assert (pod.trees.worktrees.root, pod.trees.worktrees.capacity) == (work / "trees", 8)
    assert (pod.root, pod.whitelist, pod.timeout, pod.clock) == (work, {"LANG": "C.UTF-8"}, 1800.0, time.monotonic)
    assert (work / "runs").is_dir() and (work / "venvs").is_dir()
    assert executors.settings(environ_of(tmp_path))[0].root == work
    assert suite == executors.Suite(tmp_path / "manifest.json", tmp_path / "flakes.jsonl")
    assert graded == executors.Mutation(tmp_path / "cache", tmp_path / "killers.json", 90.5, 3)
    ranges: list[PortRange] = []
    with pytest.raises(CannotRun):
        while True:
            ranges.append(pod.ports.take())
    assert (len(ranges), ranges[0], ranges[-1]) == (1000, PortRange(20000, 20009), PortRange(29990, 29999))


@pytest.mark.feature("lab-static-step-env")
def test_settings_keep_each_step_secret_by_its_name_under_the_scoped_prefix(tmp_path: Path) -> None:
    environ = {
        **environ_of(tmp_path),
        "CI_LAB_SECRET_DOCS_SANITY_KEYWORDS": "s3cr3t",
        "DOCS_SANITY_KEYWORDS": "bare",
        "CI_LAB_SECRETS": "other",
    }
    assert executors.settings(environ)[0].secrets == {"DOCS_SANITY_KEYWORDS": "s3cr3t"}
    assert executors.settings(environ_of(tmp_path))[0].secrets == {}


def test_settings_refuse_a_redis_variable(tmp_path: Path) -> None:
    with pytest.raises(worker.Refused):
        executors.settings({**environ_of(tmp_path), "CI_LAB_REDIS_URL": "redis://x"})


def graded_on_the_lab_only(tmp_path: Path, node: Path) -> tuple[str, Path]:
    source, lab = tmp_path / "source", tmp_path / "lab" / "agentihooks.git"
    subprocess.run([*GIT, "init", "-q", "-b", "main", str(source)], check=True)
    (source / "a").write_text("a")
    subprocess.run([*GIT, "-C", str(source), "add", "a"], check=True)
    subprocess.run([*GIT, "-C", str(source), "commit", "-q", "-m", "a"], check=True)
    head = subprocess.run([*GIT, "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    for bare in (node, lab):
        subprocess.run([*GIT, "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run([*GIT, "-C", str(source), "push", "-q", f"file://{lab}", "HEAD:refs/grade/t1"], check=True)
    return head.stdout.strip(), lab


def lab_environ(tmp_path: Path, node: Path, lab: Path, repository: str | None) -> dict[str, str]:
    environ = {
        **environ_of(tmp_path),
        contract.GIT_MIRROR: str(node),
        "AGENTIHOOKS_LAB_REMOTE": f"file://{lab}",
        "AGENTIHOOKS_LAB_REMOTE_TOKEN": "lab-token",
    }
    return environ if repository is None else {**environ, "CI_LAB_REPOSITORY": repository}


@pytest.mark.feature("lab-remote-heads")
def test_a_worker_cuts_the_tree_of_a_head_only_the_lab_remote_holds(tmp_path: Path) -> None:
    node = tmp_path / "node"
    head, lab = graded_on_the_lab_only(tmp_path, node)
    environ = lab_environ(tmp_path, node, lab, "https://github.com/the-cloud-clockwork/agentihooks.git")

    tree, fetched = executors.settings(environ)[0].trees.worktrees.lease(head)

    assert fetched is True
    assert (tree / "a").read_text() == "a"


@pytest.mark.feature("lab-remote-heads")
@pytest.mark.parametrize(
    ("repository", "node"),
    [("https://github.com/the-cloud-clockwork/widgets.git", "node"), (None, "agentihooks.git")],
    ids=["another-repository", "no-repository-on-a-node-named-like-the-lab"],
)
def test_a_worker_never_asks_a_lab_remote_its_repository_does_not_name(
    tmp_path: Path, repository: str | None, node: str
) -> None:
    head, lab = graded_on_the_lab_only(tmp_path, tmp_path / node)

    with pytest.raises(subprocess.CalledProcessError):
        executors.settings(lab_environ(tmp_path, tmp_path / node, lab, repository))[0].trees.worktrees.lease(head)


@pytest.mark.feature("lab-worker-executors")
@pytest.mark.parametrize(("cpus", "slots"), [(4, 4), (None, 1)])
def test_main_runs_the_worker_over_the_adapters_until_sigterm_stops_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path, cpus: int | None, slots: int
) -> None:
    class Service:
        def __init__(self) -> None:
            self.events: list[str] = []

        def run(self) -> list[Lease]:
            self.events.append("run")
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return []

        def stop(self) -> None:
            self.events.append("stop")

    service = Service()
    built: list[tuple[object, ...]] = []
    handlers: dict[int, Callable[[int, object], None]] = {}

    def build(*args: object) -> Service:
        built.append(args)
        return service

    monkeypatch.setattr(executors.worker, "build", build)
    monkeypatch.setattr(executors.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))
    monkeypatch.setattr(executors.socket, "gethostname", lambda: "pod-1")
    monkeypatch.setattr(executors.os, "cpu_count", lambda: cpus)
    monkeypatch.setattr(executors.ci_cpu_quota, "quota_cpus", lambda: 2.0)
    monkeypatch.setattr(executors.contract, "METRICS_PORT", 0)
    environ = environ_of(tmp_path)

    assert executors.main([], environ) == 0

    [(given, adapters, name, count, quota)] = built
    assert (given, sorted(cast("dict[str, object]", adapters)), name, count, quota) == (
        environ,
        ["mutant", "static", "stats", "test"],
        "pod-1",
        slots,
        2.0,
    )
    found = {kind: parts(adapter) for kind, adapter in cast("dict[str, object]", adapters).items()}
    static_pod = cast("executors.Pod", found["static"][1][0])
    suite = executors.Suite(tmp_path / "manifest.json", tmp_path / "flakes.jsonl")
    graded = executors.Mutation(tmp_path / "cache", tmp_path / "killers.json", 90.5, 3)
    assert static_pod.root == tmp_path / "work"
    assert found["test"] == (executors.tests, (static_pod, suite))
    assert found["mutant"] == (executors.mutation, (static_pod, graded))
    assert service.events == ["run", "stop"]
    with pytest.raises(SystemExit):
        executors.main(["--unknown"], environ)
    assert capsys.readouterr().err == (
        "usage: agentihooks ci-lab-worker [-h]\nagentihooks ci-lab-worker: error: unrecognized arguments: --unknown\n"
    )


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_main_empties_the_work_root_before_it_builds_its_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Service:
        def run(self) -> list[Lease]:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return []

        def stop(self) -> None:
            pass

    handlers: dict[int, Callable[[int, object], None]] = {}
    monkeypatch.setattr(executors.worker, "build", lambda *args: Service())
    monkeypatch.setattr(executors.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))
    monkeypatch.setattr(executors.contract, "METRICS_PORT", 0)
    environ = environ_of(tmp_path)
    stale = tmp_path / "work" / "trees" / "old-head" / "left"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale")

    assert executors.main([], environ) == 0

    assert not stale.exists()
    assert (tmp_path / "work" / "runs").is_dir()
    assert (tmp_path / "work" / "venvs").is_dir()


@pytest.mark.feature("lab-worker-metrics")
def test_main_serves_the_worker_metrics_on_the_contract_port_until_the_worker_drains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served: list[tuple[str, int, ThreadingHTTPServer]] = []
    scraped: list[tuple[int, str, str]] = []
    stopped: list[bool] = []
    handlers: dict[int, Callable[[int, object], None]] = {}
    bind = metrics.serve_registry

    def serve_registry(host: str, port: int, registry: metrics.Registry) -> ThreadingHTTPServer:
        server = bind(host, 0, registry)
        served.append((host, port, server))
        return server

    def serving(target: Callable[[], None]) -> threading.Thread:
        if target != served[0][2].serve_forever:
            pytest.fail(f"main ran {target!r} on its thread instead of the metrics server")
        return threading.Thread(target=target)

    class Service:
        def run(self) -> list[Lease]:
            address = f"http://127.0.0.1:{served[0][2].server_address[1]}/metrics"
            with urllib.request.urlopen(address, timeout=5) as reply:
                scraped.append((reply.status, reply.headers["Content-Type"], reply.read().decode()))
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return []

        def stop(self) -> None:
            stopped.append(True)

    before = set(threading.enumerate())
    monkeypatch.setattr(metrics, "serve_registry", serve_registry)
    monkeypatch.setattr(executors, "threading", SimpleNamespace(Thread=serving))
    monkeypatch.setattr(executors.worker, "build", lambda *args: Service())
    monkeypatch.setattr(executors.signal, "signal", lambda number, handler: handlers.__setitem__(number, handler))

    assert executors.main([], environ_of(tmp_path)) == 0

    [(host, port, server)] = served
    assert (host, port, stopped) == ("", contract.METRICS_PORT, [True])
    assert scraped == [(200, metrics.EXPOSITION, metrics.Registry().render())]
    assert server.socket.fileno() == -1
    for thread in set(threading.enumerate()) - before:
        thread.join(timeout=5)
        assert not thread.is_alive()
