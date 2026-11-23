"""Lab mutation runner: content keyed stats per test file batch and warm forked mutants per function unit.

A stats unit returns, through the stats cache, which collected tests reach each trampolined function. A mutant unit
collects its covering tests once in this process, then forks one child per mutant that selects it through mutmut's
trampolines with MUTANT_UNDER_TEST, runs the remembered killer first and stops at the first failing test. Each row
carries the mutant's wall seconds, and a function's mutants run side by side only when in turn they would overrun
the unit budget. Run it as `python -m scripts.ci_lab.run_mutation REQUEST OUTPUT` in the tree that holds the
trampolined sources.
"""

import json
import math
import sys
from collections.abc import Mapping, MutableMapping, Sequence, Set
from dataclasses import asdict, dataclass
from functools import partial
from gc import freeze
from os import _exit, environ, fork, wait, waitstatus_to_exitcode
from pathlib import Path
from time import monotonic, process_time
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from mutmut.utils.format_utils import get_mutant_name

from scripts.ci_lab.units import Unit
from scripts.ci_mutation.killers import KillMemory
from scripts.ci_mutation.selection import LIGHT_MAIN, plain_copies
from scripts.ci_mutation.stats_cache import collect_cached_stats  # pyright: ignore[reportUnknownVariableType]

__all__ = ["Recorder", "Row", "WarmLoop", "install", "main", "mutants", "run", "stats", "trampoline"]

SWITCH = "MUTANT_UNDER_TEST"
STATS = "stats"
CLEAN = ""
MAIN = "mutmut.__main__"
TRAMPOLINE = "mutmut.mutation.trampoline"
HIT = "record_trampoline_hit"
NO_TESTS = "no tests"
SUSPICIOUS = "suspicious"
STATUSES = {0: "survived", 1: "killed"}
CRASHED = 3
HITS: set[str] = set()


@dataclass(frozen=True)
class Row:
    """One graded mutant: its mutmut name, its status and the wall seconds its child took."""

    name: str
    status: str
    seconds: float


def install(modules: MutableMapping[str, ModuleType]) -> ModuleType:
    """Serve the trampolines a light mutmut main whose stats hits land in HITS, also where they were bound before."""
    light = ModuleType(MAIN)
    exec(LIGHT_MAIN, vars(light))
    vars(light)[HIT] = HITS.add
    modules[MAIN] = light
    if TRAMPOLINE in modules:
        vars(modules[TRAMPOLINE])[HIT] = HITS.add
    return light


def trampoline(tree: Path, path: str, changed: Set[int]) -> list[str]:
    """Rewrite path under tree with every mutant of each function whose lines meet changed behind trampolines, leave
    its other functions as they stand and return the full names of the mutants written, applying no ignore pattern
    and reading no mutmut config."""
    from mutmut.mutation.file_mutation import combine_mutations_to_source

    from scripts.ci_lab.expand_mutation import changed_functions

    target = tree / path
    wrapper, functions = changed_functions(path, target.read_text(), changed)
    if not functions:
        return []
    selected = [mutation for mutations in functions.values() for mutation in mutations]
    code, names = combine_mutations_to_source(wrapper.module, selected)
    if any(function.decorators for function in functions):
        code = plain_copies(code)
    target.write_text(code)
    return [get_mutant_name(Path(path), name) for name in names]


def status_of(code: int) -> str:
    """Name the mutmut status of a child's exit code; anything but a pass or a kill is suspicious."""
    return STATUSES.get(code, SUSPICIOUS)


def lanes(count: int, seconds: float, budget: float, cap: int) -> int:
    """Return how many children grade count mutants of seconds each: one unless in turn they overrun budget."""
    return max(1, min(cap, math.ceil(count * seconds / budget)))


def run_items(session: pytest.Session, items: Sequence[pytest.Item]) -> int:
    """Run items in order through pytest's protocol, stop at the first failure and return 1 for a kill, else 0."""
    for index, item in enumerate(items):
        following = items[index + 1] if index + 1 < len(items) else None
        item.config.hook.pytest_runtest_protocol(item=item, nextitem=following)
        if session.testsfailed:
            return 1
    return 0


class Recorder:
    """Credits each trampolined function a test reaches to that test and sums the test's seconds."""

    def __init__(self) -> None:
        self.tests: dict[str, set[str]] = {}
        self.durations: dict[str, float] = {}

    def plugin(self) -> SimpleNamespace:
        """Return the pytest plugin that starts each test afresh and records each of its reports."""
        return SimpleNamespace(pytest_runtest_logstart=self.start, pytest_runtest_logreport=self.record)

    def start(self) -> None:
        """Forget the hits of the test before."""
        HITS.clear()

    def record(self, report: pytest.TestReport) -> None:
        """Credit every function hit so far to the reported test and add the phase's seconds to it."""
        self.durations[report.nodeid] = self.durations.get(report.nodeid, 0.0) + report.duration
        for function in list(HITS):
            self.tests.setdefault(function, set()).add(report.nodeid)


class WarmLoop:
    """Replaces pytest's test loop: it forks one child per mutant over the tests collected once."""

    def __init__(self, members: Sequence[str], tests: Sequence[str], memory: KillMemory, lanes: int) -> None:
        self.members = members
        self.tests = tests
        self.memory = memory
        self.lanes = lanes
        self.started = monotonic()
        self.collection = 0.0
        self.rows: list[Row] = []

    def plugin(self) -> SimpleNamespace:
        """Return the pytest plugin whose test loop is run."""
        return SimpleNamespace(pytest_runtestloop=self.run)

    def run(self, session: pytest.Session) -> bool | None:
        """Grade every member, at most lanes children at a time; a failed collection is left to pytest's loop."""
        self.collection = monotonic() - self.started
        if session.testsfailed:
            return None
        items = {item.nodeid: item for item in session.items}
        freeze()
        pending = list(self.members)
        running: dict[int, tuple[str, float]] = {}
        while pending or running:
            while pending and len(running) < self.lanes:
                name = pending.pop(0)
                running[self.fork(session, items, name)] = (name, monotonic())
            self.reap(running)
        return True

    def reap(self, running: dict[int, tuple[str, float]]) -> None:
        """Wait for a child and record the row of the running mutant it graded."""
        pid, status = wait()
        if pid in running:
            name, started = running.pop(pid)
            self.rows.append(Row(name, status_of(waitstatus_to_exitcode(status)), monotonic() - started))

    def fork(self, session: pytest.Session, items: Mapping[str, pytest.Item], name: str) -> int:
        """Start the child that grades name and return its pid; the child never returns into the session."""
        pid = fork()
        if pid:
            return pid
        code = CRASHED
        try:
            code = self.child(session, items, name)
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            _exit(code)

    def child(self, session: pytest.Session, items: Mapping[str, pytest.Item], name: str) -> int:
        """Select name through the trampolines and run its covering tests, the remembered killer first."""
        environ[SWITCH] = name
        order = self.memory.prepare(name, self.tests)
        return run_items(session, [items[test] for test in order if test in items])


def collect(options: Sequence[str], chosen: list[str]) -> list[dict[str, Any]]:
    """Run chosen once with stats tracing on and return the one stats result the cache splits per test file."""
    recorder = Recorder()
    environ[SWITCH] = STATS
    started = process_time()
    status = pytest.main([*options, *chosen], plugins=[recorder.plugin()])
    tests = {function: sorted(names) for function, names in recorder.tests.items()}
    cpu = process_time() - started
    return [{"status": int(status), "tests": tests, "durations": recorder.durations, "cpu": cpu}]


def stats(root: Path, cache: Path, file_batch: Sequence[str], options: Sequence[str]) -> list[dict[str, Any]]:
    """Return the stats records of file_batch from the cache keyed by root's content, collecting misses here."""
    collected = partial(collect, options)
    records: list[dict[str, Any]] = collect_cached_stats(root, cache, {}, list(file_batch), list(options), collected)  # pyright: ignore[reportUnknownVariableType]
    return records


def mutants(
    unit: Unit, tests: Mapping[str, float], memory: KillMemory, options: Sequence[str], budget: float, cap: int
) -> tuple[list[Row], float]:
    """Grade every mutant of unit here and return its rows with the seconds the one collection of tests took."""
    if not tests:
        return [Row(name, NO_TESTS, 0.0) for name in unit.members], 0.0
    ordered = sorted(tests, key=tests.__getitem__)
    loop = WarmLoop(unit.members, ordered, memory, lanes(len(unit.members), sum(tests.values()), budget, cap))
    environ[SWITCH] = CLEAN
    status = pytest.main([*options, *ordered], plugins=[loop.plugin(), memory])
    if status:
        raise SystemExit(int(status))
    return loop.rows, loop.collection


def run(request: Mapping[str, Any]) -> dict[str, Any]:
    """Run the unit a request names in the working directory and return its JSON result."""
    unit = Unit(request["kind"], request["key"], tuple(request["members"]))
    if unit.kind == STATS:
        return {"stats": stats(Path(request["root"]), Path(request["cache"]), unit.members, request["options"])}
    memory = KillMemory(Path(request["killers"]))
    rows, collection = mutants(unit, request["tests"], memory, request["options"], request["budget"], request["cap"])
    return {"collection_seconds": collection, "rows": [asdict(row) for row in rows]}


def main(argv: Sequence[str]) -> int:
    """Read the request file, run its unit and write the result file."""
    install(sys.modules)
    request, output = argv
    Path(output).write_text(json.dumps(run(json.loads(Path(request).read_text()))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
