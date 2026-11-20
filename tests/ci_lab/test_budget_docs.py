import re
from pathlib import Path

import pytest
import yaml

from scripts.ci_lab import budget
from scripts.ci_lab.delivery import SHEDDABLE, runtime

DOC = Path(__file__).resolve().parents[2] / "docs" / "reference" / "ci-lab.md"
SECTION = "### Runtime configuration"
LEFT = {"half": 0.5, "a quarter": 0.25, "a tenth": 0.1}
POINT = re.compile(rf"\b({'|'.join(SHEDDABLE)})(?: is shed)? at ({'|'.join(LEFT)})\b")
RULE = re.compile(r"Each threshold is [^.]*\.")
ALERT = re.compile(r"\| `CILabBudgetShedding` \| ([^|]*) \|")


def check(text: str) -> None:
    _, heading, section = text.partition(SECTION)
    flat = " ".join(section.split())
    config = re.search(r"```yaml\n(.*?)```", section, re.DOTALL)
    prose = re.search(r"In this example, ([^;.]*)", flat)
    rule = RULE.search(flat)
    alert = ALERT.search(" ".join(text.split()))
    assert heading and config and prose and rule and alert
    policy = budget.policy(runtime(yaml.safe_load(config[1])))
    points = [(name, LEFT[word]) for name, word in POINT.findall(prose[1])]
    assert [name for name, _ in points] == list(SHEDDABLE)
    for index, (name, left) in enumerate(points):
        share = policy.fractions[name]
        status = budget.assess(share * policy.core_minutes_per_day, policy)
        assert share == pytest.approx(1 - left), f"{name} sheds at {share} spent, the prose at {left} left"
        assert status.fraction == pytest.approx(left)
        assert status.shed == SHEDDABLE[: index + 1]
    for sentence in (rule[0], alert[1]):
        assert re.search(r"\bspent\b.*\breach", sentence), sentence
        assert not re.search(r"\b(?:left|remaining)\b", sentence), sentence


@pytest.mark.feature("lab-budget-docs")
def test_the_published_lab_budget_docs_match_the_shed_thresholds_in_code() -> None:
    check(DOC.read_text())


@pytest.mark.feature("lab-budget-docs")
@pytest.mark.parametrize(
    ("published", "planted"),
    [
        ("promotion: 0.75\n    queue: 0.9", "promotion: 0.25\n    queue: 0.1"),
        ("backfill: 0.5\n    promotion: 0.75\n    queue: 0.9", "backfill: 0.9\n    promotion: 0.75\n    queue: 0.5"),
        ("budget spent; reaching it", "budget left; reaching it"),
        (
            "the share of the day's core budget spent reached that class's shed fraction",
            "the day's core budget left fell below that class's shed fraction",
        ),
    ],
    ids=["remaining_shares", "swapped_classes", "rule_reads_the_budget_left", "alert_reads_the_budget_left"],
)
def test_a_docs_copy_with_the_thresholds_swapped_fails_the_check(published: str, planted: str) -> None:
    text = DOC.read_text()
    assert published in text
    with pytest.raises(AssertionError):
        check(text.replace(published, planted))
