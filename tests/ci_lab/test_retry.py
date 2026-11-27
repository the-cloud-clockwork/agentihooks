"""Tests for the lab retry policy: one retry for a lost worker, never for a failed test."""

from pathlib import Path

import pytest
import yaml

from scripts.ci_lab import delivery, retry
from scripts.ci_lab.retry import Policy, UnitResult

LAB: dict[str, object] = {
    "authoritative": [],
    "mutation": {"run_on": "every_push", "mutants_per_changed_line": "all", "arid": []},
    "check_prefix": "lab",
}


def tree(root: Path, lab: dict[str, object]) -> Path:
    """Write a repository tree holding an empty workflow folder and a delivery.yaml with the given lab block."""
    (root / ".github/workflows").mkdir(parents=True)
    (root / "delivery.yaml").write_text(yaml.safe_dump({"lab": lab}))
    return root


@pytest.mark.parametrize(
    ("outcome", "attempts", "expected"),
    [
        ("passed", 0, "final"),
        ("passed", 1, "final"),
        ("lost", 0, "retry"),
        ("lost", 1, "final"),
        ("lost", 2, "final"),
        ("crash", 0, "retry"),
        ("crash", 1, "final"),
        ("crash", 2, "final"),
        ("failed", 0, "final"),
        ("failed", 1, "final"),
        ("gate_failed", 0, "final"),
        ("survived", 0, "final"),
        ("unknown", 0, "final"),
    ],
)
def test_decision_table_under_the_default_policy(outcome: str, attempts: int, expected: str) -> None:
    assert retry.decide(UnitResult(outcome, attempts)) == expected


@pytest.mark.parametrize(
    ("outcome", "attempts", "expected"),
    [
        ("passed", 0, "final"),
        ("lost", 1, "retry"),
        ("lost", 2, "final"),
        ("failed", 0, "retry"),
        ("survived", 0, "retry"),
        ("failed", 1, "final"),
    ],
)
def test_decision_table_follows_the_configured_counts(outcome: str, attempts: int, expected: str) -> None:
    assert retry.decide(UnitResult(outcome, attempts), Policy(lost=2, failed=1)) == expected


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
def test_a_second_loss_is_final_and_leaves_its_grade_without_a_verdict() -> None:
    first, second = UnitResult("lost", 0), UnitResult("lost", 1)
    assert retry.decide(first) == "retry"
    assert (retry.decide(second), retry.red(second)) == ("final", False)


@pytest.mark.feature("lab-lost-unit-leaves-gate-neutral")
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("passed", False),
        ("failed", True),
        ("gate_failed", True),
        ("survived", True),
        ("lost", False),
        ("crash", False),
        ("cannot_run", False),
        ("dropped", False),
    ],
)
def test_only_a_result_with_a_failing_verdict_is_red(outcome: str, expected: bool) -> None:
    assert retry.red(UnitResult(outcome, 0)) is expected


def test_policy_defaults_to_one_lost_retry_and_no_failed_retry(tmp_path: Path) -> None:
    assert retry.policy(delivery.lab(tree(tmp_path, LAB))) == Policy(lost=1, failed=0)


@pytest.mark.parametrize(
    ("counts", "expected"),
    [({"lost": 2}, Policy(lost=2, failed=0)), ({"lost": 0, "failed": 0}, Policy(lost=0, failed=0))],
)
def test_policy_reads_lab_retry(tmp_path: Path, counts: dict[str, int], expected: Policy) -> None:
    assert retry.policy(delivery.lab(tree(tmp_path, {**LAB, "retry": counts}))) == expected


@pytest.mark.parametrize(
    "counts",
    [{"lost": -1}, {"failed": -1}, {"lost": 1.5}, {"failed": 0.5}, {"lost": "one"}, {"lost": True}, {"extra": 1}, []],
)
def test_invalid_lab_retry_is_an_error(tmp_path: Path, counts: object) -> None:
    with pytest.raises(delivery.DeliveryError, match="retry"):
        delivery.lab(tree(tmp_path, {**LAB, "retry": counts}))
