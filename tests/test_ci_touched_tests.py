import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(("selected", "maximum", "workers"), [(4, 8, 4), (10, 2, 2), (1, 8, 1), (None, 8, None)])
def test_touched_test_workers_follow_the_collected_selection(tmp_path, selected, maximum, workers):
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/mutation-preflight.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["touched-tests"]["steps"]
        if step.get("name") == "Run the tests the branch touches"
    )
    suite = tmp_path / "test_selection.py"
    suite.write_text(
        "invalid python!\n"
        if selected is None
        else f"import pytest\n@pytest.mark.parametrize('case', range({selected}))\ndef test_case(case):\n    pass\n"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=root,
        env={**os.environ, "FILES": str(suite), "PYTEST_ADDOPTS": "", "PYTEST_XDIST_AUTO_NUM_WORKERS": str(maximum)},
        capture_output=True,
        text=True,
        timeout=90,
    )
    output = result.stdout + result.stderr
    if selected is None:
        assert result.returncode != 0, output
        assert "SyntaxError" in output
        assert "created:" not in output
    else:
        assert result.returncode == 0, output
        started = re.search(r"created: (\d+)/", output)
        assert started is not None, output
        assert int(started[1]) == workers, output
        assert f"{selected} passed" in output
