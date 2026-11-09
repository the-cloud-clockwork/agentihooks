import hashlib
import json
from pathlib import Path

import pytest


@pytest.mark.feature("gate-admin-recovery-payload")
def test_the_frozen_registry_accepts_the_admin_pull_request_recovery_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    candidate = (root / "tests/registry/dev-history-admin.json").read_bytes()
    payload = json.loads(candidate)
    assert payload["bypass_actors"] == [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}]
    current = json.loads((root / ".github/rulesets/dev-history.json").read_bytes())
    assert {key: value for key, value in payload.items() if key != "bypass_actors"} == {
        key: value for key, value in current.items() if key != "bypass_actors"
    }
    blob = hashlib.sha1(f"blob {len(candidate)}\0".encode() + candidate).hexdigest()
    registry = json.loads((root / "tests/registry/FROZEN_GATES.json").read_bytes())
    assert blob in registry[".github/rulesets/dev-history.json"]
