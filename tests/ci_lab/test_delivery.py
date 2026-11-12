"""Tests for the delivery.yaml lab block, the cutover output and the runtime config schema."""

import json
import runpy
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci_lab import __main__ as entry
from scripts.ci_lab import delivery

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = {"on": "push", "jobs": {"lint": {}, "type-gate": {}, "unit": {}}}
RUNTIME = {
    "classes": ["hotfix", "pull_request", "queue", "promotion", "backfill"],
    "budget": {"coreMinutesPerDay": 1440, "shed": {"backfill": 0.6, "promotion": 0.8, "queue": 0.95}},
    "backfill": {"auditShare": 0.05, "moduleSweeps": True},
}


def tree(root: Path, lab: dict[str, object]) -> Path:
    """Write a repository tree holding one workflow and a delivery.yaml with the given lab block."""
    (root / ".github/workflows").mkdir(parents=True)
    (root / ".github/workflows/test.yml").write_text(yaml.safe_dump(WORKFLOW))
    (root / "delivery.yaml").write_text(yaml.safe_dump({"queue": ["./ci/unit"], "lab": lab}))
    return root


def block(**changes: object) -> dict[str, object]:
    """Return a valid lab block with the given keys replaced."""
    base: dict[str, object] = {
        "authoritative": [],
        "mutation": {"run_on": "every_push", "mutants_per_changed_line": "all", "arid": []},
        "check_prefix": "lab",
    }
    return {**base, **changes}


def test_head_copy_is_ignored(tmp_path: Path) -> None:
    grader = tree(tmp_path / "grader", block())
    tree(tmp_path / "head", block(authoritative=["lint", "unit"]))
    assert delivery.authoritative(grader) == ()


def test_authoritative_reads_grader_gates(tmp_path: Path) -> None:
    grader = tree(tmp_path / "grader", block(authoritative=["unit", "lint"]))
    assert delivery.authoritative(grader) == ("unit", "lint")


def test_unknown_gate_name_is_an_error(tmp_path: Path) -> None:
    grader = tree(tmp_path / "grader", block(authoritative=["lint", "no-such-gate"]))
    with pytest.raises(delivery.DeliveryError, match="no-such-gate"):
        delivery.authoritative(grader)


def test_gates_are_job_ids_of_every_workflow(tmp_path: Path) -> None:
    grader = tree(tmp_path, block())
    (tmp_path / ".github/workflows/other.yaml").write_text(yaml.safe_dump({"jobs": {"docs": {}}}))
    (tmp_path / ".github/workflows/notes.txt").write_text("jobs: {ignored: {}}")
    assert delivery.gates(grader) == frozenset({"lint", "type-gate", "unit", "docs"})


def test_lab_block_parses(tmp_path: Path) -> None:
    mutation = {"run_on": "ready", "mutants_per_changed_line": 3, "arid": ["scripts/x.py:f"]}
    expected = block(mutation=mutation, check_prefix="grade")
    assert delivery.lab(tree(tmp_path, expected)) == expected


def test_all_mutants_is_kept(tmp_path: Path) -> None:
    assert delivery.lab(tree(tmp_path, block()))["mutation"]["mutants_per_changed_line"] == delivery.ALL_MUTANTS


@pytest.mark.parametrize(
    "lab",
    [
        block(mutation={"run_on": "sometimes", "mutants_per_changed_line": "all", "arid": []}),
        block(mutation={"run_on": "ready", "mutants_per_changed_line": 0, "arid": []}),
        block(mutation={"run_on": "ready", "mutants_per_changed_line": "some", "arid": []}),
        block(mutation={"run_on": "ready", "mutants_per_changed_line": True, "arid": []}),
        block(mutation={"run_on": "ready", "mutants_per_changed_line": "all", "arid": "x"}),
        block(mutation={"run_on": "ready", "arid": []}),
        block(check_prefix=""),
        block(authoritative="lint"),
        block(authoritative=["lint", "lint"]),
        block(extra=1),
        {"authoritative": []},
    ],
)
def test_invalid_lab_block_is_an_error(tmp_path: Path, lab: dict[str, object]) -> None:
    with pytest.raises(delivery.DeliveryError):
        delivery.lab(tree(tmp_path, lab))


def test_missing_lab_block_is_an_error(tmp_path: Path) -> None:
    root = tree(tmp_path, block())
    (root / "delivery.yaml").write_text(yaml.safe_dump({"queue": ["./ci/unit"]}))
    with pytest.raises(delivery.DeliveryError, match="lab"):
        delivery.lab(root)


def test_cutover_writes_skip_to_github_output(tmp_path: Path) -> None:
    grader = tree(tmp_path / "grader", block(authoritative=["lint"]))
    output = tmp_path / "output"
    output.write_text("before=1\n")
    code = delivery.main(["cutover", "--grader-root", str(grader)], {"GITHUB_OUTPUT": str(output)})
    assert code == 0
    assert output.read_text() == 'before=1\nskip=["lint"]\n'


@pytest.mark.parametrize(("value", "skip"), [("true", []), ("false", ["lint"]), ("1", ["lint"]), ("TRUE", ["lint"])])
def test_lab_off_forces_an_empty_skip(tmp_path: Path, value: str, skip: list[str]) -> None:
    grader = tree(tmp_path / "grader", block(authoritative=["lint"]))
    output = tmp_path / "output"
    environ = {"GITHUB_OUTPUT": str(output), "AGENTIHOOKS_LAB_OFF": value}
    assert delivery.main(["cutover", "--grader-root", str(grader)], environ) == 0
    assert json.loads(output.read_text().removeprefix("skip=")) == skip


def test_cutover_refuses_an_unknown_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    grader = tree(tmp_path / "grader", block(authoritative=["ghost"]))
    output = tmp_path / "output"
    assert delivery.main(["cutover", "--grader-root", str(grader)], {"GITHUB_OUTPUT": str(output)}) == 1
    assert not output.exists()
    assert "ghost" in capsys.readouterr().err


def test_runtime_config_validates() -> None:
    assert delivery.runtime(RUNTIME) is RUNTIME


@pytest.mark.parametrize("fraction", [-0.01, 1.01, 2, "half"])
def test_runtime_rejects_a_shed_fraction_outside_zero_to_one(fraction: object) -> None:
    budget = {"coreMinutesPerDay": 1440, "shed": {"backfill": fraction}}
    with pytest.raises(delivery.DeliveryError, match="shed"):
        delivery.runtime({**RUNTIME, "budget": budget})


@pytest.mark.parametrize("fraction", [0, 1])
def test_runtime_accepts_shed_bounds(fraction: float) -> None:
    budget = {"coreMinutesPerDay": 1440, "shed": {"backfill": fraction}}
    document = {**RUNTIME, "budget": budget}
    assert delivery.runtime(document) == document


@pytest.mark.parametrize(
    "document",
    [
        {**RUNTIME, "budget": {"coreMinutesPerDay": 1440, "shed": {"pull_request": 0.5}}},
        {**RUNTIME, "budget": {"coreMinutesPerDay": 1440, "shed": {"hotfix": 0.5}}},
        {**RUNTIME, "budget": {"coreMinutesPerDay": 0, "shed": {}}},
        {**RUNTIME, "budget": {"shed": {}}},
        {**RUNTIME, "classes": ["hotfix", "nightly"]},
        {**RUNTIME, "classes": ["hotfix", "hotfix"]},
        {**RUNTIME, "backfill": {"auditShare": 1.5, "moduleSweeps": True}},
        {**RUNTIME, "extra": 1},
        {"classes": RUNTIME["classes"], "budget": RUNTIME["budget"]},
    ],
)
def test_runtime_rejects_invalid_documents(document: dict[str, object]) -> None:
    with pytest.raises(delivery.DeliveryError):
        delivery.runtime(document)


def test_load_runtime_reads_the_mounted_file(tmp_path: Path) -> None:
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(RUNTIME))
    assert delivery.load_runtime(path) == RUNTIME


def test_repository_file_has_an_empty_authoritative_list() -> None:
    assert delivery.authoritative(ROOT) == ()
    assert delivery.lab(ROOT)["check_prefix"] == "lab"


def test_template_carries_the_lab_block() -> None:
    template = yaml.safe_load((ROOT / "profiles/package/templates/ci/delivery.yaml").read_text())
    assert template["lab"] == block()


def test_module_entry_runs_cutover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    grader = tree(tmp_path / "grader", block())
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["scripts.ci_lab", "cutover", "--grader-root", str(grader)])
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("scripts.ci_lab", run_name="__main__")
    assert exited.value.code == 0
    assert output.read_text() == "skip=[]\n"


def test_entry_main_dispatches_to_delivery(tmp_path: Path) -> None:
    grader = tree(tmp_path / "grader", block(authoritative=["unit"]))
    output = tmp_path / "output"
    assert entry.main(["cutover", "--grader-root", str(grader)], {"GITHUB_OUTPUT": str(output)}) == 0
    assert output.read_text() == 'skip=["unit"]\n'


def test_runtime_command_validates_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump(RUNTIME))
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({**RUNTIME, "budget": {"coreMinutesPerDay": 1, "shed": {"queue": 1.5}}}))
    assert delivery.main(["runtime", str(good)], {}) == 0
    assert capsys.readouterr().err == ""
    assert delivery.main(["runtime", str(bad)], {}) == 1
    assert "budget.shed.queue" in capsys.readouterr().err


def test_error_messages_name_the_file_and_path(tmp_path: Path) -> None:
    budget = {"coreMinutesPerDay": 1, "shed": {"queue": 1.5}}
    with pytest.raises(delivery.DeliveryError) as runtime_error:
        delivery.runtime({**RUNTIME, "budget": budget})
    assert str(runtime_error.value) == "runtime budget.shed.queue: 1.5 is greater than the maximum of 1"
    root = tree(tmp_path / "missing", block())
    (root / "delivery.yaml").write_text(yaml.safe_dump({"queue": []}))
    with pytest.raises(delivery.DeliveryError) as missing:
        delivery.lab(root)
    assert str(missing.value) == "delivery.yaml : 'lab' is a required property"
    ghosts = tree(tmp_path / "ghosts", block(authoritative=["phantom", "ghost", "lint"]))
    with pytest.raises(delivery.DeliveryError) as unknown:
        delivery.lab(ghosts)
    assert str(unknown.value) == "lab.authoritative names unknown gates: ghost, phantom"


@pytest.mark.parametrize("argv", [[], ["cutover"]])
def test_command_and_grader_root_are_required(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        delivery.main(argv, {})
    assert exited.value.code == 2
    assert capsys.readouterr().err.startswith("usage: python -m scripts.ci_lab ")
