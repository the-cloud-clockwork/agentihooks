import json
import shutil
import statistics
import subprocess
import sys
import time
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from mutmut import configuration
from mutmut.mutation.pragma_handling import PragmaParseError

from scripts.ci_lab import run_mutation
from scripts.ci_lab.run_mutation import Recorder, Row, WarmLoop
from scripts.ci_lab.units import Unit
from scripts.ci_mutation.killers import KillMemory
from scripts.ci_mutation.runner import run_gate
from scripts.ci_mutation.selection import selected_mutant_names, selected_mutants
from tests.mutation_self_plant import plant
from tests.test_ci_mutation_stats import place_flake_ledger

REPO = Path(__file__).resolve().parents[2]
OPTIONS = ["-q", "-p", "no:cacheprovider", "-o", "addopts="]
FUNCTION = "scripts.mutation_self_plant_sample.x_fee"
SAMPLE = "def fee(amount, rate):\n    return amount * rate + 1\n"
FIXED_COST_RATIO = 0.25


class Exited(Exception):
    """Stands in for a child's process exit so the forked path runs inside the test."""


def exit_with(code: int) -> None:
    raise Exited(code)


def clock(*readings: float) -> Callable[[], float]:
    values = iter(readings)
    return lambda: next(values)


def fake_items(session: SimpleNamespace, failures: list[int], calls: list[tuple[str, str | None]]) -> list[Any]:
    def protocol(*, item: SimpleNamespace, nextitem: SimpleNamespace | None) -> None:
        calls.append((item.nodeid, nextitem.nodeid if nextitem else None))
        session.testsfailed += item.fails

    hook = SimpleNamespace(pytest_runtest_protocol=protocol)
    return [
        SimpleNamespace(nodeid=f"t{index}", fails=fails, config=SimpleNamespace(hook=hook))
        for index, fails in enumerate(failures)
    ]


def as_session(session: SimpleNamespace) -> pytest.Session:
    return cast(pytest.Session, session)


def report(nodeid: str, duration: float = 0.0, failed: bool = False) -> pytest.TestReport:
    return cast(pytest.TestReport, SimpleNamespace(nodeid=nodeid, duration=duration, failed=failed))


@pytest.mark.parametrize("bound", [True, False])
def test_install_serves_the_trampolines_a_light_main_that_records_stats_hits(bound: bool) -> None:
    trampoline = ModuleType(run_mutation.TRAMPOLINE)
    modules = {run_mutation.TRAMPOLINE: trampoline} if bound else {}
    run_mutation.HITS.clear()
    light = run_mutation.install(modules)
    assert modules[run_mutation.MAIN] is light
    assert light.__name__ == "mutmut.__main__"
    assert light.mangled_name_from_mutant_name("x_fee__mutmut_orig") == "x_fee"
    assert issubclass(light.MutmutProgrammaticFailException, Exception)
    light.record_trampoline_hit("scripts.sample.x_fee")
    if bound:
        trampoline.record_trampoline_hit("scripts.sample.x_rate")
    assert run_mutation.HITS == (
        {"scripts.sample.x_fee", "scripts.sample.x_rate"} if bound else {"scripts.sample.x_fee"}
    )
    assert set(modules) == ({run_mutation.MAIN, run_mutation.TRAMPOLINE} if bound else {run_mutation.MAIN})
    run_mutation.HITS.clear()


def test_trampoline_writes_every_mutant_of_the_file_and_names_them_as_mutmut_does(tmp_path: Path) -> None:
    path = "scripts/sample.py"
    (tmp_path / "scripts").mkdir()
    (tmp_path / path).write_text(SAMPLE)
    names = run_mutation.trampoline(tmp_path, path)
    assert names == [f"scripts.sample.{name}" for name in selected_mutant_names(path, SAMPLE, {1, 2})]
    assert len(names) > 1
    assert (tmp_path / path).read_text() == selected_mutants(path, SAMPLE, {1, 2})[0]


def test_trampoline_names_the_file_mutmut_refuses_a_pragma_in(tmp_path: Path) -> None:
    path = "scripts/sample.py"
    (tmp_path / "scripts").mkdir()
    (tmp_path / path).write_text(
        "def fee(amount, rate):\n    # pragma: no mutate start\n    return amount * rate + 1\n"
    )
    with pytest.raises(PragmaParseError, match="scripts/sample.py:2"):
        run_mutation.trampoline(tmp_path, path)


@pytest.mark.feature("lab-mutants-read-no-mutmut-config")
def test_trampoline_reads_no_mutmut_config_from_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = "scripts/sample.py"
    tree, elsewhere = tmp_path / "tree", tmp_path / "elsewhere"
    (tree / "scripts").mkdir(parents=True)
    elsewhere.mkdir()
    (tree / path).write_text(SAMPLE)
    expected = [f"scripts.sample.{name}" for name in selected_mutant_names(path, SAMPLE, {1, 2})]
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(configuration, "_config", None)
    assert run_mutation.trampoline(tree, path) == expected
    assert len(expected) > 1
    assert vars(configuration)["_config"] is None


@pytest.mark.parametrize("code, status", [(0, "survived"), (1, "killed"), (3, "suspicious"), (-9, "suspicious")])
def test_a_child_exit_code_names_its_mutmut_status(code: int, status: str) -> None:
    assert run_mutation.status_of(code) == status


@pytest.mark.parametrize(
    "count, seconds, budget, cap, expected",
    [
        (10, 1.0, 60.0, 4, 1),
        (10, 12.0, 60.0, 4, 2),
        (10, 13.0, 60.0, 4, 3),
        (10, 100.0, 60.0, 4, 4),
        (0, 1.0, 60.0, 4, 1),
    ],
)
def test_mutants_run_side_by_side_only_when_in_turn_they_overrun_the_budget(
    count: int, seconds: float, budget: float, cap: int, expected: int
) -> None:
    assert run_mutation.lanes(count, seconds, budget, cap) == expected


@pytest.mark.parametrize(
    "failures, code, expected",
    [
        ([0, 1, 0], 1, [("t0", "t1"), ("t1", "t2")]),
        ([0, 0], 0, [("t0", "t1"), ("t1", None)]),
        ([1], 1, [("t0", None)]),
        ([], 0, []),
    ],
)
def test_items_run_in_order_and_stop_at_the_first_failure(
    failures: list[int], code: int, expected: list[tuple[str, str | None]]
) -> None:
    session = SimpleNamespace(testsfailed=0)
    calls: list[tuple[str, str | None]] = []
    assert run_mutation.run_items(as_session(session), fake_items(session, failures, calls)) == code
    assert calls == expected


def test_the_recorder_plugin_starts_each_test_afresh_and_records_its_reports() -> None:
    recorder = Recorder()
    plugin = recorder.plugin()
    assert (plugin.pytest_runtest_logstart, plugin.pytest_runtest_logreport) == (recorder.start, recorder.record)


def test_the_recorder_credits_each_hit_function_to_its_test_and_sums_its_phase_seconds() -> None:
    recorder = Recorder()
    run_mutation.HITS.add("scripts.sample.x_stale")
    recorder.start()
    assert run_mutation.HITS == set()
    run_mutation.HITS.add("scripts.sample.x_fee")
    recorder.record(report("tests/test_a.py::test_one", 0.25))
    recorder.record(report("tests/test_a.py::test_one", 0.5))
    recorder.start()
    run_mutation.HITS.add("scripts.sample.x_rate")
    recorder.record(report("tests/test_a.py::test_two", 1.0))
    run_mutation.HITS.clear()
    assert recorder.tests == {
        "scripts.sample.x_fee": {"tests/test_a.py::test_one"},
        "scripts.sample.x_rate": {"tests/test_a.py::test_two"},
    }
    assert recorder.durations == {"tests/test_a.py::test_one": 0.75, "tests/test_a.py::test_two": 1.0}


@pytest.mark.parametrize("failures, code", [([0, 1], 1), ([0, 0], 0)])
def test_a_child_selects_its_mutant_and_runs_the_remembered_killer_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: list[int], code: int
) -> None:
    monkeypatch.setenv(run_mutation.SWITCH, run_mutation.environ.get(run_mutation.SWITCH, ""))
    memory = KillMemory(tmp_path / "killers")
    memory.prepare("scripts.sample.x_fee__mutmut_1", ["t1"])
    memory.pytest_runtest_logreport(report("t1", failed=True))
    session = SimpleNamespace(testsfailed=0)
    calls: list[tuple[str, str | None]] = []
    items = {item.nodeid: item for item in fake_items(session, failures, calls)}
    loop = WarmLoop([], ["t0", "gone", "t1"], memory, 1)
    assert loop.child(as_session(session), items, "scripts.sample.x_fee__mutmut_2") == code
    assert run_mutation.environ[run_mutation.SWITCH] == "scripts.sample.x_fee__mutmut_2"
    assert calls[0] == ("t1", "t0")
    assert calls == ([("t1", "t0")] if code else [("t1", "t0"), ("t0", None)])


def test_a_forked_parent_returns_the_child_pid_without_grading(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = WarmLoop(["m"], ["t"], KillMemory(Path("unused")), 1)
    monkeypatch.setattr(run_mutation, "fork", lambda: 4242)
    monkeypatch.setattr(loop, "child", lambda *args: pytest.fail("the parent graded the mutant"))
    assert loop.fork(as_session(SimpleNamespace()), {}, "m") == 4242


@pytest.mark.parametrize("graded, code", [(1, 1), (0, 0), (None, run_mutation.CRASHED)])
def test_a_forked_child_exits_with_its_grade_and_a_crash_exits_suspicious(
    monkeypatch: pytest.MonkeyPatch, graded: int | None, code: int
) -> None:
    loop = WarmLoop(["m"], ["t"], KillMemory(Path("unused")), 1)
    session = as_session(SimpleNamespace())
    items = cast(Mapping[str, pytest.Item], {"t": SimpleNamespace(nodeid="t")})
    graded_calls: list[tuple[pytest.Session, Mapping[str, pytest.Item], str]] = []

    def child(child_session: pytest.Session, child_items: Mapping[str, pytest.Item], name: str) -> int:
        graded_calls.append((child_session, child_items, name))
        if graded is None:
            raise RuntimeError("the child crashed")
        return graded

    monkeypatch.setattr(run_mutation, "fork", lambda: 0)
    monkeypatch.setattr(run_mutation, "_exit", exit_with)
    monkeypatch.setattr(loop, "child", child)
    with pytest.raises(Exited) as exited:
        loop.fork(session, items, "m")
    assert exited.value.args == (code,)
    assert graded_calls == [(session, items, "m")]


def test_the_loop_forks_each_mutant_at_most_lanes_at_a_time_and_rows_its_status_and_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_mutation, "monotonic", clock(1.0, 5.0, 6.0, 6.5, 7.0, 7.5, 8.0, 10.0))
    loop = WarmLoop(["m1", "m2", "m3"], ["t"], KillMemory(Path("unused")), 2)
    alive: list[int] = []
    forked: list[tuple[str, int]] = []
    finished = iter([(999, 0), (101, 256), (102, 0), (103, 256)])

    def fork(forked_session: pytest.Session, items: Mapping[str, pytest.Item], name: str) -> int:
        assert forked_session is session
        pid = 101 + len(forked)
        alive.append(pid)
        forked.append((name, len(alive)))
        assert set(items) == {"t"}
        return pid

    def reaped() -> tuple[int, int]:
        pid, status = next(finished)
        if pid in alive:
            alive.remove(pid)
        return pid, status

    frozen: list[int] = []
    monkeypatch.setattr(loop, "fork", fork)
    monkeypatch.setattr(run_mutation, "wait", reaped)
    monkeypatch.setattr(run_mutation, "freeze", lambda: frozen.append(len(forked)))
    session = SimpleNamespace(testsfailed=0, items=[SimpleNamespace(nodeid="t")])
    assert loop.plugin().pytest_runtestloop(as_session(session)) is True
    assert frozen == [0]
    assert forked == [("m1", 1), ("m2", 2), ("m3", 2)]
    assert loop.collection == 4.0
    assert loop.rows == [Row("m1", "killed", 1.0), Row("m2", "survived", 1.5), Row("m3", "killed", 2.5)]


def test_a_failed_collection_forks_nothing_and_leaves_the_session_to_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_mutation, "monotonic", clock(2.0, 5.0))
    loop = WarmLoop(["m1"], ["t"], KillMemory(Path("unused")), 1)
    monkeypatch.setattr(loop, "fork", lambda *args: pytest.fail("forked after a failed collection"))
    monkeypatch.setattr(run_mutation, "freeze", lambda: pytest.fail("froze after a failed collection"))
    assert loop.collection == 0.0
    assert loop.run(as_session(SimpleNamespace(testsfailed=1, items=[]))) is None
    assert loop.collection == 3.0
    assert loop.rows == []


def test_a_unit_without_covering_tests_grades_no_tests_without_collecting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_mutation, "pytest", SimpleNamespace(main=lambda *args, **kwargs: pytest.fail("collected")))
    unit = Unit("mutant", "key", ("m1", "m2"))
    rows, collection = run_mutation.mutants(unit, {}, KillMemory(Path("unused")), OPTIONS, 60.0, 4)
    assert rows == [Row("m1", "no tests", 0.0), Row("m2", "no tests", 0.0)]
    assert collection == 0.0


@pytest.mark.parametrize("status", [0, 2])
def test_a_unit_collects_its_fastest_tests_first_once_in_one_pytest_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv(run_mutation.SWITCH, run_mutation.environ.get(run_mutation.SWITCH, ""))
    memory = KillMemory(tmp_path / "killers")
    sessions: list[tuple[list[str], list[Any], str]] = []

    def main(args: list[str], plugins: list[Any]) -> int:
        sessions.append((args, plugins, run_mutation.environ[run_mutation.SWITCH]))
        loop = plugins[0].pytest_runtestloop.__self__
        loop.rows = [Row("m1", "killed", 0.5)]
        loop.collection = 2.0
        return status

    monkeypatch.setattr(run_mutation, "pytest", SimpleNamespace(main=main))
    unit = Unit("mutant", "key", ("m1", "m2", "m3"))
    tests = {"tests/test_a.py::a_slow_one": 30.0, "tests/test_a.py::b_fast_one": 10.0}
    if status:
        with pytest.raises(SystemExit) as stopped:
            run_mutation.mutants(unit, tests, memory, OPTIONS, 60.0, 4)
        assert stopped.value.code == status
        return
    assert run_mutation.mutants(unit, tests, memory, OPTIONS, 60.0, 4) == ([Row("m1", "killed", 0.5)], 2.0)
    [(args, plugins, switch)] = sessions
    loop = plugins[0].pytest_runtestloop.__self__
    assert args == [*OPTIONS, "tests/test_a.py::b_fast_one", "tests/test_a.py::a_slow_one"]
    assert plugins[1] is memory
    assert switch == ""
    assert (loop.members, loop.tests, loop.memory, loop.lanes) == (
        ("m1", "m2", "m3"),
        ["tests/test_a.py::b_fast_one", "tests/test_a.py::a_slow_one"],
        memory,
        2,
    )


def test_stats_collection_traces_one_session_and_returns_one_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(run_mutation.SWITCH, run_mutation.environ.get(run_mutation.SWITCH, ""))
    monkeypatch.setattr(run_mutation, "process_time", clock(2.0, 5.5))
    switches: list[str] = []

    def main(args: list[str], plugins: list[Any]) -> int:
        assert args == [*OPTIONS, "tests/test_a.py"]
        switches.append(run_mutation.environ[run_mutation.SWITCH])
        [recorder] = plugins
        recorder.pytest_runtest_logstart()
        run_mutation.HITS.update({"scripts.sample.x_fee", "scripts.sample.x_rate"})
        recorder.pytest_runtest_logreport(report("tests/test_a.py::test_b", 0.5))
        recorder.pytest_runtest_logreport(report("tests/test_a.py::test_a", 0.25))
        run_mutation.HITS.clear()
        return 1

    monkeypatch.setattr(run_mutation, "pytest", SimpleNamespace(main=main))
    [result] = run_mutation.collect(OPTIONS, ["tests/test_a.py"])
    assert switches == ["stats"]
    assert result == {
        "status": 1,
        "tests": {
            "scripts.sample.x_fee": ["tests/test_a.py::test_a", "tests/test_a.py::test_b"],
            "scripts.sample.x_rate": ["tests/test_a.py::test_a", "tests/test_a.py::test_b"],
        },
        "durations": {"tests/test_a.py::test_b": 0.5, "tests/test_a.py::test_a": 0.25},
        "cpu": 3.5,
    }


def test_stats_read_the_content_keyed_cache_and_collect_its_misses_here(monkeypatch: pytest.MonkeyPatch) -> None:
    collected: list[tuple[Any, ...]] = []
    monkeypatch.setattr(run_mutation, "collect", lambda *args: collected.append(args) or [{"status": 0}])

    def cached(
        root: Path, cache: Path, changes: dict[str, Any], files: list[str], options: list[str], collect: Any
    ) -> list[dict[str, Any]]:
        assert (root, cache, changes, files, options) == (Path("root"), Path("cache"), {}, ["tests/test_a.py"], OPTIONS)
        return [{"reused": True}, *collect(["tests/test_a.py"])]

    monkeypatch.setattr(run_mutation, "collect_cached_stats", cached)
    records = run_mutation.stats(Path("root"), Path("cache"), ("tests/test_a.py",), tuple(OPTIONS))
    assert records == [{"reused": True}, {"status": 0}]
    assert collected == [(tuple(OPTIONS), ["tests/test_a.py"])]


def test_a_stats_request_returns_its_records(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(run_mutation, "stats", lambda *args: calls.append(args) or [{"status": 0}])
    request = {
        "kind": "stats",
        "key": "k",
        "members": ["tests/test_a.py"],
        "options": OPTIONS,
        "root": "r",
        "cache": "c",
    }
    assert run_mutation.run(request) == {"stats": [{"status": 0}]}
    assert calls == [(Path("r"), Path("c"), ("tests/test_a.py",), OPTIONS)]


def test_a_mutant_request_returns_its_rows_and_collection_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def graded(*args: Any) -> tuple[list[Row], float]:
        calls.append(args)
        return [Row("m1", "killed", 0.5)], 2.0

    monkeypatch.setattr(run_mutation, "mutants", graded)
    request = {
        "kind": "mutant",
        "key": "k",
        "members": ["m1"],
        "options": OPTIONS,
        "tests": {"t": 1.0},
        "killers": "killers",
        "budget": 60.0,
        "cap": 4,
    }
    result = run_mutation.run(request)
    assert result == {"collection_seconds": 2.0, "rows": [{"name": "m1", "status": "killed", "seconds": 0.5}]}
    [(unit, tests, memory, options, budget, cap)] = calls
    assert (unit, tests, options, budget, cap) == (Unit("mutant", "k", ("m1",)), {"t": 1.0}, OPTIONS, 60.0, 4)
    assert memory.folder == Path("killers")


def test_main_installs_the_light_main_and_writes_the_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(run_mutation, "install", installed.append)
    monkeypatch.setattr(run_mutation, "run", lambda request: {"echo": request})
    (tmp_path / "request.json").write_text(json.dumps({"kind": "stats"}))
    assert run_mutation.main([str(tmp_path / "request.json"), str(tmp_path / "result.json")]) == 0
    assert installed == [sys.modules]
    assert json.loads((tmp_path / "result.json").read_text()) == {"echo": {"kind": "stats"}}


def corpus(path: Path, mode: str) -> Path:
    for package in ("hooks", "scripts", "tests"):
        (path / package).mkdir(parents=True)
    (path / "hooks/__init__.py").touch()
    (path / "scripts/__init__.py").touch()
    place_flake_ledger(path / "tests")
    for folder in ("scripts/ci_mutation", "scripts/ci_lab"):
        shutil.copytree(REPO / folder, path / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (path / plant.SAMPLE).write_text(plant.SOURCE)
    (path / plant.TEST).write_text(plant.TESTS[mode])
    return path


def lab(tree: Path, request: dict[str, Any]) -> dict[str, Any]:
    (tree / "request.json").write_text(json.dumps(request))
    subprocess.run(
        [sys.executable, "-m", "scripts.ci_lab.run_mutation", "request.json", "result.json"],
        cwd=tree,
        check=True,
        timeout=50,
    )
    return json.loads((tree / "result.json").read_text())


def graded_by_lab(work: Path, tree: Path, names: list[str], tests: dict[str, float]) -> dict[str, Any]:
    request = {
        "kind": "mutant",
        "key": "lab",
        "members": names,
        "options": OPTIONS,
        "tests": tests,
        "killers": str(work / "killers"),
        "budget": 60.0,
        "cap": 2,
    }
    started = time.monotonic()
    graded = lab(tree, request)
    return {**graded, "elapsed": time.monotonic() - started}


def statuses(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {row["name"]: row["status"] for row in rows}


@pytest.fixture(scope="module", params=["kill", "survive"])
def corpus_run(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    mode = str(request.param)
    work = tmp_path_factory.mktemp(f"lab-{mode}")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        reference = run_gate(corpus(work / "reference", mode), {plant.SAMPLE: {1, 2}}, work / "evidence", 50)
        assert reference["not_mutated"] == [], reference["not_mutated"]
        root = corpus(work / "root", mode)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        tree = corpus(work / "tree", mode)
        names = run_mutation.trampoline(tree, plant.SAMPLE)
        stats_request = {
            "kind": "stats",
            "key": "lab",
            "members": [plant.TEST],
            "options": OPTIONS,
            "root": str(root),
            "cache": str(work / "cache"),
        }
        records = lab(tree, stats_request)["stats"]
        tests = {test: record["durations"][test] for record in records for test in record["tests"].get(FUNCTION, [])}
        return {
            "mode": mode,
            "reference": statuses(reference["graded"][0]["rows"]),
            "names": names,
            "tests": tests,
            "graded": graded_by_lab(work, tree, names, tests),
            "untrampolined": graded_by_lab(work, root, names, tests),
        }


def assert_equivalent(corpus_run: dict[str, Any], graded: dict[str, Any]) -> None:
    """Hold graded to the status the selection gate's run_selected gave every corpus mutant."""
    reference = corpus_run["reference"]
    assert corpus_run["tests"], "stats reached no test of the planted function"
    assert sorted(reference) == sorted(corpus_run["names"])
    assert statuses(graded["rows"]) == reference


@pytest.mark.feature("lab-warm-mutant-runner")
def test_the_warm_runner_grades_every_corpus_mutant_as_the_selection_gate_does(corpus_run: dict[str, Any]) -> None:
    assert_equivalent(corpus_run, corpus_run["graded"])


@pytest.mark.parametrize("corpus_run", ["kill"], indirect=True)
@pytest.mark.feature("lab-warm-mutant-runner")
def test_a_runner_that_grades_a_killable_mutant_survived_fails_the_equivalence(corpus_run: dict[str, Any]) -> None:
    planted = corpus_run["untrampolined"]
    assert set(corpus_run["reference"].values()) == {"killed"}
    assert set(statuses(planted["rows"]).values()) == {"survived"}
    with pytest.raises(AssertionError):
        assert_equivalent(corpus_run, planted)


@pytest.mark.wall_clock
@pytest.mark.parametrize("corpus_run", ["survive"], indirect=True)
@pytest.mark.feature("lab-warm-mutant-runner")
def test_the_fixed_cost_per_mutant_is_a_fraction_of_the_one_collection(corpus_run: dict[str, Any]) -> None:
    graded = corpus_run["graded"]
    seconds = [row["seconds"] for row in graded["rows"]]
    once = graded["elapsed"] - sum(seconds)
    ratio = statistics.median(seconds) / once
    warnings.warn(
        f"lab warm runner fixed cost ratio {ratio:.4f}: mutant seconds {seconds}, one time seconds {once:.4f}, "
        f"of which collection {graded['collection_seconds']:.4f}"
    )
    assert ratio <= FIXED_COST_RATIO
