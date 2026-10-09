import json

import pytest

from scripts.ci_mutation import plan


def test_mean_test_seconds_reads_stored_timings_of_selected_files_only():
    durations = {"tests/test_a.py::t1": 1.0, "tests/test_a.py::t2": 3.0, "tests/test_b.py::t": 40.0}
    assert plan.mean_test_seconds(durations, ["tests/test_a.py"]) == 2.0
    assert plan.mean_test_seconds(durations, ["tests/test_new.py"]) == plan.UNTIMED_TEST_SECONDS


@pytest.mark.parametrize(("seconds", "expected"), [(0, 1), (1, 1), (960, 1), (961, 2), (4800, 5), (10**6, 10)])
def test_shard_count_fills_each_shard_to_its_target_and_stays_within_the_limit(seconds, expected):
    assert plan.shard_count(seconds, 240, 10) == expected


def _project(tmp_path, durations):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["scripts/"]\n')
    (tmp_path / "scripts/sample.py").write_text("def f(a, b):\n    return a - b\n\n\ndef g(a):\n    return a > 1\n")
    (tmp_path / "scripts/plain.py").write_text("VALUE = 1\n")
    (tmp_path / "tests/test_sample.py").write_text("from scripts.sample import f\n")
    (tmp_path / ".test_durations").write_text(json.dumps(durations))


def test_estimate_weighs_each_changed_line_mutant_by_its_selected_test_timings(tmp_path, monkeypatch):
    from scripts.ci_mutation.selection import changed_mutations

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    _project(tmp_path, {"tests/test_sample.py::t": 0.4})
    source = (tmp_path / "scripts/sample.py").read_text()
    per_mutant = plan.MUTANT_SECONDS + plan.TEST_WEIGHT * 0.4
    for lines in ({2}, {2, 6}):
        _, mutations = changed_mutations("scripts/sample.py", source, lines)
        assert plan.estimate(tmp_path, {"scripts/sample.py": lines}) == pytest.approx(len(mutations) * per_mutant)
    assert plan.estimate(tmp_path, {"scripts/sample.py": {2, 6}}) > plan.estimate(tmp_path, {"scripts/sample.py": {2}})
    assert plan.estimate(tmp_path, {"scripts/sample.py": {3}}) == 0
    assert plan.estimate(tmp_path, {"scripts/plain.py": {1}}) == 0


def test_main_writes_one_matrix_entry_per_shard(tmp_path, monkeypatch, capsys):
    output = tmp_path / "github-output"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["plan", "--base", "base", "--target", "100", "--limit", "4"])
    calls = []

    def discover(root, base, head):
        calls.append((root, base, head))
        return {"scripts/sample.py": {2}}

    monkeypatch.setattr("scripts.ci_mutation.plan.discover_changes", discover)
    monkeypatch.setattr("scripts.ci_mutation.plan.estimate", lambda root, changes: 1201)
    assert plan.main() == 0
    assert calls == [(tmp_path, "base", "HEAD")]
    assert output.read_text() == "shards=[0, 1, 2, 3]\n"
    assert capsys.readouterr().out == "Estimated mutation seconds: 1201\nMutation shards: 4\n"


def test_main_plans_one_shard_when_nothing_is_mutable(tmp_path, monkeypatch):
    output = tmp_path / "github-output"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["plan"])
    monkeypatch.setattr("scripts.ci_mutation.plan.discover_changes", lambda root, base, head: {})
    assert plan.main() == 0
    assert output.read_text() == "shards=[0]\n"
