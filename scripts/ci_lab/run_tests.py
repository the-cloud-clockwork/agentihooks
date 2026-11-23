"""The lab's test batch executor: one pytest run of a batch in the sandbox, and one more on identical inputs after
a failure.

A pass after a failure is green and flaky, with a flake ledger row per test the first attempt failed; a second
failure is red and final, so the store keeps the negative verdict. A repository file the batch opened that its
manifest records leave out is listed on the verdict and makes it broad.
"""

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts import dep_manifest
from scripts.ci_lab import open_capture
from scripts.ci_lab.expand_tests import PYTEST_INVOCATION
from scripts.ci_lab.outcomes import FAILED, PASSED
from scripts.ci_lab.sandbox import PortAllocator, Sandbox

__all__ = ["COVERAGE", "Attempt", "Lab", "Verdict", "flake_rows", "outside", "run"]

COVERAGE = (
    "--cov=hooks",
    "--cov=scripts",
    "--cov-report=",
    "--cov-config=.github/coverage/coverage.ini",
    "--cov-context=test",
)
SKIPPED = (".git/", ".venv/", ".playwright/", "node_modules/", ".pytest_cache/")
SITE = (
    "import importlib.util\n"
    "_spec = importlib.util.spec_from_file_location('ci_lab_open_capture', {path!r})\n"
    "_module = importlib.util.module_from_spec(_spec)\n"
    "_spec.loader.exec_module(_module)\n"
    "_module.install()\n"
)


@dataclass(frozen=True)
class Lab:
    """Where batches run: the sandbox root, ports and environment, the dependency manifest and the flake ledger."""

    root: Path
    ports: PortAllocator
    whitelist: Mapping[str, str]
    manifest: Mapping[str, Any]
    flakes: Path
    timeout: float
    net: bool


@dataclass(frozen=True)
class Attempt:
    """One pytest run of a batch: its exit code, None when the timeout killed it, its output folder and its log."""

    code: int | None
    folder: Path
    log: Path


@dataclass(frozen=True)
class Verdict:
    """A batch's verdict: passed or failed, whether a retry passed it, the repository files it opened outside its
    manifest, the final attempt's durations, log and coverage data, how many attempts ran, and the flake ledger rows
    of a batch a retry passed."""

    outcome: str
    flaky: bool
    outside: tuple[str, ...]
    durations: Mapping[str, float]
    attempts: int
    log: Path
    coverage: Path
    flakes: tuple[Mapping[str, Any], ...] = ()

    @property
    def broad(self) -> bool:
        """Whether the batch opened a repository file its manifest records leave out."""
        return bool(self.outside)


def _nodes(batch: Sequence[str]) -> list[str]:
    """Return the pytest node ids of a batch's manifest tests, in batch order."""
    return [dep_manifest.node(test) for test in batch]


def _shared(path: str) -> bool:
    """Return whether a tree file is an input every test shares or a file the manifest never records."""
    shared = path in dep_manifest.GLOBAL_FILES or path.startswith((*dep_manifest.GLOBAL_PREFIXES, *SKIPPED))
    return shared or path.endswith(".pyc") or "__pycache__" in path


def _environment(tree: Path, venv: Path, folder: Path, whitelist: Mapping[str, str]) -> dict[str, str]:
    """Return the whitelist with venv first on the path and the attempt's output files under folder."""
    return {
        **whitelist,
        "PATH": os.pathsep.join((str(venv / "bin"), whitelist.get("PATH", os.defpath))),
        "PYTHONPATH": str(folder / "site"),
        "CI_RESULTS": str(folder / "results"),
        "CI_FLAKES": str(folder / "flakes.jsonl"),
        "COVERAGE_FILE": str(folder / ".coverage"),
        open_capture.TREE: str(tree),
        open_capture.OPENED: str(folder / "opened"),
    }


def _attempt(batch: Sequence[str], tree: Path, venv: Path, slots: int, lab: Lab) -> Attempt:
    """Run the batch's pytest once in a fresh output folder under the lab root."""
    folder = Path(tempfile.mkdtemp(dir=lab.root))
    for name in ("site", "opened"):
        (folder / name).mkdir()
    (folder / "site" / "sitecustomize.py").write_text(SITE.format(path=open_capture.__file__))
    durations = ("--store-durations", "--durations-path", str(folder / "durations.json"))
    argv = [str(venv / "bin" / "python"), "-m", *PYTEST_INVOCATION, "-n", str(slots), *COVERAGE, *durations]
    box = Sandbox(lab.root, tree, lab.ports, _environment(tree, venv, folder, lab.whitelist))
    outcome = box.run([*argv, *_nodes(batch)], lab.timeout, lab.net)
    return Attempt(code=outcome.code, folder=folder, log=outcome.log)


def _opened(attempt: Attempt) -> set[str]:
    """Return the tree files the attempt's pytest workers read, relative to the tree."""
    logs = (attempt.folder / "opened").glob("*.txt")
    return {os.fsdecode(path) for log in logs for path in log.read_bytes().splitlines()}


def _durations(attempt: Attempt) -> dict[str, float]:
    """Return the seconds per node id the attempt stored, none when it ended before storing them."""
    path = attempt.folder / "durations.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def outside(opened: set[str], batch: Sequence[str], tree: Path, manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the tree files the batch opened that neither its manifest records nor the inputs every test shares hold;
    a batch member the manifest does not list, such as a test module path, records none."""
    listed = {path for test in batch for path in manifest["tests"].get(test, {}).get("files", ())}
    found = (path for path in opened if path not in listed and not _shared(path) and (tree / path).is_file())
    return tuple(sorted(found))


def flake_rows(first: Attempt, batch: Sequence[str]) -> list[dict[str, Any]]:
    """Return the flake ledger rows of a batch whose retry passed: one per test the first attempt's ledger names, or
    one per batch test when that ledger names none."""
    ledger = first.folder / "flakes.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.is_file() else []
    if not rows:
        error = f"pytest exited with code {first.code}"
        rows = [{"test": node, "errors": [error], "tracebacks": []} for node in _nodes(batch)]
    return [{**row, "attempts": 2, "outcome": "flaky"} for row in rows]


def run(batch: Sequence[str], tree: Path, venv: Path, slots: int, lab: Lab) -> Verdict:
    """Run a batch from tree with venv's python on slots workers, at least one, and once more on identical inputs after
    a failure."""
    attempts = [_attempt(batch, tree, venv, slots, lab)]
    if attempts[0].code != 0:
        attempts.append(_attempt(batch, tree, venv, slots, lab))
    final = attempts[-1]
    flaky = final.code == 0 and len(attempts) == 2
    rows = flake_rows(attempts[0], batch) if flaky else []
    if flaky:
        with lab.flakes.open("a") as ledger:
            ledger.writelines(json.dumps(row) + "\n" for row in rows)
    opened = {path for attempt in attempts for path in _opened(attempt)}
    return Verdict(
        outcome=PASSED if final.code == 0 else FAILED,
        flaky=flaky,
        outside=outside(opened, batch, tree, lab.manifest),
        durations=_durations(final),
        attempts=len(attempts),
        log=final.log,
        coverage=final.folder / ".coverage",
        flakes=tuple(rows),
    )
