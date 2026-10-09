from pathlib import Path

import pytest
import yaml
from coverage import CoverageData

from tests import coverage_baseline as cache


@pytest.fixture
def measured(tmp_path, monkeypatch):
    state = {"commit": "base", "tree": "tree"}
    monkeypatch.setattr(
        cache, "_git", lambda repo, revision: state["tree"] if revision.endswith("^{tree}") else state["commit"]
    )
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({"hooks/example.py": {1, 2}})
    data.write()
    return tmp_path, state, [tmp_path / ".coverage"]


def test_record_computes_the_baseline_from_coverage(measured):
    root, _, shards = measured
    target = root / "baseline.json"
    cache.record(root, shards, target)
    assert cache.read(target, "tree", 1) == {
        "commit": "base",
        "tree": "tree",
        "shards": 1,
        "executed": {"hooks/example.py": [1, 2]},
        "history": [],
    }


def test_a_cache_miss_names_a_failed_dev_run_and_the_recovery(measured):
    root, _, _ = measured
    with pytest.raises(ValueError, match="failed dev run.*merge a measured dev revision.*rerunning"):
        cache.read(root / "missing.json", "tree", 1)


@pytest.mark.parametrize("tree,shards,message", [("another", 1, "tree"), ("tree", 8, "shards")])
def test_a_cache_for_another_measurement_is_red(measured, tree, shards, message):
    root, _, files = measured
    target = root / "baseline.json"
    cache.record(root, files, target)
    with pytest.raises(ValueError, match=message):
        cache.read(target, tree, shards)


def test_record_retains_the_previous_measurement_and_its_history(measured):
    root, state, shards = measured
    target = root / "baseline.json"
    cache.record(root, shards, target)
    state.update(commit="next", tree="next-tree")
    cache.record(root, shards, target, target)
    value = cache.read(target)
    assert value["commit"] == "next"
    assert [item["commit"] for item in value["history"]] == ["base"]


def test_an_incomplete_suite_cannot_publish_a_baseline(measured):
    root, _, shards = measured
    target = root / "baseline.json"
    with pytest.raises(ValueError, match="shard coverage missing"):
        cache.record(root, [*shards, root / "missing.coverage"], target)
    assert not target.exists()


def test_history_is_bounded_to_thirty_measurements(measured):
    root, state, shards = measured
    target = root / "baseline.json"
    for index in range(35):
        state.update(commit=str(index), tree=str(index))
        cache.record(root, shards, target, target)
    assert [item["commit"] for item in cache.read(target)["history"]] == [str(index) for index in range(33, 3, -1)]


def test_publisher_checks_parent_results_without_inheriting_skips():
    workflow = Path(__file__).parents[1] / ".github/workflows/test.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["coverage-baseline"]
    assert job["needs"] == ["gate-required", "sonar"]
    condition = job["if"]
    assert "!cancelled()" in condition
    assert "github.event_name == 'push'" in condition
    assert "needs['gate-required'].result == 'success'" in condition
    assert "needs.sonar.result == 'success'" in condition


def test_publisher_reads_shards_without_the_optional_sonar_report():
    workflow = Path(__file__).parents[1] / ".github/workflows/test.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["coverage-baseline"]
    steps = {step.get("name"): step for step in job["steps"]}
    download = steps["Download this run's measured coverage"]["with"]
    assert download["pattern"] == "coverage-3.12-*"
    assert "name" not in download
    assert f"--head-shards {download['path']} " in steps["Compute the coverage baseline"]["run"]
