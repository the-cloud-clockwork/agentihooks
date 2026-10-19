import time
from pathlib import Path

import pytest

from scripts.ci_mutation.runner import mutate_files
from scripts.ci_mutation.stats import SharedStats

# Each mutmut pass takes about fifteen seconds, so the baseline and grading passes run as two tests on one worker.
pytestmark = pytest.mark.xdist_group("remote_control_mutation")
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "scripts/swarm/commands.py"


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    line = next(
        number
        for number, text in enumerate((ROOT / SOURCE).read_text().splitlines(), 1)
        if "order = store.redis.lrange" in text
    )
    selected = {SOURCE: ({line}, ["tests/swarm_ledger/test_remote_control_process.py"])}
    folder = tmp_path_factory.mktemp("remote-control")
    stats = SharedStats(folder / "stats", "remote-control", (0, 1))
    stats.folder.mkdir()
    work = folder / "baseline"
    work.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        results, error = mutate_files(ROOT, work, selected, time.monotonic() + 120, (0, 1), stats)
    return selected, stats, work, results, error


def test_remote_control_statistics_baseline_collects_without_grading(baseline):
    _, _, work, results, error = baseline
    assert error == "", (work / "run.log").read_text()
    assert results == {SOURCE: []}


def test_remote_control_statistics_baseline_feeds_mutant_grading(baseline, tmp_path, monkeypatch):
    selected, stats, work, _, error = baseline
    assert error == "", (work / "run.log").read_text()
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    grading = tmp_path / "grading"
    grading.mkdir()
    results, error = mutate_files(
        ROOT, grading, selected, time.monotonic() + 120, (0, 1), SharedStats(stats.folder, stats.head)
    )
    assert error == "", (grading / "run.log").read_text()
    assert results[SOURCE]
    assert any(row["status"] == "killed" for row in results[SOURCE]), results
