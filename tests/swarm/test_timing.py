import io
import json
from types import SimpleNamespace

import pytest

from scripts.swarm import timing

pytestmark = pytest.mark.unit


class Output(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushed = []

    def flush(self):
        self.flushed.append(self.getvalue())


def test_stage_records_separate_cpu_and_flushes_before_work(monkeypatch):
    output = Output()
    monkeypatch.setattr(timing.sys, "stderr", output)
    monkeypatch.setattr(timing.os, "getpid", lambda: 71)
    monkeypatch.setattr(timing.time, "monotonic", iter([100.0, 109.0]).__next__)
    usages = iter(
        [
            SimpleNamespace(ru_utime=2.0, ru_stime=3.0),
            SimpleNamespace(ru_utime=10.0, ru_stime=20.0),
            SimpleNamespace(ru_utime=4.0, ru_stime=7.0),
            SimpleNamespace(ru_utime=13.0, ru_stime=24.0),
        ]
    )
    who = []

    def usage(kind):
        who.append(kind)
        return next(usages)

    monkeypatch.setattr(timing.resource, "getrusage", usage)
    with timing.tick("sw"):
        with timing.step("launch"):
            start = json.loads(output.getvalue().splitlines()[0])
            assert start == {
                "event": "swarm_tick_step",
                "phase": "started",
                "pid": 71,
                "slug": "sw",
                "step": "launch",
                "started": 100.0,
            }
            assert output.flushed == [output.getvalue()]
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rows == [
        start,
        {
            **start,
            "phase": "finished",
            "outcome": "success",
            "wall_s": 9.0,
            "own_cpu_s": 6.0,
            "reaped_child_cpu_s": 7.0,
        },
    ]
    assert who == [timing.resource.RUSAGE_THREAD, timing.resource.RUSAGE_CHILDREN] * 2
    assert output.flushed[-1] == output.getvalue()
    assert len(output.flushed) == 2
    assert timing.SWARM.get() is None


def test_unchanged_cpu_counters_report_exactly_zero(monkeypatch, capsys):
    monkeypatch.setattr(timing.resource, "getrusage", lambda kind: SimpleNamespace(ru_utime=0.3, ru_stime=0.6))
    with timing.tick("sw"), timing.step("idle"):
        pass
    finished = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert finished["own_cpu_s"] == 0.0
    assert finished["reaped_child_cpu_s"] == 0.0


def test_failure_records_cost_and_reraises_the_same_exception(monkeypatch, capsys):
    monkeypatch.setattr(timing.time, "monotonic", iter([1.0, 3.0]).__next__)
    usages = iter([SimpleNamespace(ru_utime=0.0, ru_stime=0.0)] * 4)
    monkeypatch.setattr(timing.resource, "getrusage", lambda kind: next(usages))
    error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught:
        with timing.tick("broken"):
            with timing.step("detector"):
                raise error
    assert caught.value is error
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    here = test_failure_records_cost_and_reraises_the_same_exception
    assert rows[-1] == {
        **rows[0],
        "phase": "finished",
        "outcome": "error",
        "error": "builtins.KeyboardInterrupt",
        "raised_at": f"tests/swarm/test_timing.py:{here.__code__.co_firstlineno + 8} {here.__name__}",
        "wall_s": 2.0,
        "own_cpu_s": 0.0,
        "reaped_child_cpu_s": 0.0,
    }
    assert timing.SWARM.get() is None


def _drop():
    raise ConnectionError("Connection closed by server.")


def test_failure_names_the_innermost_repository_frame_and_leaves_out_the_message(capsys):
    with pytest.raises(ConnectionError), timing.tick("sw"), timing.step("sweep"):
        _drop()
    finished = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert finished["error"] == "builtins.ConnectionError"
    assert finished["raised_at"] == f"tests/swarm/test_timing.py:{_drop.__code__.co_firstlineno + 1} _drop"
    assert "closed by server" not in json.dumps(finished)


def test_failure_outside_the_repository_names_its_caller_here(capsys):
    with pytest.raises(json.JSONDecodeError), timing.tick("sw"), timing.step("parse"):
        json.loads("{")
    finished = json.loads(capsys.readouterr().err.splitlines()[-1])
    here = test_failure_outside_the_repository_names_its_caller_here
    assert finished["error"] == "json.decoder.JSONDecodeError"
    assert finished["raised_at"] == f"tests/swarm/test_timing.py:{here.__code__.co_firstlineno + 2} {here.__name__}"


def test_a_failure_inside_a_timed_call_names_the_call_frame(capsys):
    with pytest.raises(json.JSONDecodeError), timing.tick("sw"):
        timing.call(json.loads, "{")
    finished = json.loads(capsys.readouterr().err.splitlines()[-1])
    line = timing.call.__code__.co_firstlineno + 3
    assert finished["raised_at"] == f"scripts/swarm/timing.py:{line} call"


def _raised_in(root, monkeypatch, name):
    root = root.resolve()
    monkeypatch.setattr(timing, "ROOT", root)
    code = compile("raise ValueError()", str(root / name), "exec")
    with pytest.raises(ValueError) as caught:
        exec(code)
    return timing.raised_at(caught.value)


def test_a_repository_file_outside_the_code_packages_names_no_frame(tmp_path, monkeypatch):
    assert _raised_in(tmp_path, monkeypatch, "setup.py") == ""


def test_a_hooks_frame_is_repository_code(tmp_path, monkeypatch):
    assert _raised_in(tmp_path, monkeypatch, "hooks/probe.py") == "hooks/probe.py:1 <module>"


def test_an_exception_that_never_ran_here_has_no_frame():
    assert timing.raised_at(ValueError()) == ""


def test_success_and_stop_records_carry_no_error_fields(capsys):
    with timing.tick("sw"), timing.step("fine"):
        pass
    finished = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert "error" not in finished
    assert "raised_at" not in finished


def test_nested_scopes_restore_the_parent_and_unscoped_steps_are_silent(capsys):
    with timing.tick("outer"):
        assert timing.SWARM.get() == "outer"
        with timing.tick("inner"):
            assert timing.SWARM.get() == "inner"
        assert timing.SWARM.get() == "outer"
    with timing.step("outside"):
        pass
    assert timing.SWARM.get() is None
    assert capsys.readouterr() == ("", "")


def test_call_preserves_arguments_result_and_stage_name(capsys):
    def task(value, *, amount):
        assert value is token
        assert amount == 4
        return result

    token, result = object(), object()
    with timing.tick("sw"):
        assert timing.call(task, token, amount=4) is result
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [row["step"] for row in rows] == [
        "tests.swarm.test_timing.test_call_preserves_arguments_result_and_stage_name.<locals>.task"
    ] * 2
    assert [row["phase"] for row in rows] == ["started", "finished"]


def test_instrumented_tick_preserves_signature_and_arguments(capsys):
    store, result = object(), object()

    @timing.instrument_tick
    def run_tick(actual_store, slug, value, *, amount):
        assert actual_store is store
        assert slug == "sw"
        assert value == 3
        assert amount == 4
        assert timing.SWARM.get() == "sw"
        return result

    assert run_tick.__name__ == "run_tick"
    assert run_tick.__wrapped__.__name__ == "run_tick"
    assert run_tick(store, "sw", 3, amount=4) is result
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [row["step"] for row in rows] == ["run_tick", "run_tick"]
    assert all(row["slug"] == "sw" for row in rows)
    assert rows[-1]["outcome"] == "success"
    assert timing.SWARM.get() is None


def test_timed_records_a_failure_without_reporting_it_or_keeping_the_tick(capsys):
    calls = []

    def broken(value, *, scale):
        raise RuntimeError(value * scale)

    failure = timing.ON_FAILURE.set(lambda step, error: calls.append(("failure", step)))
    before = timing.BEFORE_STEP.set(lambda: calls.append(("keep",)))
    try:
        with timing.tick("sw"), pytest.raises(RuntimeError, match="^6$"):
            timing.timed(broken, 2, scale=3)
    finally:
        timing.ON_FAILURE.reset(failure)
        timing.BEFORE_STEP.reset(before)
    assert calls == []
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [(row["phase"], row["step"]) for row in rows] == [
        (
            "started",
            "tests.swarm.test_timing.test_timed_records_a_failure_without_reporting_it_or_keeping_the_tick.<locals>.broken",
        ),
        (
            "finished",
            "tests.swarm.test_timing.test_timed_records_a_failure_without_reporting_it_or_keeping_the_tick.<locals>.broken",
        ),
    ]
    assert (rows[1]["outcome"], rows[1]["error"]) == ("error", "builtins.RuntimeError")


def test_timed_returns_the_result_of_its_call(capsys):
    with timing.tick("sw"):
        assert timing.timed(divmod, 7, 2) == (3, 1)
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [(row["phase"], row["step"], row.get("outcome")) for row in rows] == [
        ("started", "builtins.divmod", None),
        ("finished", "builtins.divmod", "success"),
    ]
