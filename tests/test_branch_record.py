import json
from pathlib import Path

from coverage import CoverageData

from tests import branch_record

SOURCE = "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
FIRST = {"hooks/sample.py": [(-1, 2), (2, -1)]}
SECOND = {"hooks/sample.py": [(-5, 6), (6, -5)]}
CONTRACT = {"version", "run", "attempt", "leg", "commit", "head", "pull", "environment", "manifest"}


def _leg(tmp_path: Path) -> Path:
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "sample.py").write_text(SOURCE)
    leg = tmp_path / "leg"
    leg.mkdir()
    return leg


def _coverage(path: Path, contexts: dict[str, dict]) -> None:
    data = CoverageData(basename=str(path))
    for context, arcs in contexts.items():
        data.set_context(context)
        data.add_arcs(arcs)
    data.write()


def _lines(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _inputs(tmp_path: Path, *tests: str) -> None:
    _lines(
        tmp_path / "inputs" / "finished" / "1.jsonl", [{"test": test, "files": [], "escapes": False} for test in tests]
    )


def _record(tmp_path: Path) -> dict:
    out = tmp_path / "out" / "record.json"
    argv = ["--coverage-dir", str(tmp_path / "leg"), "--inputs", str(tmp_path / "inputs")]
    argv += ["--results", str(tmp_path / "results"), "--flakes", str(tmp_path / "flakes.jsonl")]
    argv += ["--root", str(tmp_path), "--commit", "c0ffee", "--head", "beef", "--pull", "7644", "--run", "51"]
    argv += ["--attempt", "2", "--leg", "3", "--environment", "venv-key", "--out", str(out)]
    assert branch_record.main(argv) == 0
    return json.loads(out.read_text())


def _units(found: dict, test: str) -> set[str]:
    manifest = found["manifest"]
    return {manifest["units"][number] for number in manifest["tests"][test]["units"]}


def test_a_killed_leg_keeps_only_passed_tests_whose_worker_coverage_and_inputs_survived(tmp_path, capsys):
    leg = _leg(tmp_path)
    _coverage(
        leg / ".coverage.runner.pid41.Xabcx",
        {"tests/test_x.py::test_a|run": FIRST, "tests/test_x.py::test_c|run": SECOND},
    )
    _lines(
        tmp_path / "results" / "41.jsonl",
        [
            {"test": "tests/test_x.py::test_a", "outcome": "passed", "pid": 41},
            {"test": "tests/test_x.py::test_c", "outcome": "passed", "pid": 41},
            {"test": "tests/test_x.py::test_d", "outcome": "failed", "pid": 41},
            {"test": "tests/test_x.py::test_e", "outcome": "skipped", "pid": 41},
        ],
    )
    _lines(tmp_path / "results" / "42.jsonl", [{"test": "tests/test_x.py::test_b", "outcome": "passed", "pid": 42}])
    _inputs(tmp_path, "tests/test_x.py::test_a", "tests/test_x.py::test_b", "tests/test_x.py::test_d")
    found = _record(tmp_path)
    assert found["passed"] == ["tests/test_x.py::test_a"]
    assert found["failed"] == ["tests/test_x.py::test_d"] and found["flaky"] == []
    assert set(found) == CONTRACT | {"passed", "failed", "flaky"}
    assert {key: found[key] for key in CONTRACT - {"manifest"}} == {
        "version": 1,
        "run": 51,
        "attempt": 2,
        "leg": "3",
        "commit": "c0ffee",
        "head": "beef",
        "pull": 7644,
        "environment": "venv-key",
    }
    assert "lines" not in found["manifest"]
    assert found["manifest"]["commit"] == "c0ffee" and found["manifest"]["environment"] == "venv-key"
    assert _units(found, "tests/test_x.py::test_a") == {"hooks/sample.py::first", "hooks/sample.py::<module>"}
    assert "kept 1 of 3 passed tests" in capsys.readouterr().out


def test_a_complete_leg_keeps_every_passed_test_with_inputs_under_its_normalized_id(tmp_path):
    leg = _leg(tmp_path)
    (leg / "elapsed.json").write_text('{"seconds": 12}')
    _coverage(
        leg / ".coverage", {"tests/test_x.py::test_a|run": FIRST, "tests/test_x.py::test_b@slow|teardown": SECOND}
    )
    _lines(
        tmp_path / "results" / "9.jsonl",
        [
            {"test": "tests/test_x.py::test_a", "outcome": "passed", "pid": 9},
            {"test": "tests/test_x.py::test_b@slow", "outcome": "passed", "pid": 10},
            {"test": "tests/test_x.py::test_g", "outcome": "passed", "pid": 9},
        ],
    )
    _inputs(tmp_path, "tests/test_x.py::test_a", "tests/test_x.py::test_b@slow")
    found = _record(tmp_path)
    assert found["passed"] == ["tests/test_x.py::test_a", "tests/test_x.py::test_b"]
    assert _units(found, "tests/test_x.py::test_b@slow") == {"hooks/sample.py::second", "hooks/sample.py::<module>"}


def test_coverage_without_per_test_contexts_proves_no_pass(tmp_path):
    leg = _leg(tmp_path)
    (leg / "elapsed.json").write_text("{}")
    _coverage(leg / ".coverage", {"": FIRST})
    _coverage(leg / ".coverage.runner.pid9.Xabcx", {"": FIRST})
    _lines(tmp_path / "results" / "9.jsonl", [{"test": "tests/test_x.py::test_a", "outcome": "passed", "pid": 9}])
    _inputs(tmp_path, "tests/test_x.py::test_a")
    assert _record(tmp_path)["passed"] == []


def test_a_retry_or_a_crash_in_the_flake_ledger_outranks_a_pass(tmp_path):
    leg = _leg(tmp_path)
    (leg / "elapsed.json").write_text("{}")
    _coverage(leg / ".coverage", {f"tests/test_x.py::test_{name}|run": FIRST for name in "afhk"})
    _lines(
        tmp_path / "results" / "9.jsonl",
        [{"test": f"tests/test_x.py::test_{name}", "outcome": "passed", "pid": 9} for name in "afhk"],
    )
    _lines(tmp_path / "results" / "10.jsonl", [{"test": "tests/test_x.py::test_h", "outcome": "flaky", "pid": 10}])
    _lines(
        tmp_path / "flakes.jsonl",
        [
            {"test": "tests/test_x.py::test_a", "outcome": "flaky", "attempts": 2},
            {"test": "tests/test_x.py::test_f", "outcome": "failed", "attempts": 3, "crashes": 1},
        ],
    )
    _inputs(tmp_path, *(f"tests/test_x.py::test_{name}" for name in "afhk"))
    found = _record(tmp_path)
    assert found["passed"] == ["tests/test_x.py::test_k"]
    assert found["flaky"] == ["tests/test_x.py::test_a", "tests/test_x.py::test_h"]
    assert found["failed"] == ["tests/test_x.py::test_f"]


def test_a_partial_folder_never_crashes_and_keeps_only_what_it_can_prove(tmp_path):
    leg = _leg(tmp_path)
    (leg / "elapsed.json").write_text("{}")
    (leg / ".coverage").write_bytes(b"SQLite format 3\x00 cut short")
    (leg / ".coverage.runner.pid41.Xbadx").write_bytes(b"\x00" * 300)
    _coverage(leg / ".coverage.runner.pid43.Xokax", {"tests/test_x.py::test_k|run": FIRST})
    results = tmp_path / "results"
    results.mkdir()
    (results / "41.jsonl").write_text(
        json.dumps({"test": "tests/test_x.py::test_a", "outcome": "passed", "pid": 41})
        + "\n[1, 2]\n"
        + json.dumps({"test": "tests/test_x.py::test_k", "outcome": "passed", "pid": 43})
        + '\n{"test": "tests/test_x.py::test_m", "outc'
    )
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "1.jsonl").write_text(
        json.dumps({"test": "tests/test_x.py::test_a", "files": [], "escapes": False})
        + "\n"
        + json.dumps({"test": "tests/test_x.py::test_k", "files": ["docs/a.md"], "escapes": False})
        + '\n{"test": "tests/test_x.py::test_q", "files": [3], "escapes": false}\n{"test": "tests/'
    )
    found = _record(tmp_path)
    assert found["passed"] == ["tests/test_x.py::test_k"]
    assert sorted(found["manifest"]["tests"]) == ["tests/test_x.py::test_a", "tests/test_x.py::test_k"]


def test_an_empty_leg_writes_a_record_that_proves_nothing(tmp_path):
    found = _record(tmp_path)
    assert (found["passed"], found["failed"], found["flaky"]) == ([], [], [])
    assert found["manifest"]["tests"] == {} and found["manifest"]["units"] == []
