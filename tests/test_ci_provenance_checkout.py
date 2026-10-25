from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_protected_provenance_checkout_bounds_stalled_fetches():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    checkout = workflow["jobs"]["select"]["steps"][0]
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["env"] == {"GIT_HTTP_LOW_SPEED_LIMIT": "1000", "GIT_HTTP_LOW_SPEED_TIME": "60"}
    assert checkout["with"] == {
        "ref": "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha }}",
        "path": "provenance-grader",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    assert checkout["if"] == "github.event_name == 'merge_group' || github.event_name == 'pull_request'"
    assert not checkout.get("continue-on-error")
