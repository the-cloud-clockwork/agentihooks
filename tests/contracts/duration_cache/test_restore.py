import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci_select import decide
from tests.dev_durations import adopt

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
KEY = "durations-merged-baseline"
STORED = {
    "tests/test_sample.py::test_value": 10.0,
    "tests/test_heavy1.py::test_value": 170.0,
    "tests/test_heavy2.py::test_value": 150.0,
    "tests/test_heavy3.py::test_value": 20.0,
}


def selected_job():
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["unit-selected"]


def restore(folder, failures):
    step = next(step for step in selected_job()["steps"] if step.get("name") == "Restore latest dev durations")
    assert step["if"] == "needs.select.outputs.key != ''"
    inputs = {**step["with"], "key": KEY, "path": str(folder)}
    assert step["with"]["key"] == "${{ needs.select.outputs.key }}"
    if step["uses"].startswith("./"):
        steps = yaml.safe_load((ROOT / step["uses"] / "action.yml").read_text())["runs"]["steps"]
    else:
        steps = [{name: value for name, value in step.items() if name != "if"}]
    outcomes = {}
    calls = []
    folder.mkdir()
    for attempt in steps:
        if condition := attempt.get("if"):
            match = re.fullmatch(r"steps\.(\w+)\.outcome == 'failure'", condition)
            assert match, condition
            if outcomes.get(match[1]) != "failure":
                continue
        assert attempt["uses"] == "actions/cache/restore@v4"
        settings = {
            name: inputs[match[1]]
            if isinstance(value, str) and (match := re.fullmatch(r"\$\{\{ inputs\.([\w-]+) \}\}", value))
            else value
            for name, value in attempt["with"].items()
        }
        if len(steps) == 1:
            settings = inputs
        assert settings == {"key": KEY, "path": str(folder), "fail-on-cache-miss": True}
        calls.append(settings)
        success = len(calls) > failures
        (folder / ".test_durations").write_text(json.dumps(STORED) if success else '{"partial":')
        if success:
            (folder / ".test_durations-3.12").write_text(json.dumps(STORED))
        outcomes[attempt.get("id", "last")] = "success" if success else "failure"
        if not success and not attempt.get("continue-on-error", False):
            return False, calls
    return True, calls


def selection(root, folder):
    for name in ("sample", "heavy1", "heavy2", "heavy3"):
        path = root / f"tests/test_{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("from hooks import sample\n")
    return decide(root, ["hooks/sample.py"], adopt(folder, "3.12"), 3)


@pytest.mark.parametrize("failures", [1, 2])
def test_failed_download_retries_the_same_baseline_and_preserves_selection(tmp_path, failures):
    baseline = tmp_path / "baseline"
    recovered = tmp_path / "recovered"
    assert restore(baseline, 0)[0]
    success, calls = restore(recovered, failures)
    assert success, "A transient duration cache download prevented the selected tests from running"
    assert len(calls) == failures + 1
    assert adopt(recovered, "3.12") == STORED
    assert (
        selection(tmp_path / "before", baseline)
        == selection(tmp_path / "after", recovered)
        == (
            "selected",
            [["tests/test_heavy1.py", "tests/test_sample.py"], ["tests/test_heavy2.py", "tests/test_heavy3.py"]],
        )
    )
    step = next(step for step in selected_job()["steps"] if step.get("name") == "Adopt latest dev durations")
    command = step["run"].replace("python -c", '"$PYTHON" -c').replace("~/dev-durations", '"$RESTORED"')
    subprocess.run(
        ["bash", "-euc", command],
        cwd=tmp_path,
        env={"PYTHON": sys.executable, "PYTHONPATH": str(ROOT), "PYTHON_VERSION": "3.12", "RESTORED": str(recovered)},
        check=True,
    )
    assert json.loads((tmp_path / ".test_durations").read_text()) == STORED


def test_exhausted_downloads_fail_without_using_partial_or_another_baseline(tmp_path):
    success, calls = restore(tmp_path / "failed", 3)
    assert not success
    assert len(calls) == 3
    assert all(call["key"] == KEY for call in calls)
