import hashlib
import json
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mutmut.mutation.data import SourceFileMutationData

from scripts.ci_mutation import selection
from scripts.ci_mutation.report import evaluate


class ChildExit(BaseException):
    pass


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("MUTANT_UNDER_TEST", raising=False)
    name = "scripts.sample.x_f__mutmut_1"
    tests = ["tests/test_sample.py::fast", "tests/test_sample.py::killer", "tests/test_sample.py::other"]
    state = SimpleNamespace(
        name=name,
        tests=tests,
        killer=tests[1],
        forks=[12, 13],
        exits=[(12, 24 << 8), (13, 1 << 8)],
        runs=[],
        timers=[],
        signals=[],
        results=[],
        overwrite=None,
        interrupt=False,
        exit_code=1,
    )

    def fork() -> int:
        return state.forks.pop(0)

    def wait() -> tuple[int, int]:
        if state.interrupt and len(state.forks) == 0:
            raise KeyboardInterrupt
        return state.exits.pop(0)

    def exit_child(status: int) -> None:
        raise ChildExit(status)

    class PytestRunner:
        def execute_pytest(self, params: list[str], **kwargs: Any) -> int:
            state.runs.append((os.environ.get("MUTANT_UNDER_TEST"), params))
            return state.exit_code

        def run_tests(self, *, mutant_name: str | None, tests: list[str]) -> int:
            return self.execute_pytest(tests)

    def cli(args: list[str]) -> None:
        pid = runner.os.fork()
        data.register_pid(pid=pid, key=name)
        assert data.key_by_pid == {12: name}
        if state.overwrite:
            record.write_text(state.overwrite)
        pid, status = runner.os.wait()
        state.results.append((pid, os.waitstatus_to_exitcode(status)))
        data.register_result(pid=pid, exit_code=state.results[-1][1])

    runner = SimpleNamespace(
        PytestRunner=PytestRunner,
        collect_source_file_mutation_data=None,
        mutmut=SimpleNamespace(
            tests_by_mangled_function_name={"scripts.sample.x_f": set(tests)},
            duration_by_test=dict(zip(tests, [1.0, 5.0, 2.0], strict=True)),
        ),
        mangled_name_from_mutant_name=lambda key: key.partition("__mutmut_")[0],
        status_by_exit_code={
            0: "survived",
            1: "killed",
            24: "timeout",
            -24: "timeout",
            36: "timeout",
            152: "timeout",
            255: "timeout",
        },
        Config=SimpleNamespace(get=lambda: SimpleNamespace(timeout_constant=1.5, timeout_multiplier=3.0)),
        register_timeout=lambda **kwargs: state.timers.append(kwargs),
        BadTestExecutionCommandsException=RuntimeError,
        cli=cli,
    )
    real_os = SimpleNamespace(
        __name__="os",
        environ=os.environ,
        fork=fork,
        wait=wait,
        waitstatus_to_exitcode=os.waitstatus_to_exitcode,
        _exit=exit_child,
        kill=lambda pid, sig: state.signals.append((pid, sig)),
        cpu_count=lambda: 2,
    )
    monkeypatch.setattr(selection, "os", real_os)
    monkeypatch.setattr(SourceFileMutationData, "save", lambda self: None)
    data = SourceFileMutationData(path="scripts/sample.py")
    data.exit_code_by_key[name] = None
    state.data = data
    state.runner = runner
    record = tmp_path / "killers" / hashlib.sha256(b"scripts.sample.x_f").hexdigest()
    record.parent.mkdir()
    selection_file = tmp_path / "selection.json"
    selection_file.write_text(json.dumps({"scripts/sample.py": {"lines": [2], "tests": tests}}))

    def run() -> None:
        if state.killer is not None:
            record.write_text(state.killer)
        monkeypatch.setitem(sys.modules, "mutmut", SimpleNamespace(__main__=runner))
        for module in list(sys.modules):
            if module == "scripts" or module.startswith("scripts."):
                monkeypatch.setitem(sys.modules, module, sys.modules[module])
        selection.run_selected(selection_file, (0, 1))

    state.run = run
    return state


@pytest.mark.feature("mutation-timeout-without-killer")
@pytest.mark.parametrize("status", [24 << 8, signal.SIGXCPU, 36 << 8, 152 << 8, 255 << 8])
def test_a_hanging_killer_gets_one_retry_and_a_complete_result(engine: Any, status: int) -> None:
    engine.exits[0] = (12, status)
    engine.run()
    assert engine.results == [(12, 1)]
    assert engine.data.exit_code_by_key == {engine.name: 1}
    assert engine.data.key_by_pid == {}
    assert engine.forks == []
    assert engine.exits == []
    assert engine.timers == [{"pid": 13, "timeout_s": 28.5}]
    row = {"status": engine.runner.status_by_exit_code[engine.results[0][1]], "lines": [2]}
    assert evaluate("scripts/sample.py", [row], {2}, {})["failures"] == []


@pytest.mark.feature("mutation-timeout-without-killer")
@pytest.mark.parametrize(
    "killer,status",
    [
        (None, 24 << 8),
        ("outside", 24 << 8),
        ("tests/test_sample.py::killer", 1 << 8),
        ("tests/test_sample.py::killer", 0),
    ],
)
def test_only_a_timeout_with_a_killer_in_the_selection_retries(engine: Any, killer: str | None, status: int) -> None:
    engine.killer = killer
    engine.exits = [(12, status)]
    engine.run()
    assert engine.results == [(12, os.waitstatus_to_exitcode(status))]
    assert engine.forks == [13]
    assert engine.timers == []


@pytest.mark.feature("mutation-timeout-without-killer")
def test_a_second_timeout_stays_incomplete_without_another_retry(engine: Any) -> None:
    engine.exits = [(99, 0), (12, 24 << 8), (13, 24 << 8)]
    engine.run()
    assert engine.results == [(12, 24)]
    assert engine.forks == []
    assert engine.exits == []
    row = {"status": "timeout", "lines": [2]}
    assert evaluate("scripts/sample.py", [row], {2}, {})["failures"] == [row]


@pytest.mark.feature("mutation-timeout-without-killer")
def test_the_retry_child_excludes_the_original_killer_after_a_sibling_overwrites_it(engine: Any) -> None:
    engine.forks = [12, 0]
    engine.overwrite = engine.tests[0]
    with pytest.raises(ChildExit) as caught:
        engine.run()
    assert caught.value.args == (1,)
    assert engine.runs == [(engine.name, [engine.tests[0], engine.tests[2]])]


@pytest.mark.feature("mutation-timeout-without-killer")
def test_the_retry_child_preserves_a_surviving_result(engine: Any) -> None:
    engine.forks = [12, 0]
    engine.exit_code = 0
    with pytest.raises(ChildExit) as caught:
        engine.run()
    assert caught.value.args == (0,)
    assert engine.runs == [(engine.name, [engine.tests[0], engine.tests[2]])]


@pytest.mark.feature("mutation-timeout-without-killer")
def test_an_interrupt_stops_the_replacement_child(engine: Any) -> None:
    engine.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        engine.run()
    assert engine.signals == [(13, signal.SIGTERM)]
