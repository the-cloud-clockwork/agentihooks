import dataclasses
import json
import os
import shutil
import sys
from pathlib import Path

import coverage
import pytest

from scripts.ci_lab import retry, run_tests, sandbox

ROOT = Path(__file__).resolve().parents[2]
VENV = Path(sys.prefix)
SUPPORT = (
    "tests/__init__.py",
    "tests/pass_record.py",
    "tests/flake_ledger.py",
    "tests/shards.py",
    ".github/coverage/coverage.ini",
)
FLAKE = """import os
from pathlib import Path


def test_flake():
    mark = Path(os.environ["FLAKE_MARK"])
    if not mark.exists():
        mark.write_text("seen")
        assert False, "first attempt fails"
"""
BROKEN = """import os
from pathlib import Path

mark = Path(os.environ["FLAKE_MARK"])
if not mark.exists():
    mark.write_text("seen")
    raise RuntimeError("first collection fails")


def test_collected():
    assert True
"""
OPENS = """from pathlib import Path

from hooks.plant import one
from scripts.plant import two


def test_listed():
    assert Path("data/listed.txt").read_text() == "listed"


def test_unlisted():
    assert Path("data/unlisted.txt").read_text() == "unlisted"
    assert one() + two() == 3
"""


def _tree(folder: Path, files: dict[str, str]) -> Path:
    for path in SUPPORT:
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, target)
    for path, text in files.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return folder


def _manifest(records: dict[str, list[str]]) -> dict[str, object]:
    tests = {test: {"units": [], "files": files, "escapes": False} for test, files in records.items()}
    return {"units": [], "tests": tests}


def _lab(tmp_path: Path, manifest: dict[str, object], whitelist: dict[str, str]) -> run_tests.Lab:
    root = tmp_path / "lab"
    root.mkdir()
    return run_tests.Lab(
        root=root,
        ports=sandbox.PortAllocator(first=21000, last=21099, width=100),
        whitelist=whitelist,
        manifest=manifest,
        flakes=tmp_path / "flakes.jsonl",
        timeout=120,
        net=True,
    )


@pytest.mark.feature("lab-report-flake-rows")
def test_a_planted_failure_is_red_on_both_attempts_and_final(tmp_path: Path) -> None:
    test = "tests/test_plant.py::test_one_is_two"
    tree = _tree(tmp_path / "tree", {"tests/test_plant.py": "def test_one_is_two():\n    assert 1 == 2\n"})
    lab = _lab(tmp_path, _manifest({test: []}), {})

    verdict = run_tests.run((test,), tree, VENV, 1, lab)

    assert (verdict.outcome, verdict.flaky, verdict.attempts) == ("failed", False, 2)
    assert "assert 1 == 2" in verdict.log.read_text()
    assert retry.decide(retry.UnitResult(outcome=verdict.outcome, attempts=verdict.attempts)) == "final"
    assert not lab.flakes.exists()


@pytest.mark.feature("lab-report-flake-rows")
def test_a_seeded_flake_is_green_flaky_and_written_to_the_flake_ledger(tmp_path: Path) -> None:
    test = "tests/test_flake.py::test_flake"
    tree = _tree(tmp_path / "tree", {"tests/test_flake.py": FLAKE})
    lab = _lab(tmp_path, _manifest({test: []}), {"FLAKE_MARK": str(tmp_path / "mark")})

    verdict = run_tests.run((test,), tree, VENV, 1, lab)

    assert (verdict.outcome, verdict.flaky, verdict.attempts) == ("passed", True, 2)
    assert (verdict.broad, verdict.outside) == (False, ())
    rows = [json.loads(line) for line in lab.flakes.read_text().splitlines()]
    assert [(row["test"], row["outcome"], row["attempts"]) for row in rows] == [(test, "flaky", 2)]
    assert "first attempt fails" in rows[0]["errors"][0]


@pytest.mark.feature("lab-report-flake-rows")
def test_a_seeded_flaky_batch_returns_the_rows_it_writes_to_the_flake_ledger(tmp_path: Path) -> None:
    test = "tests/test_flake.py::test_flake"
    tree = _tree(tmp_path / "tree", {"tests/test_flake.py": FLAKE})
    lab = _lab(tmp_path, _manifest({test: []}), {"FLAKE_MARK": str(tmp_path / "mark")})

    verdict = run_tests.run((test,), tree, VENV, 1, lab)

    rows = [json.loads(line) for line in lab.flakes.read_text().splitlines()]
    assert [(row["test"], row["outcome"], row["attempts"]) for row in verdict.flakes] == [(test, "flaky", 2)]
    assert list(verdict.flakes) == rows


@pytest.mark.feature("lab-report-flake-rows")
def test_a_red_retry_returns_no_flake_rows(tmp_path: Path) -> None:
    test = "tests/test_plant.py::test_one_is_two"
    tree = _tree(tmp_path / "tree", {"tests/test_plant.py": "def test_one_is_two():\n    assert 1 == 2\n"})
    lab = _lab(tmp_path, _manifest({test: []}), {})

    verdict = run_tests.run((test,), tree, VENV, 1, lab)

    assert (verdict.outcome, verdict.attempts, verdict.flakes) == ("failed", 2, ())


@pytest.mark.feature("lab-report-flake-rows")
def test_a_green_first_attempt_returns_no_flake_rows(tmp_path: Path) -> None:
    test = "tests/test_green.py::test_one_is_one"
    tree = _tree(tmp_path / "tree", {"tests/test_green.py": "def test_one_is_one():\n    assert 1 == 1\n"})
    lab = _lab(tmp_path, _manifest({test: []}), {})

    verdict = run_tests.run((test,), tree, VENV, 1, lab)

    assert (verdict.outcome, verdict.attempts, verdict.flakes) == ("passed", 1, ())


def test_a_green_batch_stores_durations_coverage_and_files_opened_outside_its_manifest(tmp_path: Path) -> None:
    listed, unlisted = "tests/test_opens.py::test_listed", "tests/test_opens.py::test_unlisted"
    files = {
        "tests/test_opens.py": OPENS,
        "data/listed.txt": "listed",
        "data/unlisted.txt": "unlisted",
        "hooks/__init__.py": "",
        "hooks/plant.py": "def one():\n    return 1\n",
        "scripts/__init__.py": "",
        "scripts/plant.py": "def two():\n    return 2\n",
    }
    tree = _tree(tmp_path / "tree", files)
    lab = _lab(tmp_path, _manifest({listed: ["data/listed.txt"], unlisted: []}), {})

    verdict = run_tests.run((listed, unlisted), tree, VENV, 2, lab)

    assert (verdict.outcome, verdict.flaky, verdict.attempts) == ("passed", False, 1)
    assert (verdict.broad, verdict.outside) == (True, ("data/unlisted.txt",))
    assert set(verdict.durations) == {listed, unlisted}
    assert all(seconds >= 0 for seconds in verdict.durations.values())
    assert (verdict.log.parent.parent, verdict.coverage.parent.parent) == (lab.root, lab.root)
    data = coverage.CoverageData(str(verdict.coverage))
    data.read()
    assert {"hooks/plant.py", "scripts/plant.py"} <= {"/".join(path.split("/")[-2:]) for path in data.measured_files()}
    assert any(context.startswith(unlisted) for context in data.measured_contexts())
    assert not lab.flakes.exists()


def test_a_batch_whose_first_collection_fails_is_flaky_with_a_row_per_batch_test(tmp_path: Path) -> None:
    test = "tests/test_broken.py::test_collected"
    tree = _tree(tmp_path / "tree", {"tests/test_broken.py": BROKEN})
    lab = _lab(tmp_path, _manifest({test: []}), {"FLAKE_MARK": str(tmp_path / "mark")})

    verdict = run_tests.run((test,), tree, VENV, 1, lab)

    assert (verdict.outcome, verdict.flaky, verdict.attempts) == ("passed", True, 2)
    [row] = [json.loads(line) for line in lab.flakes.read_text().splitlines()]
    assert (row["test"], row["outcome"], row["attempts"], row["tracebacks"]) == (test, "flaky", 2, [])
    assert row["errors"][0].startswith("pytest exited with code ")


def test_a_batch_the_timeout_kills_is_red_on_both_attempts_without_durations(tmp_path: Path) -> None:
    test = "tests/test_plant.py::test_one_is_two"
    tree = _tree(tmp_path / "tree", {"tests/test_plant.py": "def test_one_is_two():\n    assert 1 == 2\n"})
    lab = dataclasses.replace(_lab(tmp_path, _manifest({test: []}), {}), timeout=0.05)

    verdict = run_tests.run((test,), tree, VENV, 1, lab)

    assert (verdict.outcome, verdict.flaky, verdict.attempts, verdict.durations) == ("failed", False, 2, {})
    assert not lab.flakes.exists()


def test_the_environment_puts_the_venv_first_and_the_outputs_under_the_attempt_folder(tmp_path: Path) -> None:
    tree, venv, folder = tmp_path / "tree", tmp_path / "venv", tmp_path / "attempt"

    given = run_tests._environment(tree, venv, folder, {"PATH": "/usr/bin", "KEPT": "1"})
    default = run_tests._environment(tree, venv, folder, {})

    assert given == {
        "KEPT": "1",
        "PATH": f"{venv / 'bin'}:/usr/bin",
        "PYTHONPATH": str(folder / "site"),
        "CI_RESULTS": str(folder / "results"),
        "CI_FLAKES": str(folder / "flakes.jsonl"),
        "COVERAGE_FILE": str(folder / ".coverage"),
        "CI_LAB_TREE": str(tree),
        "CI_LAB_OPENED": str(folder / "opened"),
    }
    assert default["PATH"] == f"{venv / 'bin'}:{os.defpath}"


def test_a_flake_with_no_failed_test_writes_one_row_per_batch_test(tmp_path: Path) -> None:
    folder = tmp_path / "attempt"
    folder.mkdir()
    first = run_tests.Attempt(code=2, folder=folder, log=folder / "log.txt")

    rows = run_tests.flake_rows(first, ("tests/test_a.py::test_x@group", "tests/test_a.py::test_y"))

    row = {"errors": ["pytest exited with code 2"], "tracebacks": [], "attempts": 2, "outcome": "flaky"}
    assert rows == [{"test": "tests/test_a.py::test_x", **row}, {"test": "tests/test_a.py::test_y", **row}]


def test_a_flake_row_keeps_the_first_attempts_errors_and_counts_two_attempts(tmp_path: Path) -> None:
    folder = tmp_path / "attempt"
    folder.mkdir()
    failed = {"test": "t::a", "outcome": "failed", "attempts": 1, "errors": ["boom"], "tracebacks": ["tb"]}
    flaky = {**failed, "test": "t::b", "outcome": "flaky", "attempts": 2}
    (folder / "flakes.jsonl").write_text(json.dumps(failed) + "\n" + json.dumps(flaky) + "\n")
    first = run_tests.Attempt(code=1, folder=folder, log=folder / "log.txt")

    rows = run_tests.flake_rows(first, ("t::a", "t::b"))

    assert rows == [{**failed, "outcome": "flaky", "attempts": 2}, flaky]


def test_outside_leaves_out_listed_shared_skipped_and_missing_files(tmp_path: Path) -> None:
    skipped = (".git/HEAD", ".venv/x", ".playwright/x", "node_modules/x", ".pytest_cache/v")
    shared = (
        "pyproject.toml",
        ".github/test-requirements.lock",
        ".test_durations",
        ".github/x.yml",
        *skipped,
        "pkg/m.pyc",
    )
    for path in ("data/a.txt", "data/b.txt", *shared):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("")
    (tmp_path / "pkg/__pycache__").mkdir()
    (tmp_path / "pkg/__pycache__/m.txt").write_text("")
    opened = {
        "data/a.txt",
        "data/b.txt",
        "data/missing.txt",
        *shared,
        "pkg/__pycache__/m.txt",
        "data",
    }
    manifest = _manifest({"t::one": ["data/a.txt"], "t::two": []})

    assert run_tests.outside(opened, ("t::one", "t::two"), tmp_path, manifest) == ("data/b.txt",)


@pytest.mark.feature("lab-scheduler-test-units")
def test_outside_lists_every_unshared_file_a_test_module_the_manifest_lacks_opened(tmp_path: Path) -> None:
    for path in ("data/a.txt", "data/b.txt", ".github/test-requirements.lock"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("")
    opened = {"data/a.txt", "data/b.txt", ".github/test-requirements.lock"}
    manifest = _manifest({"t::one": ["data/a.txt"]})

    assert run_tests.outside(opened, ("t::one", "tests/test_new.py"), tmp_path, manifest) == ("data/b.txt",)
    assert run_tests.outside(opened, ("tests/test_new.py",), tmp_path, manifest) == ("data/a.txt", "data/b.txt")
