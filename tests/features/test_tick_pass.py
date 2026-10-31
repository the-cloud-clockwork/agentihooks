from pathlib import Path

import pytest
import yaml


@pytest.mark.feature
def test_the_real_backend_feature_suite_holds_both_promotion_jobs() -> None:
    root = Path(__file__).resolve().parents[2]
    jobs = yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]
    suite = jobs["feature-suite"]
    assert suite["uses"] == "./.github/workflows/feature-suite.yml"
    assert "github.event_name == 'push'" in suite["if"]
    assert "github.ref == 'refs/heads/dev'" in suite["if"]
    assert not suite.get("needs")
    for name in ("promotion-ready", "promote"):
        assert "feature-suite" in jobs[name]["needs"]
        assert "needs.feature-suite.result == 'success'" in jobs[name]["if"]
