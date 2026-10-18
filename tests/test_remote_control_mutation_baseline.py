import time
from pathlib import Path

from scripts.ci_mutation.runner import mutate_files
from scripts.ci_mutation.stats import SharedStats


def test_remote_control_statistics_baseline_reaches_mutant_grading(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    root = Path(__file__).resolve().parents[1]
    source = "scripts/swarm/commands.py"
    line = next(
        number
        for number, text in enumerate((root / source).read_text().splitlines(), 1)
        if "order = store.redis.lrange" in text
    )
    selected = {source: ({line}, ["tests/swarm_ledger/test_remote_control_process.py"])}
    stats = SharedStats(tmp_path / "stats", "remote-control", (0, 1))
    stats.folder.mkdir()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    results, error = mutate_files(root, baseline, selected, time.monotonic() + 120, (0, 1), stats)
    assert error == "", (baseline / "run.log").read_text()
    assert results == {source: []}

    grading = tmp_path / "grading"
    grading.mkdir()
    results, error = mutate_files(
        root, grading, selected, time.monotonic() + 120, (0, 1), SharedStats(stats.folder, stats.head)
    )
    assert error == "", (grading / "run.log").read_text()
    assert results[source]
    assert any(row["status"] == "killed" for row in results[source]), results
