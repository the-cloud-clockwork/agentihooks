from pathlib import Path

import pytest
from coverage import CoverageData

from tests import coverage_ratchet as ratchet

pytestmark = pytest.mark.unit

SOURCE = "def f(x):\n    if x:\n        return 1\n    return 2\n"


def _shard(path: Path, lines: dict[str, list[int]]) -> Path:
    data = CoverageData(basename=str(path))
    data.add_lines(lines)
    data.write()
    return path


def _measure(commit, executed, sources):
    return ratchet.Measurement(commit, executed, lambda path: sources.get(path))


def test_unchanged_lines_map_across_an_insert_above_them():
    old = "a\nb\nc\n"
    new = "a\nnew\nb\nc\n"
    assert ratchet.line_map(old, new) == {1: 1, 2: 3, 3: 4}


def test_shards_union_their_executed_lines_under_the_graded_trees(tmp_path):
    one = _shard(tmp_path / "one", {"hooks/a.py": [1, 2], "tests/t.py": [1]})
    two = _shard(tmp_path / "two", {"hooks/a.py": [3], "scripts/b.py": [5]})
    assert ratchet.executed([one, two]) == {"hooks/a.py": {1, 2, 3}, "scripts/b.py": {5}}


def test_a_shard_that_measured_nothing_cannot_be_graded(tmp_path):
    one = _shard(tmp_path / "one", {"hooks/a.py": [1]})
    empty = _shard(tmp_path / "empty", {"tests/t.py": [1]})
    with pytest.raises(ratchet.Unmeasured, match="empty"):
        ratchet.executed([one, empty])


def test_a_line_the_base_ran_and_the_head_no_longer_runs_is_lost():
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter([base]))
    assert result.base == "b1"
    assert result.lost == {"hooks/a.py": [3]}


def test_an_added_line_no_test_runs_is_not_a_lost_line():
    head_source = SOURCE + "\n\ndef g():\n    return 3\n"
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})
    result = ratchet.grade({"hooks/a.py": {1, 2, 3, 7}}, lambda path: head_source, iter([base]))
    assert result.lost == {}


def test_a_deleted_module_loses_nothing():
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})
    assert ratchet.grade({}, lambda path: None, iter([base])).lost == {}


def test_a_renamed_module_keeps_every_line_the_base_ran():
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})
    sources = {"hooks/b.py": SOURCE}
    result = ratchet.grade({"hooks/b.py": {1, 2}}, sources.get, iter([base]), {"hooks/a.py": "hooks/b.py"})
    assert result.lost == {"hooks/a.py": [3]}


def test_a_module_the_head_stopped_measuring_loses_every_line_it_still_has():
    base = _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE})
    assert ratchet.grade({}, lambda path: SOURCE, iter([base])).lost == {"hooks/a.py": [1, 2, 3]}


def test_a_line_older_runs_missed_after_an_even_older_run_covered_it_is_cleared():
    runs = [
        _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE}),
        _measure("b2", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}),
        _measure("b3", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE}),
    ]
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert result.lost == {}
    assert result.unstable == {"hooks/a.py": [3]}


def test_a_line_first_covered_recently_stays_graded():
    runs = [
        _measure("b1", {"hooks/a.py": {1, 2, 3}}, {"hooks/a.py": SOURCE}),
        _measure("b2", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}),
        _measure("b3", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE}),
    ]
    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, iter(runs))
    assert result.lost == {"hooks/a.py": [3]}


def test_history_is_read_only_when_a_line_was_lost():
    def runs():
        yield _measure("b1", {"hooks/a.py": {1, 2}}, {"hooks/a.py": SOURCE})
        raise AssertionError("history read without a lost line")

    assert ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, runs()).lost == {}


def test_history_stops_once_every_lost_line_is_cleared():
    read = []

    def runs():
        for commit, ran in (("b1", {1, 2, 3}), ("b2", {1, 2}), ("b3", {1, 2, 3}), ("b4", {1, 2, 3})):
            read.append(commit)
            yield _measure(commit, {"hooks/a.py": ran}, {"hooks/a.py": SOURCE})

    result = ratchet.grade({"hooks/a.py": {1, 2}}, lambda path: SOURCE, runs())
    assert result.unstable == {"hooks/a.py": [3]}
    assert read == ["b1", "b2", "b3"]


def test_the_base_run_costs_one_github_lookup(monkeypatch, tmp_path):
    from tests import coverage_history

    commits = [f"c{n}" for n in range(coverage_history.SEARCH)]
    looked_up = []

    def passed_run(repo, commit):
        looked_up.append(commit)
        return f"run-{commit}"

    def fetcher(repo, shards, scratch, read):
        return lambda pair: _measure(pair[0], {}, {})

    monkeypatch.setattr(coverage_history, "git", lambda *args, cwd: "\n".join(commits))
    monkeypatch.setattr(coverage_history, "_passed_run", passed_run)
    monkeypatch.setattr(coverage_history, "_fetcher", fetcher)
    runs = coverage_history.dev_runs(tmp_path, "c0", 8, tmp_path)
    assert next(runs).commit == "c0"
    runs.close()
    assert looked_up == ["c0"]
    assert [run.commit for run in coverage_history.dev_runs(tmp_path, "c0", 8, tmp_path)] == commits


def test_no_measured_base_cannot_be_graded():
    with pytest.raises(ratchet.Unmeasured, match="base"):
        ratchet.grade({"hooks/a.py": {1}}, lambda path: SOURCE, iter([]))


def test_the_report_lists_every_missed_line_and_every_lost_line(tmp_path):
    result = ratchet.Result("b1", {"hooks/a.py": [3]}, {})
    text = ratchet.report(result, {"hooks/a.py": [3, 4]}, {"hooks/a.py": 2}, {"hooks/a.py": 3})
    assert "hooks/a.py: covered 2 (base 3), missed 3, 4" in text
    assert "hooks/a.py:3 ran on base b1 and no head test runs it" in text


def test_a_failed_github_read_is_retried_before_the_gate_gives_up(monkeypatch):
    import subprocess

    from tests import coverage_history

    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        code = 1 if len(calls) < 3 else 0
        return subprocess.CompletedProcess(cmd, code, stdout="42\n", stderr="HTTP 502")

    monkeypatch.setattr(coverage_history.subprocess, "run", run)
    monkeypatch.setattr(coverage_history.time, "sleep", lambda seconds: None)
    assert coverage_history._gh("api", "x") == "42"
    assert len(calls) == 3
    calls.clear()
    monkeypatch.setattr(
        coverage_history.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "HTTP 502")
    )
    with pytest.raises(subprocess.CalledProcessError):
        coverage_history._gh("api", "x")
