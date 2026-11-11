"""Tests for the delivery.yaml lab block, the cutover output and the runtime config schema."""

import json
from pathlib import Path

import pytest
import yaml

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
    lab = delivery.lab(tree(tmp_path, block(mutation=mutation, check_prefix="grade")))
    assert lab == delivery.Lab(
        authoritative=(),
        mutation=delivery.Mutation(run_on="ready", mutants_per_changed_line=3, arid=("scripts/x.py:f",)),
        check_prefix="grade",
    )


def test_all_mutants_is_none(tmp_path: Path) -> None:
    assert delivery.lab(tree(tmp_path, block())).mutation.mutants_per_changed_line is None


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


def test_lab_off_forces_an_empty_skip(tmp_path: Path) -> None:
    grader = tree(tmp_path / "grader", block(authoritative=["lint"]))
    output = tmp_path / "output"
    environ = {"GITHUB_OUTPUT": str(output), "AGENTIHOOKS_LAB_OFF": "true"}
    assert delivery.main(["cutover", "--grader-root", str(grader)], environ) == 0
    assert json.loads(output.read_text().removeprefix("skip=")) == []


def test_cutover_refuses_an_unknown_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    grader = tree(tmp_path / "grader", block(authoritative=["ghost"]))
    output = tmp_path / "output"
    assert delivery.main(["cutover", "--grader-root", str(grader)], {"GITHUB_OUTPUT": str(output)}) == 1
    assert not output.exists()
    assert "ghost" in capsys.readouterr().err


def test_runtime_config_validates() -> None:
    assert delivery.runtime(RUNTIME) == RUNTIME


@pytest.mark.parametrize("fraction", [-0.01, 1.01, 2, "half"])
def test_runtime_rejects_a_shed_fraction_outside_zero_to_one(fraction: object) -> None:
    budget = {"coreMinutesPerDay": 1440, "shed": {"backfill": fraction}}
    with pytest.raises(delivery.DeliveryError, match="shed"):
        delivery.runtime({**RUNTIME, "budget": budget})


@pytest.mark.parametrize("fraction", [0, 1])
def test_runtime_accepts_shed_bounds(fraction: float) -> None:
    budget = {"coreMinutesPerDay": 1440, "shed": {"backfill": fraction}}
    assert delivery.runtime({**RUNTIME, "budget": budget})["budget"]["shed"] == {"backfill": fraction}


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
    assert delivery.lab(ROOT).check_prefix == "lab"


def test_template_carries_the_lab_block() -> None:
    template = yaml.safe_load((ROOT / "profiles/package/templates/ci/delivery.yaml").read_text())
    assert template["lab"] == block()


def test_module_entry_runs_cutover(tmp_path: Path) -> None:
    from scripts.ci_lab import __main__ as entry

    grader = tree(tmp_path / "grader", block())
    output = tmp_path / "output"
    assert entry.main(["cutover", "--grader-root", str(grader)], {"GITHUB_OUTPUT": str(output)}) == 0
    assert output.read_text() == "skip=[]\n"
