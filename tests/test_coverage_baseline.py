import json
from pathlib import Path

import pytest
import yaml
from coverage import CoverageData

from tests import coverage_baseline as cache
from tests import coverage_summary
from tests.coverage_grade import measured_lines

Measured = tuple[Path, dict[str, str], list[Path]]


def _summarize(summary: Path, lines: dict[str, set[int]]) -> None:
    """Write the executed lines summary a unit shard uploads when its own database ran these lines."""
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps({cache.OWN_DATABASE: {path: sorted(ran) for path, ran in lines.items()}}))


@pytest.fixture
def measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Measured:
    state = {"commit": "base", "tree": "tree"}
    monkeypatch.setattr(
        cache, "_git", lambda repo, revision: state["tree"] if revision.endswith("^{tree}") else state["commit"]
    )
    source = tmp_path / "hooks/example.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text("value = 1\nvalue += 1\n")
    summary = tmp_path / "executed-lines.json"
    _summarize(summary, {"hooks/example.py": {1, 2}})
    return tmp_path, state, [summary]


@pytest.mark.parametrize(("invalid", "dropped"), [(0, True), (4, False), (5, False), (6, True), (99, True)])
def test_impossible_lines_cannot_replace_a_baseline(
    measured: Measured, invalid: int, dropped: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    root, state, shards = measured
    source = root / "hooks/example.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text('value = 1\nscript = """\nconst example = 1;\nconst other = 2;\n"""\n# comment\nvalue += 1\n')
    _summarize(shards[0], {"hooks/example.py": {1, 2, 7}})
    target = root / "baseline.json"
    cache.record(root, shards, target)
    state.update(commit="next", tree="next-tree")
    _summarize(shards[0], {"hooks/example.py": {1, 2, 7, invalid}})
    cache.record(root, shards, target, target)
    assert (f"hooks/example.py:{invalid}" in capsys.readouterr().out) is dropped
    value = cache.read(target)
    assert value["executed"] == {"hooks/example.py": [1, 2, 7]}
    assert value["history"][0]["executed"] == {"hooks/example.py": [1, 2, 7]}


def test_publication_reports_every_impossible_line(measured: Measured, capsys: pytest.CaptureFixture[str]) -> None:
    root, _, shards = measured
    source = root / "hooks/example.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text("value = 1\nvalue += 1\n")
    _summarize(shards[0], {"hooks/example.py": {1, 2, 99, 100}})
    target = root / "baseline.json"
    cache.record(root, shards, target)
    output = capsys.readouterr().out
    assert "hooks/example.py:99" in output
    assert "hooks/example.py:100" in output
    assert cache.read(target)["executed"] == {"hooks/example.py": [1, 2]}


def test_continuation_lines_publish_as_their_statement(measured: Measured, capsys: pytest.CaptureFixture[str]) -> None:
    root, _, shards = measured
    source = root / "hooks/example.py"
    source.write_text('value = max(\n    1,\n    2,\n)\nscript = """\nconst a = 1;\n"""\n')
    _summarize(shards[0], {"hooks/example.py": {2, 3, 6}})
    target = root / "baseline.json"
    cache.record(root, shards, target)
    assert "hooks/example.py" not in capsys.readouterr().out
    assert cache.read(target)["executed"] == {"hooks/example.py": [1, 5]}


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


def test_an_incomplete_suite_cannot_publish_a_baseline(measured: Measured) -> None:
    root, _, shards = measured
    target = root / "baseline.json"
    with pytest.raises(ValueError, match="shard summary missing"):
        cache.record(root, [*shards, root / "missing.json"], target)
    assert not target.exists()


@pytest.mark.parametrize("folders,code", [(2, 0), (3, 1), (1, 1)], ids=["exact", "extra-shard", "missing-shard"])
def test_the_recorder_reads_exactly_the_shards_the_run_split(measured: Measured, folders: int, code: int) -> None:
    root, _, _ = measured
    for index in range(1, folders + 1):
        summary = root / "downloaded" / f"executed-lines-3.12-{index}" / "executed-lines.json"
        _summarize(summary, {"hooks/example.py": {index}})
    target = root / "baseline.json"
    argv = ["--head", str(root), "--shards", "2", "--head-summaries", str(root / "downloaded"), "--out", str(target)]
    assert cache.main(argv) == code
    assert target.exists() == (code == 0)


def test_an_unparsable_source_fails_publication_with_an_error(
    measured: Measured, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _, _ = measured
    (root / "hooks/example.py").write_text("def (\n")
    _summarize(root / "downloaded" / "executed-lines-3.12-1" / "executed-lines.json", {"hooks/example.py": {1}})
    target = root / "baseline.json"
    argv = ["--head", str(root), "--shards", "1", "--head-summaries", str(root / "downloaded"), "--out", str(target)]
    assert cache.main(argv) == 1
    assert "::error::Cannot record the coverage baseline" in capsys.readouterr().out
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
    assert job["needs"] in (["unit", "gate-required"], ["unit", "coverage-ratchet"])
    condition = "!cancelled() && github.event_name == 'push' && needs.unit.result == 'success'"
    if "coverage-ratchet" in job["needs"]:
        condition += " && needs.coverage-ratchet.result == 'success'"
    assert job["if"] == "${{ " + condition + " }}"


def test_publisher_waits_only_for_measurements_and_the_ratchet():
    workflow = Path(__file__).parents[1] / ".github/workflows/test.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["coverage-baseline"]
    assert job["needs"] == ["unit", "coverage-ratchet"]


def test_publisher_reads_shards_without_the_optional_sonar_report() -> None:
    workflow = Path(__file__).parents[1] / ".github/workflows/test.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["coverage-baseline"]
    steps = {step.get("name"): step for step in job["steps"]}
    download = steps["Download this run's measured coverage"]["with"]
    assert download["pattern"] == "executed-lines-3.12-*"
    assert "name" not in download
    assert f"--head-summaries {download['path']} " in steps["Compute the coverage baseline"]["run"]


def test_the_publisher_never_downloads_a_shard_coverage_database() -> None:
    jobs = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    downloads = [
        step["with"]["pattern"]
        for step in jobs["coverage-baseline"]["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert downloads and all(pattern == "executed-lines-3.12-*" for pattern in downloads)
    summary = next(step["run"] for step in jobs["unit"]["steps"] if step.get("name") == "Summarize the executed lines")
    assert f"databases=({cache.OWN_DATABASE})" in summary
    assert 'python -m tests.coverage_summary --out executed-lines.json "${databases[@]}"' in summary


def test_a_shard_summary_records_the_lines_its_database_ran(
    measured: Measured, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = measured
    (root / "hooks/example.py").write_text("value = 1\nif value:\n    value += 1\nelse:\n    value = 0\n")
    shard = root / "shard"
    shard.mkdir()
    data = CoverageData(basename=str(shard / ".coverage"))
    data.set_context("tests/test_one.py::test_one|run")
    data.add_arcs({"hooks/example.py": {(-1, 1), (1, 2), (2, 3), (3, -1)}, "tests/test_one.py": {(-1, 1), (1, -1)}})
    data.set_context("tests/test_two.py::test_two|run")
    data.add_arcs({"hooks/example.py": {(-1, 1), (1, 2), (2, 5), (5, -1)}})
    data.write()
    summary = root / "downloaded" / "executed-lines-3.12-1" / "executed-lines.json"
    summary.parent.mkdir(parents=True)
    monkeypatch.chdir(shard)
    coverage_summary.write([Path(".coverage")], summary)
    target = root / "baseline.json"
    argv = ["--head", str(root), "--shards", "1", "--head-summaries", str(root / "downloaded"), "--out", str(target)]
    assert cache.main(argv) == 0
    database = {path: sorted(ran) for path, ran in measured_lines(shard / ".coverage").items()}
    assert database == {"hooks/example.py": [1, 2, 3, 5]}
    assert cache.read(target)["executed"] == database


def test_the_baseline_reads_only_each_shards_own_database(measured: Measured) -> None:
    root, _, shards = measured
    own = {"hooks/example.py": [1]}
    shards[0].write_text(json.dumps({cache.OWN_DATABASE: own, "reused-coverage/.coverage": {"hooks/example.py": [2]}}))
    target = root / "baseline.json"
    cache.record(root, shards, target)
    assert cache.read(target)["executed"] == own


def test_a_shard_summary_without_graded_lines_cannot_publish(measured: Measured) -> None:
    root, _, shards = measured
    shards[0].write_text(json.dumps({cache.OWN_DATABASE: {}, "reused-coverage/.coverage": {"hooks/example.py": [1]}}))
    target = root / "baseline.json"
    with pytest.raises(ValueError, match="measured no line under hooks or scripts"):
        cache.record(root, shards, target)
    assert not target.exists()


@pytest.mark.parametrize("restored", ["ancestor", "sibling", "unknown", "missing-commit", "absent"])
def test_publisher_records_only_ancestor_history(tmp_path: Path, restored: str) -> None:
    import os
    import subprocess

    project = Path(__file__).parents[1]
    job = yaml.safe_load((project / ".github/workflows/test.yml").read_text())["jobs"]["coverage-baseline"]
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PYTHONPATH": str(project),
        "GIT_AUTHOR_NAME": "Coverage",
        "GIT_AUTHOR_EMAIL": "coverage@example.invalid",
        "GIT_COMMITTER_NAME": "Coverage",
        "GIT_COMMITTER_EMAIL": "coverage@example.invalid",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init")
    (repo / "hooks").mkdir()
    (repo / "hooks/example.py").write_text("value = 1\n")
    git("add", "hooks/example.py")
    git("commit", "-m", "Base")
    ancestor = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    sibling = git("commit-tree", tree, "-p", ancestor, "-m", "Sibling")
    git("commit", "--allow-empty", "-m", "Head")
    head = git("rev-parse", "HEAD")
    target = Path(env["HOME"]) / "coverage-baseline/baseline.json"
    target.parent.mkdir(parents=True)
    previous = {"tree": tree, "executed": {"hooks/example.py": [1]}, "history": [], "shards": 20}
    if restored != "missing-commit":
        previous["commit"] = {"ancestor": ancestor, "sibling": sibling}.get(restored, "f" * 40)
    if restored != "absent":
        target.write_text(json.dumps(previous))
    for index in range(1, 21):
        summary = repo / "baseline-coverage" / f"executed-lines-3.12-{index}" / "executed-lines.json"
        _summarize(summary, {"hooks/example.py": {1}})
    script = next(step["run"] for step in job["steps"] if step.get("name") == "Compute the coverage baseline")
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script], cwd=repo, env=env, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(target.read_text())
    assert value["commit"] == head
    assert [entry["commit"] for entry in value["history"]] == ([ancestor] if restored == "ancestor" else [])
