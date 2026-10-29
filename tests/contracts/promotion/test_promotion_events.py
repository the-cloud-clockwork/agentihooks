from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("base,expected", [("dev", True), ("main", False)])
def test_pull_request_grading_runs_only_for_integration(base, expected):
    root = Path(__file__).resolve().parents[3]
    triggers = yaml.safe_load((root / ".github/workflows/test.yml").read_text())[True]
    assert (base in triggers["pull_request"]["branches"]) is expected
