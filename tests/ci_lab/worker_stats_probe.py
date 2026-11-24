import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci_mutation.runner import prepare_workspace


@pytest.mark.feature("lab-worker-sink-backlog")
def test_forty_uploads_collect_mutation_stats(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    path = "scripts/ci_lab/worker.py"
    node = "tests/ci_lab/test_worker.py::test_every_slot_runs_a_unit_at_once"
    work = tmp_path / "worker-stats"
    work.mkdir()
    prepare_workspace(root, work, [path], [node])
    selection = work / "selection.json"
    selection.write_text(
        json.dumps({path: {"lines": list(range(1, len((root / path).read_text().splitlines()) + 1)), "tests": [node]}})
    )
    output = work / "stats.json"
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MUTATION_STATS_CACHE", "MUTATION_STATS_ROOT", "MUTANT_UNDER_TEST"}
    }
    child_env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.ci_mutation.selection",
            str(selection),
            "0",
            "1",
            "collect",
            str(output),
            "worker-sink-stats",
            "0",
            "1",
        ],
        cwd=work,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600,
        check=False,
    )
    print(done.stdout)
    assert done.returncode == 0, done.stdout
    assert "ConnectionResetError" not in done.stdout
    assert re.search(r"\b\d+ rerun", done.stdout) is None, done.stdout
    report = json.loads(output.read_text())
    assert report["part"] == 0
    assert report["parts"] == 1
    assert len(report["results"]) == 1
    result = report["results"][0]
    assert result["status"] == 0
    assert result["files"] == [node]
    assert {name.partition("@")[0] for name in result["durations"]} == {node}
    assert any(result["tests"].values())
    print(json.dumps({"node": node, "status": result["status"], "stats_runs": len(report["results"])}))
