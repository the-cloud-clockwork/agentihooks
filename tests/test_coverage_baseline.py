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
    assert cache.read(target, "tree") == {
        "commit": "base",
        "tree": "tree",
        "shards": 1,
        "executed": {"hooks/example.py": [1, 2]},
        "history": [],
    }


def test_a_cache_miss_names_a_failed_dev_run_and_the_recovery(measured):
    root, _, _ = measured
    with pytest.raises(ValueError, match="failed dev run.*merge a measured dev revision.*rerunning"):
        cache.read(root / "missing.json", "tree")


def test_a_cache_for_another_tree_is_red(measured):
    root, _, files = measured
    target = root / "baseline.json"
    cache.record(root, files, target)
    with pytest.raises(ValueError, match="tree"):
        cache.read(target, "another")


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


@pytest.mark.parametrize("folders,code", [(2, 0), (3, 1)], ids=["exact", "extra-shard"])
def test_the_recorder_reads_exactly_the_shards_the_run_split(measured, folders, code):
    root, _, _ = measured
    for index in range(1, folders + 1):
        folder = root / "downloaded" / f"coverage-3.12-{index}"
        folder.mkdir(parents=True)
        data = CoverageData(basename=str(folder / ".coverage"))
        data.add_lines({"hooks/example.py": {index}})
        data.write()
    target = root / "baseline.json"
    argv = ["--head", str(root), "--shards", "2", "--head-shards", str(root / "downloaded"), "--out", str(target)]
    assert cache.main(argv) == code
    assert target.exists() == (code == 0)


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
    assert job["needs"] == ["unit", "gate-required"]
    assert job["if"] == "${{ !cancelled() && github.event_name == 'push' && needs.unit.result == 'success' }}"


def test_publisher_reads_shards_without_the_optional_sonar_report():
    workflow = Path(__file__).parents[1] / ".github/workflows/test.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["coverage-baseline"]
    steps = {step.get("name"): step for step in job["steps"]}
    download = steps["Download this run's measured coverage"]["with"]
    assert download["pattern"] == "coverage-3.12-*"
    assert "name" not in download
    assert f"--head-shards {download['path']} " in steps["Compute the coverage baseline"]["run"]
