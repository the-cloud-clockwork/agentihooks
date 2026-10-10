import json

import pytest

from scripts.ci_mutation import plan


def test_mean_test_seconds_reads_stored_timings_of_selected_files_only():
    durations = {"tests/test_a.py::t1": 1.0, "tests/test_a.py::Case::t2": 3.0, "tests/test_b.py::t": 40.0}
    assert plan.mean_test_seconds(durations, ["tests/test_a.py"]) == 2.0
    assert plan.mean_test_seconds(durations, ["tests/test_new.py"]) == plan.UNTIMED_TEST_SECONDS


@pytest.mark.parametrize(
    ("seconds", "mutants", "expected"),
    [
        (0, 0, 1),
        (1, 1, 1),
        (960, 500, 1),
        (961, 500, 2),
        (4800, 500, 5),
        (4800, 3, 3),
        (10**6, 10**4, 10),
    ],
)
def test_shard_count_fills_each_shard_to_its_target_and_stays_within_the_limit(seconds, mutants, expected):
    assert plan.shard_count(seconds, mutants, 240, 10) == expected


@pytest.mark.parametrize(("stats", "expected"), [(0, 1), (1, 1), (360, 1), (361, 2), (1920, 6), (10**6, 10)])
def test_stats_parts_split_the_stats_pass_to_its_target_and_stay_within_the_limit(stats, expected):
    assert plan.stats_part_count(stats, 90, 10) == expected


def _project(tmp_path, durations):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["scripts/"]\n')
    (tmp_path / "scripts/sample.py").write_text(
        "def f(a, b):\n    return a - b\n\n\ndef g(a):\n    return a > 1\n\n\nLIMIT = 5\n"
    )
    (tmp_path / "scripts/other.py").write_text("def h(a):\n    return a * 3\n")
    (tmp_path / "scripts/plain.py").write_text("VALUE = 1\n")
    (tmp_path / "tests/test_sample.py").write_text("from scripts.sample import f\n")
    (tmp_path / "tests/test_other.py").write_text("from scripts.other import h\n")
    (tmp_path / ".test_durations").write_text(json.dumps(durations))


def test_estimate_weighs_each_changed_line_mutant_by_its_selected_test_timings(tmp_path, monkeypatch):
    from scripts.ci_mutation.selection import changed_mutations

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    timings = {"tests/test_sample.py::t1": 0.2, "tests/test_sample.py::Case::t2": 0.6, "tests/test_x.py::t": 9}
    _project(tmp_path, timings | {"tests/test_other.py::t": 2.0})
    source = (tmp_path / "scripts/sample.py").read_text()
    per_mutant = plan.MUTANT_SECONDS + plan.TEST_WEIGHT * 0.4
    for lines in ({2}, {2, 6}):
        _, mutations = changed_mutations("scripts/sample.py", source, lines)
        seconds, mutants, stats = plan.estimate(tmp_path, {"scripts/sample.py": lines})
        assert mutants == len(mutations) > 0
        assert seconds == pytest.approx(mutants * per_mutant)
        assert stats == pytest.approx(0.8)
    sample = plan.estimate(tmp_path, {"scripts/sample.py": {2}})
    other = plan.estimate(tmp_path, {"scripts/other.py": {2}})
    both = plan.estimate(tmp_path, {"scripts/plain.py": {1}, "scripts/sample.py": {2}, "scripts/other.py": {2}})
    assert other[1] > 0
    assert both == (pytest.approx(sample[0] + other[0]), sample[1] + other[1], pytest.approx(2.8))
    assert plan.estimate(tmp_path, {"scripts/sample.py": {3}}) == (0, 0, pytest.approx(0.8))
    assert changed_mutations("scripts/sample.py", source, {9})[1]
    assert plan.estimate(tmp_path, {"scripts/sample.py": {9}}) == (0, 0, pytest.approx(0.8))
    assert plan.estimate(tmp_path, {"scripts/plain.py": {1}}) == (0, 0, 0)


def test_estimate_names_the_file_whose_pragma_is_broken(tmp_path, monkeypatch):
    from mutmut.mutation.pragma_handling import PragmaParseError

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    _project(tmp_path, {})
    (tmp_path / "scripts/broken.py").write_text("# pragma: no mutate end\ndef f():\n    return 1\n")
    (tmp_path / "tests/test_broken.py").write_text("from scripts.broken import f\n")
    with pytest.raises(PragmaParseError, match="at scripts/broken.py:1"):
        plan.estimate(tmp_path, {"scripts/broken.py": {3}})


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
    monkeypatch.setattr(
        "scripts.ci_mutation.plan.own_bases",
        lambda root, base, head: ["earlier", "merged"] if (root, base, head) == (tmp_path, "base", "HEAD") else None,
    )
    monkeypatch.setattr(
        "scripts.ci_mutation.plan.estimate",
        lambda root, changes: (1601.4, 50, 1201) if (root, changes) == (tmp_path, {"scripts/sample.py": {2}}) else None,
    )
    monkeypatch.setattr("sys.argv", [*__import__("sys").argv, "--stats-target", "100"])
    assert plan.main() == 0
    assert calls == [(tmp_path, ["earlier", "merged"], "HEAD")]
    assert output.read_text() == "shards=[0, 1, 2, 3]\nstats_parts=[0, 1, 2, 3]\nbases=earlier,merged\n"
    assert capsys.readouterr().out == (
        "Mutation bases: earlier merged\n"
        "Changed line mutants: 50\nEstimated mutation seconds: 1601, stats seconds: 1201\n"
        "Mutation shards: 4\nStats parts: 4\n"
    )


@pytest.mark.parametrize(
    ("estimated", "shards", "parts"),
    [
        ((0, 0, 0), "[0]", "[0]"),
        ((962, 500, 361), "[0, 1]", "[0, 1]"),
        ((10**6, 10**4, 10**6), json.dumps(list(range(10))), json.dumps(list(range(10)))),
    ],
)
def test_main_defaults_to_origin_dev_a_four_minute_target_and_ten_shards(
    tmp_path, monkeypatch, estimated, shards, parts
):
    output = tmp_path / "github-output"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["plan"])
    calls = []

    def discover(root, base, head):
        calls.append((root, base, head))
        return {}

    monkeypatch.setattr("scripts.ci_mutation.plan.discover_changes", discover)
    monkeypatch.setattr("scripts.ci_mutation.plan.own_bases", lambda root, base, head: [f"{base}@{head}"])
    monkeypatch.setattr("scripts.ci_mutation.plan.estimate", lambda root, changes: estimated)
    assert plan.main() == 0
    assert calls == [(tmp_path, ["origin/dev@HEAD"], "HEAD")]
    assert output.read_text() == f"shards={shards}\nstats_parts={parts}\nbases=origin/dev@HEAD\n"
