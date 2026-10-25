import json

import pytest

from scripts import feature_map


@pytest.fixture
def mapped_repo(tmp_path):
    rules = tmp_path / ".semgrep/features"
    rules.mkdir(parents=True)
    (rules / "login.yml").write_text("rules:\n  - id: feature-login\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_login.py").write_text(
        'import pytest\n@pytest.mark.feature("login")\ndef test_shared():\n    assert True\n'
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "live.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    entry = {
        "id": "login",
        "operator_words": "All front doors use the same stored login.",
        "ordered_at": "2026-10-25",
        "ledger_item": "ledger:doctor/tasks/t1",
        "behaviour": "All front doors share the preferred stored login.",
        "semgrep_rule": "feature-login",
        "behaviour_test": "tests/test_login.py::test_shared",
        "live_check": ["bash", "scripts/live.sh"],
    }
    path = tmp_path / "feature-map.json"
    path.write_text(json.dumps({"features": [entry]}))
    return tmp_path, path, entry


def test_complete_feature_map_passes(mapped_repo):
    root, path, _ = mapped_repo
    assert feature_map.validate(root, path) == 1


def test_missing_behaviour_test_fails_with_the_feature_name(mapped_repo):
    root, path, entry = mapped_repo
    entry["behaviour_test"] = "tests/test_login.py::test_missing"
    path.write_text(json.dumps({"features": [entry]}))
    with pytest.raises(ValueError, match="login.*missing behaviour test"):
        feature_map.validate(root, path)
