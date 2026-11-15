from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from scripts.ci_lab import __main__ as lab_cli
from scripts.ci_lab import evidence, objects
from scripts.ci_lab.digests import GradeInputs
from scripts.cli_delegates import delegated_cli

HEAD = "a" * 40
BASE = "b" * 40
GRADER = "c" * 40
STATIC = f"static-v1-{'1' * 64}"
FIRST_TEST = f"test-v1-{'2' * 64}"
SECOND_TEST = f"test-v1-{'3' * 64}"

PULL_REQUEST_BLOCK = f"""grade run-2
repository the-cloud-clockwork/agentihooks
head {HEAD}
base {BASE}
tier pull_request
grader {GRADER}
kinds static:1 test:2
unit static {STATIC} run run-2
  report lab/static/{"1" * 64}/report.json
  log lab/static/{"1" * 64}/log.txt
unit test {FIRST_TEST} run run-1
  report lab/test/{"2" * 64}/report.json
  log lab/test/{"2" * 64}/log.txt
unit test {SECOND_TEST} run run-2
  report lab/test/{"3" * 64}/report.json
  log lab/test/{"3" * 64}/log.txt
"""

QUEUE_BLOCK = f"""grade run-3
repository the-cloud-clockwork/agentihooks
head {HEAD}
base {BASE}
tier queue
grader {GRADER}
kinds test:1
unit test {FIRST_TEST} run run-1
  report lab/test/{"2" * 64}/report.json
  log lab/test/{"2" * 64}/log.txt
"""


def stored(blobs: objects.Objects, kind: str, key: str, names: tuple[str, ...] = ("report", "log")) -> None:
    for name in names:
        blobs.write_once(objects.unit_path(kind, key, name), b"{}")


def grade(run: str, tier: str, units: tuple[evidence.GradeUnit, ...]) -> evidence.GradeEvidence:
    inputs = GradeInputs(repository="the-cloud-clockwork/agentihooks", head=HEAD, base=BASE, tier=tier, grader=GRADER)
    return evidence.GradeEvidence(run=run, inputs=inputs, units=units)


@pytest.fixture
def blobs(tmp_path: Path) -> objects.Objects:
    store = objects.LocalObjects(tmp_path)
    for kind, key in (("static", STATIC), ("test", FIRST_TEST), ("test", SECOND_TEST)):
        stored(store, kind, key)
    units = (
        evidence.GradeUnit(kind="test", key=SECOND_TEST, run="run-2"),
        evidence.GradeUnit(kind="static", key=STATIC, run="run-2"),
        evidence.GradeUnit(kind="test", key=FIRST_TEST, run="run-1"),
    )
    evidence.record(store, grade("run-3", "queue", (evidence.GradeUnit(kind="test", key=FIRST_TEST, run="run-1"),)))
    evidence.record(store, grade("run-2", "pull_request", units))
    return store


def test_evidence_by_grade_prints_the_golden_block(
    blobs: objects.Objects, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = lab_cli.main(["evidence", "--grade", "run-2", "--objects", str(tmp_path)], {})

    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (0, PULL_REQUEST_BLOCK, "")


def test_ci_lab_show_prints_every_grade_of_the_head_in_run_order(
    blobs: objects.Objects, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["ci", "lab", "show", HEAD, "--objects", str(tmp_path)]
    command = delegated_cli(argv)

    assert command is not None
    code = command(argv[1:])

    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (0, f"{PULL_REQUEST_BLOCK}\n{QUEUE_BLOCK}", "")


def test_a_second_record_of_a_run_keeps_the_first_evidence(
    blobs: objects.Objects, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence.record(blobs, grade("run-2", "promotion", ()))

    code = evidence.main(["--grade", "run-2", "--objects", str(tmp_path)])

    assert (code, capsys.readouterr().out) == (0, PULL_REQUEST_BLOCK)


def test_an_unknown_grade_exits_non_zero_naming_its_path(
    blobs: objects.Objects, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = evidence.main(["--grade", "run-9", "--objects", str(tmp_path)])

    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (1, "", "missing lab/grades/run-9/grade.json\n")


def test_a_head_without_a_grade_exits_non_zero_naming_its_path(
    blobs: objects.Objects, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = evidence.ci(["lab", "show", BASE, "--objects", str(tmp_path)])

    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (1, "", f"missing lab/heads/{BASE}/\n")


def test_a_head_marker_without_its_grade_exits_non_zero_naming_the_grade(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = objects.LocalObjects(tmp_path)
    stored(store, "test", FIRST_TEST)
    store.write_once(evidence.head_prefix(HEAD) + "run-2", b"")
    evidence.record(store, grade("run-3", "queue", (evidence.GradeUnit(kind="test", key=FIRST_TEST, run="run-1"),)))

    code = evidence.ci(["lab", "show", HEAD, "--objects", str(tmp_path)])

    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (1, QUEUE_BLOCK, "missing lab/grades/run-2/grade.json\n")


def test_ci_lab_show_orders_numeric_runs_by_value_and_prints_an_empty_grade(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = objects.LocalObjects(tmp_path)
    for run in ("10", "9"):
        evidence.record(store, grade(run, "queue", ()))
    block = (
        f"repository the-cloud-clockwork/agentihooks\nhead {HEAD}\nbase {BASE}\ntier queue\ngrader {GRADER}\nkinds\n"
    )

    code = evidence.ci(["lab", "show", HEAD, "--objects", str(tmp_path)])

    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (0, f"grade 9\n{block}\ngrade 10\n{block}", "")


@pytest.mark.parametrize(
    ("command", "argv", "error"),
    [
        (evidence.main, ["--grade", "run-2"], "python -m scripts.ci_lab evidence: error: {} --objects"),
        (evidence.main, ["--objects", "objects"], "python -m scripts.ci_lab evidence: error: {} --grade"),
        (evidence.ci, [], "agentihooks ci: error: {} group"),
        (evidence.ci, ["lab"], "agentihooks ci lab: error: {} command"),
        (evidence.ci, ["lab", "show", HEAD], "agentihooks ci lab show: error: {} --objects"),
    ],
)
def test_a_missing_argument_exits_two_naming_it(
    command: Callable[[Sequence[str]], int], argv: list[str], error: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        command(argv)

    last = capsys.readouterr().err.splitlines()[-1]
    assert (exited.value.code, last) == (2, error.format("the following arguments are required:"))


@pytest.mark.parametrize(
    ("absent", "path"),
    [("report", f"lab/test/{'2' * 64}/report.json"), ("log", f"lab/test/{'2' * 64}/log.txt")],
)
def test_a_missing_unit_object_is_marked_and_exits_non_zero(
    absent: str, path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = objects.LocalObjects(tmp_path)
    stored(store, "test", FIRST_TEST, names=tuple(name for name in ("report", "log") if name != absent))
    evidence.record(store, grade("run-3", "queue", (evidence.GradeUnit(kind="test", key=FIRST_TEST, run="run-1"),)))

    code = evidence.main(["--grade", "run-3", "--objects", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == QUEUE_BLOCK.replace(f"  {absent} {path}\n", f"  {absent} {path} missing\n")
    assert captured.err == f"missing {path}\n"
