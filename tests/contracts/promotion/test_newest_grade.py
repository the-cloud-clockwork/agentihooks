from pathlib import Path

import pytest
import yaml

from tests.contracts.ci_tiers.test_workflow import enabled
from tests.contracts.promotion.test_workflow import _graded_needs

ROOT = Path(__file__).resolve().parents[3]


def test_readiness_certifies_the_same_grade_before_serialized_promotion():
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    ready = jobs["promotion-ready"]
    promote = jobs["promote"]
    assert ready["name"] == "Promotion ready"
    assert set(ready["needs"]) == set(promote["needs"]) - {"promotion-ready"}
    assert "concurrency" not in ready
    assert enabled(ready["if"], "push", _graded_needs(ready))
    assert not enabled(ready["if"], "push", _graded_needs(ready, analysis=""))
    for event in ("pull_request", "merge_group", "workflow_dispatch"):
        assert not enabled(ready["if"], event, _graded_needs(ready))
    for name in ready["needs"]:
        needs = _graded_needs(ready)
        needs[name].result = "failure"
        assert not enabled(ready["if"], "push", needs)


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_release_requires_a_successful_readiness_marker(result):
    promote = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["promote"]
    needs = _graded_needs(promote)
    needs["promotion-ready"].result = result
    assert not enabled(promote["if"], "push", needs)
