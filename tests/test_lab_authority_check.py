"""The live check of the lab authoritative static gates over this checkout and over damaged copies of it."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci_lab import delivery, static_exclusions, static_gates
from scripts.feature_checks import lab_authority as check

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
HELD = [
    "the checkout holds exactly lint, type-gate and test-count in the lab authoritative list",
    *(
        line
        for gate in check.GATES
        for line in (
            f"the checkout holds lab authority for {gate}",
            f"the checkout holds the lab check that replaces the {gate} job",
            f"the checkout holds the lab skip clause of the {gate} job",
        )
    ),
    f"the checkout holds {check.COMMIT_ID_PART}",
]
LINT_CLAUSE = check.CLAUSE.format(gate="lint")
TRUST_STEP = "Require trusted actors before private runner admission"


def _trust_step(document: Any) -> dict[str, Any]:
    return next(step for step in document["jobs"]["lint"]["steps"] if step.get("name") == TRUST_STEP)


DAMAGE: list[tuple[str, Callable[[Any], object], set[int]]] = [
    (delivery.DELIVERY, lambda document: document["lab"]["authoritative"].remove("test-count"), {0, 7}),
    (delivery.DELIVERY, lambda document: document["lab"]["authoritative"].append("docs"), {0}),
    (
        static_exclusions.PATH,
        lambda document: document.update({"lint/lint-check": {"needs": ["network"], "reason": "planted"}}),
        {1},
    ),
    (check.WIRING, lambda document: document["lab_gates"].pop("lab/type-gate"), {5}),
    (check.WORKFLOW, lambda document: document["jobs"]["test-count"].update({"if": "${{ !cancelled() }}"}), {9}),
    (check.WORKFLOW, lambda document: document["jobs"]["lint"]["needs"].remove("lab-verdict"), {3}),
    (check.WORKFLOW, lambda document: document["jobs"]["lint"].pop("needs"), {3}),
    (delivery.DELIVERY, lambda document: document["lab"]["authoritative"].reverse(), {0}),
    (
        check.WORKFLOW,
        lambda document: document["jobs"]["lint"].update({"if": "${{ !cancelled() || " + LINT_CLAUSE + " }}"}),
        {3},
    ),
    (
        check.WORKFLOW,
        lambda document: document["jobs"]["lint"].update(
            {"if": "${{ always() || !cancelled() && " + LINT_CLAUSE + " }}"}
        ),
        {3},
    ),
    (
        check.WORKFLOW,
        lambda document: (
            _trust_step(document).setdefault("env", {}).update({"OTHER": "${{ github.event.workflow_run.head_sha }}"})
        ),
        {10},
    ),
]


def _lacking(lost: set[int]) -> list[str]:
    return [
        line.replace("the checkout holds", "FAIL: the checkout lacks") if index in lost else line
        for index, line in enumerate(HELD)
    ]


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    for path in (delivery.DELIVERY, static_exclusions.PATH, check.WIRING):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, tmp_path / path)
    shutil.copytree(ROOT / delivery.WORKFLOWS, tmp_path / delivery.WORKFLOWS)
    return tmp_path


def _edit(path: Path, change: Callable[[Any], object]) -> None:
    document = yaml.safe_load(path.read_text())
    change(document)
    path.write_text(json.dumps(document) if path.suffix == ".json" else yaml.safe_dump(document))


@pytest.mark.feature("lab-authoritative-static-gates")
def test_the_checkout_holds_every_part_of_the_lab_authority_and_each_is_listed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check.ROOT == ROOT
    assert check.main() == 0
    assert capsys.readouterr().out.splitlines() == HELD


@pytest.mark.feature("lab-authoritative-static-gates")
@pytest.mark.parametrize(
    ("path", "change", "lost"),
    DAMAGE,
    ids=[
        "a listed gate removed",
        "a fourth gate listed",
        "a lint step excluded",
        "a lab check undeclared",
        "a skip clause dropped",
        "a lab verdict need dropped",
        "a job with the skip clause and no needs",
        "the list reordered",
        "a skip clause joined with or",
        "an or beside the terms that hold the skip clause",
        "a commit id read outside the static key",
    ],
)
def test_a_part_the_checkout_lost_fails_by_name(
    copy: Path, capsys: pytest.CaptureFixture[str], path: str, change: Callable[[Any], object], lost: set[int]
) -> None:
    _edit(copy / path, change)
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == _lacking(lost)


@pytest.mark.feature("lab-authoritative-static-gates")
def test_a_delisted_gate_whose_job_is_gone_fails_by_name_on_the_list_its_authority_and_its_skip_clause(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit(copy / delivery.DELIVERY, lambda document: document["lab"]["authoritative"].remove("type-gate"))
    _edit(copy / check.WORKFLOW, lambda document: document["jobs"].pop("type-gate"))
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == _lacking({0, 4, 6})


@pytest.mark.feature("lab-authoritative-static-gates")
def test_a_listed_gate_whose_job_is_gone_fails_on_the_delivery_file_and_never_raises(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit(copy / check.WORKFLOW, lambda document: document["jobs"].pop("type-gate"))
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == [
        "FAIL: the checkout lacks a delivery file its reader accepts: lab.authoritative names unknown gates: type-gate"
    ]


@pytest.mark.feature("lab-authoritative-static-gates")
def test_a_checkout_with_no_wiring_file_raises_and_is_never_reported_as_a_delivery_fault(copy: Path) -> None:
    (copy / check.WIRING).unlink()
    with pytest.raises(FileNotFoundError):
        check.main(copy)


@pytest.mark.feature("lab-authoritative-static-gates")
def test_a_checkout_with_a_malformed_wiring_file_raises_and_is_never_reported_as_a_delivery_fault(copy: Path) -> None:
    (copy / check.WIRING).write_text("{")
    with pytest.raises(json.JSONDecodeError):
        check.main(copy)


@pytest.mark.feature("lab-authoritative-static-gates")
def test_a_job_whose_whole_condition_is_the_skip_clause_holds_it(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit(copy / check.WORKFLOW, lambda document: document["jobs"]["lint"].update({"if": "${{ " + LINT_CLAUSE + " }}"}))
    assert check.main(copy) == 0
    assert capsys.readouterr().out.splitlines() == HELD


@pytest.mark.feature("lab-authoritative-static-gates")
def test_a_step_reading_an_earlier_steps_output_shaped_like_a_commit_id_holds_the_part(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit(
        copy / check.WORKFLOW,
        lambda document: (
            _trust_step(document).setdefault("env", {}).update({"OTHER": "${{ steps.earlier.outputs.release_sha }}"})
        ),
    )
    assert check.main(copy) == 0
    assert capsys.readouterr().out.splitlines() == HELD


@pytest.mark.feature("lab-authoritative-static-gates")
def test_a_step_reading_a_context_name_that_is_not_a_commit_id_holds_the_part(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit(
        copy / check.WORKFLOW,
        lambda document: _trust_step(document).setdefault("env", {}).update({"OTHER": "${{ github.event.number }}"}),
    )
    assert check.main(copy) == 0
    assert capsys.readouterr().out.splitlines() == HELD


@pytest.mark.feature("lab-authoritative-static-gates")
@pytest.mark.parametrize("registry", [None, "{"], ids=["absent", "malformed"])
def test_the_check_holds_whatever_the_input_key_registry_file_is_since_it_never_reads_it(
    copy: Path, capsys: pytest.CaptureFixture[str], registry: str | None
) -> None:
    if registry is not None:
        (copy / static_gates.REGISTRY).write_text(registry)
    assert (copy / static_gates.REGISTRY).exists() == (registry is not None)
    assert check.main(copy) == 0
    assert capsys.readouterr().out.splitlines() == HELD


@pytest.mark.feature("lab-authoritative-static-gates")
def test_a_job_of_a_workflow_a_listed_gate_calls_reading_a_commit_id_outside_the_key_fails_the_part(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    step = {"run": "python -m scripts.gate_protected", "env": {"OTHER": "${{ github.event.workflow_run.head_sha }}"}}
    called = {"on": "workflow_call", "jobs": {"checks": {"runs-on": "ubuntu-latest", "steps": [step]}}}
    (copy / delivery.WORKFLOWS / "lint-called.yml").write_text(yaml.safe_dump(called))

    def call(document: Any) -> None:
        document["jobs"]["lint"].pop("steps")
        document["jobs"]["lint"]["uses"] = "./.github/workflows/lint-called.yml"

    _edit(copy / check.WORKFLOW, call)
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == _lacking({10})


@pytest.mark.feature("lab-authoritative-static-gates")
def test_a_step_whose_run_text_reads_github_sha_in_the_shell_fails_the_part(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def plant(document: Any) -> None:
        step = _trust_step(document)
        step["run"] = step["run"] + '\necho "$GITHUB_SHA"\n'

    _edit(copy / check.WORKFLOW, plant)
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == _lacking({10})


@pytest.mark.feature("lab-authoritative-static-gates")
def test_an_earlier_steps_run_text_that_writes_git_rev_parse_to_its_output_fails_the_part(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def plant(document: Any) -> None:
        steps = document["jobs"]["lint"]["steps"]
        trust = _trust_step(document)
        steps.insert(
            steps.index(trust),
            {
                "name": "Record the head sha",
                "id": "record",
                "run": 'echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"',
            },
        )
        trust.setdefault("env", {})["OTHER"] = "${{ steps.record.outputs.sha }}"

    _edit(copy / check.WORKFLOW, plant)
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == _lacking({10})
