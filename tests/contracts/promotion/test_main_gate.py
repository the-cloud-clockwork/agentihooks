import json
from pathlib import Path


def test_main_requires_the_promotion_apps_snapshot_grade():
    root = Path(__file__).resolve().parents[3]
    policy = json.loads((root / ".github/rulesets/main-prod-lockdown.json").read_text())
    required = [rule for rule in policy["rules"] if rule["type"] == "required_status_checks"]
    assert required == [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": False,
                "do_not_enforce_on_create": False,
                "required_status_checks": [{"context": "Gate — Required", "integration_id": 5243008}],
            },
        }
    ]
    assert policy["bypass_actors"] == []
