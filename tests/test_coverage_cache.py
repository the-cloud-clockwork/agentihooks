import argparse

import pytest
from coverage import CoverageData

from tests import coverage_baseline as cache
from tests import coverage_ratchet as ratchet


@pytest.fixture
def measured(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_git", lambda repo, revision: "tree" if revision.endswith("^{tree}") else "base")
    monkeypatch.setattr(ratchet, "git", lambda *args, **kwargs: "tree" if args[-1].endswith("^{tree}") else "base")
    monkeypatch.setattr("tests.coverage_history._show", lambda repo, commit: lambda path: "one\ntwo\nthree\n")
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({"hooks/example.py": {1, 2}})
    data.write()
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/example.py").write_text("one\ntwo\nthree\n")
    target = tmp_path / "baseline.json"
    cache.record(tmp_path, [tmp_path / ".coverage"], target)
    return tmp_path, target


def test_cached_base_uses_the_requested_commit_with_the_same_tree(measured):
    root, target = measured
    base = next(ratchet._cached_runs(root, "squash", target))
    assert base.commit == "squash"
    assert base.executed == {"hooks/example.py": {1, 2}}
    assert base.source("hooks/example.py") == "one\ntwo\nthree\n"


@pytest.mark.parametrize("covered,lost", [({1, 2}, {}), ({1}, {"hooks/example.py": [2]})])
def test_cached_grading_does_not_read_github(measured, monkeypatch, covered, lost):
    root, target = measured
    shard = root / ".coverage"
    data = CoverageData(basename=str(shard))
    data.add_lines({"hooks/example.py": covered})
    data.write()
    monkeypatch.setattr(ratchet, "renamed", lambda *args: {})
    monkeypatch.setattr(ratchet, "dev_runs", lambda *args: pytest.fail("GitHub history read"))
    args = argparse.Namespace(head=root, base="base", shards=1, base_baseline=target)
    _, _, result = ratchet._grade(args, [shard], root)
    assert result.lost == lost


def test_a_head_split_another_way_grades_against_the_cached_base(measured, monkeypatch):
    root, target = measured
    shards = []
    for index, covered in enumerate([{1}, {2}], start=1):
        data = CoverageData(basename=str(root / f"shard-{index}.coverage"))
        data.add_lines({"hooks/example.py": covered})
        data.write()
        shards.append(root / f"shard-{index}.coverage")
    monkeypatch.setattr(ratchet, "renamed", lambda *args: {})
    args = argparse.Namespace(head=root, base="base", shards=len(shards), base_baseline=target)
    _, _, result = ratchet._grade(args, shards, root)
    assert result.lost == {}


def test_the_ratchet_refuses_a_cached_base_from_another_tree(measured, monkeypatch):
    root, target = measured
    monkeypatch.setattr(ratchet, "git", lambda *args, **kwargs: "another-tree")
    with pytest.raises(ValueError, match="tree differs"):
        next(ratchet._cached_runs(root, "base", target))


def test_cache_miss_fails_the_cli_with_recovery_guidance(measured, capsys):
    root, _ = measured
    folder = root / "coverage-3.12-1"
    folder.mkdir()
    data = CoverageData(basename=str(folder / ".coverage"))
    data.add_lines({"hooks/example.py": {1, 2}})
    data.write()
    result = ratchet.main(
        [
            "--head",
            str(root),
            "--base",
            "base",
            "--shards",
            "1",
            "--head-shards",
            str(root),
            "--out",
            str(root / "report"),
            "--base-baseline",
            str(root / "missing.json"),
        ]
    )
    assert result == 1
    assert "failed dev run" in capsys.readouterr().out
    assert not (root / "report/report.txt").exists()


def test_queue_grading_consumes_the_restored_exact_base():
    from pathlib import Path

    import yaml

    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())
    job = workflow["jobs"]["coverage-ratchet"]
    assert "queue-baseline" not in workflow["jobs"]
    assert "queue-baseline" not in job["needs"]
    assert "!cancelled()" in job["if"]
    steps = {step.get("name"): step for step in job["steps"]}
    base = steps["Resolve the measured base tree"]
    assert base["env"]["QUEUE_BASE"] == "${{ github.event.merge_group.base_sha }}"
    assert "git merge-base" not in base["run"]
    restore = steps["Restore the exact coverage baseline"]
    assert restore["if"] == "github.event_name != 'merge_group' && github.event_name != 'workflow_dispatch'"
    download = steps["Download the queue base coverage baseline"]
    assert download["if"] == "github.event_name == 'workflow_dispatch'"
    assert download["with"]["name"] == "queue-coverage-baseline"
    assert download["with"]["path"] == "~/coverage-baseline"
    assert "git merge-base" not in steps["Hold every line the base ran"]["run"]
