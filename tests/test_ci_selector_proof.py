import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.xdist_group("fakeredis")
ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/selector-proof.yml").read_text())


def test_selector_proof_is_unprivileged_and_restricted_to_plants():
    proof = workflow()
    assert set(proof[True]) == {"workflow_dispatch", "push"}
    assert proof[True]["push"]["branches"] == ["diffcheck/selector-*"]
    assert proof["permissions"] == {"contents": "read"}
    job = proof["jobs"]["selection"]
    assert "'anton-oss-light'" in job["runs-on"]
    assert job["runs-on"].endswith("|| 'ubuntu-latest' }}")
    assert "Gate — Required" not in json.dumps(proof)
    assert "secrets." not in json.dumps(proof)
    checkouts = [step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")]
    assert len(checkouts) == 2
    assert all(step["with"]["persist-credentials"] is False for step in checkouts)
    steps = {step.get("name"): step for step in job["steps"]}
    assert "refs/heads/diffcheck/selector-" in steps["Require a plant branch"]["run"]
    for side in ("base", "head"):
        step = steps[f"Run {side} selector"]
        assert step["working-directory"] == side
        assert "scripts.ci_select" in step["run"]
        assert '--root "$GITHUB_WORKSPACE/head"' in step["run"]
        assert '--base "$CHANGE_BASE" --head "$PLANT_HEAD"' in step["run"]


@pytest.mark.parametrize(
    ("base", "head", "success"),
    [
        ("scope=none\n", "scope=selected\nfiles_1=tests/test_ci_select.py\n", True),
        ("scope=none\n", "scope=none\n", False),
        ("scope=full\n", "scope=selected\nfiles_1=tests/test_ci_select.py\n", False),
        ("scope=selected\nfiles_1=tests/test_ci_select.py\n", "scope=full\n", False),
        ("scope=none\n", "scope=full\n", True),
        ("scope=none\n", "scope=selected\nfiles_1=tests/test_other.py\n", False),
        ("scope=none\n", "", False),
    ],
)
def test_selector_proof_reports_both_selections_and_requires_a_base_miss(tmp_path, base, head, success):
    (tmp_path / "base.txt").write_text(base)
    (tmp_path / "head.txt").write_text(head)
    (tmp_path / "metadata.json").write_text(
        json.dumps({"plant": "Named plant", "base": "base-sha", "head": "head-sha"})
    )
    step = next(step for step in workflow()["jobs"]["selection"]["steps"] if step.get("name") == "Compare selections")
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "EXPECTED_TESTS": "tests/test_ci_select.py",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        text=True,
        capture_output=True,
    )
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    report = json.loads((tmp_path / "selection-proof.json").read_text())
    assert report["plant"] == "Named plant"
    assert report["selections"]["base"]["raw"] == base
    assert report["selections"]["head"]["raw"] == head
    assert "base-sha" in (tmp_path / "summary").read_text()
    assert "head-sha" in (tmp_path / "summary").read_text()
