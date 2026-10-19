import time
from pathlib import Path

import pytest

from scripts.ci_mutation.runner import mutate_files
from scripts.ci_mutation.stats import SharedStats

# One xdist worker builds the baseline pass once; a module cache behind a function fixture keeps that pass inside
# the function scoped home isolation, which a module fixture would run before.
pytestmark = pytest.mark.xdist_group("remote_control_mutation")
ROOT = Path(__file__).resolve().parents[1]
SOURCE = "scripts/swarm/commands.py"


@pytest.fixture(scope="module")
def baseline_cache():
    return []


@pytest.fixture
def baseline(baseline_cache, tmp_path_factory, monkeypatch):
    if not baseline_cache:
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
        monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        results, error = mutate_files(ROOT, work, selected, time.monotonic() + 120, (0, 1), stats)
        baseline_cache.append((selected, stats, work, results, error))
    return baseline_cache[0]


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
