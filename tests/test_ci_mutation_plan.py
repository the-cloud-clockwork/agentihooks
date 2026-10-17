import json

import pytest

from scripts.ci_mutation import plan


def test_each_mutant_costs_the_measured_worker_seconds_whatever_its_tests_take(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    _project(tmp_path, {"tests/test_sample.py::t": 0.01})
    fast = plan.estimate(tmp_path, {"scripts/sample.py": {2}})
    (tmp_path / ".test_durations").write_text(json.dumps({"tests/test_sample.py::t": 90.0}))
    assert plan.estimate(tmp_path, {"scripts/sample.py": {2}}) == fast
    assert fast[0] == pytest.approx(fast[1] * plan.MUTANT_SECONDS)
    assert plan.MUTANT_SECONDS == 5.6
    assert plan.shard_count(412 * plan.MUTANT_SECONDS, 412, 240, 10) == 3


def test_shared_statistics_replace_flat_costs_before_assigning_shards():
    from scripts.ci_mutation.measured import measured_costs, plan_shards

    names = [f"scripts.sample.x_expensive__mutmut_{i}" for i in range(8)]
    names += [f"scripts.sample.x_cheap__mutmut_{i}" for i in range(8)]
    stats = [
        {
            "tests": {"scripts.sample.x_expensive": ["slow"], "scripts.sample.x_cheap": ["fast"]},
            "durations": {"slow": 200.0, "fast": 1.0},
        }
    ]
    costs = measured_costs(names, stats)
    assert costs[names[0]] >= 200.0
    assert costs[names[-1]] >= 1.0
    assert costs[names[0]] > costs[names[-1]]
    plan = plan_shards(costs, target=100, limit=10)
    assert plan["shards"] > 1
    assert set(plan["assignments"]) == set(names)
    assert plan["units"] == "worker seconds"
    assert plan == plan_shards(dict(reversed(list(costs.items()))), target=100, limit=10)
    expensive_shards = [plan["assignments"][name] for name in names[:8]]
    assert len(set(expensive_shards)) == min(plan["shards"], 8)


def test_measured_costs_merge_mappings_keep_slowest_durations_and_cover_missing_nodes():
    from scripts.ci_mutation.measured import measured_costs

    names = [
        "scripts.sample.x_f__mutmut_1",
        "scripts.sample.x_new__mutmut_1",
        "scripts.sample.x_name__mutmut_tag__mutmut_1",
    ]
    results = [
        {
            "tests": {"scripts.sample.x_f": ["fast", "missing"], "scripts.sample.x_name__mutmut_tag": ["fast"]},
            "durations": {"fast": 0.25, "slow": 10},
        },
        {"tests": {"scripts.sample.x_f": ["fast", "missing"]}, "durations": {"fast": 0.1, "slow": 3}},
    ]
    assert measured_costs(names, results) == pytest.approx({names[0]: 18.4125, names[1]: 80.6, names[2]: 5.9125})
    assert measured_costs([names[1]], []) == {names[1]: 80.6}


def test_expensive_mutants_are_balanced_before_cheap_names_and_totals_are_preserved():
    from scripts.ci_mutation.measured import plan_shards

    plan = plan_shards({"a": 1, "b": 1, "c": 1, "z": 9}, target=2)
    assert plan == {
        "shards": 2,
        "assignments": {"z": 0, "a": 1, "b": 1, "c": 1},
        "costs": {"a": 1, "b": 1, "c": 1, "z": 9},
        "units": "worker seconds",
        "estimated_worker_seconds": 12,
        "shard_worker_seconds": [9, 3],
        "workers": 4,
    }
    assert plan_shards({"a": 480, "b": 481})["shards"] == 2
    assert plan_shards({str(i): 10000 for i in range(11)})["shards"] == 10


@pytest.mark.parametrize("args", [[], ["--bases", "base"], ["--stats", "stats"]])
def test_measured_cli_requires_bases_and_statistics(monkeypatch, args):
    from scripts.ci_mutation import measured

    monkeypatch.setattr("sys.argv", ["measured", *args])
    with pytest.raises(SystemExit) as error:
        measured.main()
    assert error.value.code == 2


@pytest.mark.parametrize("identity_only", [False, True])
def test_refinement_preserves_identity_groups_and_skips_plain_modules(tmp_path, monkeypatch, identity_only):
    from scripts.ci_mutation import measured
    from scripts.ci_mutation.stats import SharedStats, stats_key, write_part

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    _project(tmp_path, {})
    directory = tmp_path / "scripts/ci_mutation"
    directory.mkdir()
    (directory / "identity.py").write_text("def f():\n    return 1\n")
    identity = {"scripts/ci_mutation/identity.py": ({2}, ["tests/test_identity.py"])}
    normal = {} if identity_only else {"scripts/sample.py": ({2}, ["tests/test_sample.py"])}
    stats = SharedStats(tmp_path / "stats", "head")
    keys = []
    for index, group in enumerate(group for group in [normal, identity] if group):
        key = stats_key("head", group)
        keys.append(key)
        write_part(stats.group(index).folder / "part-0.json", key, (0, 1), [])
    result = measured.refine_plan(
        tmp_path, {**normal, **identity, "scripts/plain.py": ({1}, ["tests/test_plain.py"])}, stats
    )
    assert result["head"] == "head"
    assert result["keys"] == keys
    assert any(name.startswith("scripts.ci_mutation.identity.") for name in result["assignments"])
    assert any(name.startswith("scripts.sample.") for name in result["assignments"]) is (not identity_only)
    assert not any(name.startswith("scripts.plain.") for name in result["assignments"])
    empty = measured.refine_plan(
        tmp_path, {"scripts/plain.py": ({1}, ["tests/test_plain.py"])}, SharedStats(tmp_path / "missing", "head")
    )
    assert empty["assignments"] == {}
    assert empty["keys"] == []


def test_refinement_names_the_file_whose_pragma_is_broken(tmp_path, monkeypatch):
    from mutmut.mutation.pragma_handling import PragmaParseError

    from scripts.ci_mutation import measured
    from scripts.ci_mutation.stats import SharedStats, stats_key, write_part

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    _project(tmp_path, {})
    (tmp_path / "scripts/broken.py").write_text("# pragma: no mutate end\ndef f():\n    return 1\n")
    selected = {"scripts/broken.py": ({3}, ["tests/test_broken.py"])}
    stats = SharedStats(tmp_path / "stats", "head")
    write_part(stats.group(0).folder / "part-0.json", stats_key("head", selected), (0, 1), [])
    with pytest.raises(PragmaParseError, match="at scripts/broken.py:1"):
        measured.refine_plan(tmp_path, selected, stats)


@pytest.mark.parametrize("stale", [False, True])
def test_measured_cli_refines_complete_statistics_and_rejects_another_head(tmp_path, monkeypatch, capsys, stale):
    from scripts.ci_mutation import measured
    from scripts.ci_mutation.stats import stats_key, write_part

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    _project(tmp_path, {})
    selected = {"scripts/sample.py": ({2}, ["tests/test_sample.py"])}
    folder = tmp_path / "stats"
    key = stats_key("other" if stale else "head", selected)
    write_part(
        folder / "group-0/part-0.json",
        key,
        (0, 1),
        [{"tests": {"scripts.sample.x_f": ["test"]}, "durations": {"test": 40.0}}],
    )
    monkeypatch.setattr(
        measured,
        "discover_changes",
        lambda root, bases, head: (
            {"scripts/sample.py": {2}} if (root, bases, head) == (tmp_path, ["base", "earlier"], "HEAD") else {}
        ),
    )
    monkeypatch.setattr(
        measured.subprocess,
        "check_output",
        lambda args, text: "head\n" if args == ["git", "rev-parse", "HEAD"] and text else "wrong",
    )
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["measured", "--bases", "base,earlier", "--stats", str(folder)])
    if stale:
        with pytest.raises(ValueError, match="stats stale"):
            measured.main()
    else:
        assert measured.main() == 0
        plan = json.loads((folder / "plan.json").read_text())
        assert plan["head"] == "head"
        assert plan["keys"] == [key]
        assert plan["assignments"]
        assert all(name.startswith("scripts.sample.x_f__mutmut_") for name in plan["assignments"])
        assert set(plan["costs"].values()) == {55.6}
        assert plan["units"] == "worker seconds"
        assert output.read_text() == "shards=[0]\n"
        text = capsys.readouterr().out
        assert "Measured mutation worker seconds:" in text
        assert f"Measured mutation shards: 1; assigned mutants: {len(plan['assignments'])}" in text


def test_every_consumer_uses_the_same_measured_assignment(tmp_path, monkeypatch):
    from scripts.ci_mutation.mutant_shards import shard_names

    monkeypatch.chdir(tmp_path)
    names = ["a", "b", "c", "d"]
    (tmp_path / "shard-plan.json").write_text(
        json.dumps({"shards": 2, "assignments": {"a": 1, "b": 1, "c": 0, "d": 1}})
    )
    assert shard_names(names, (0, 2)) == {"c"}
    assert shard_names(names[:3], (1, 2)) == {"a", "b"}
    assert shard_names(reversed(names), (1, 2)) == {"a", "b", "d"}
    with pytest.raises(ValueError, match="^measured mutation plan has a different shard count$"):
        shard_names(names, (0, 3))
    with pytest.raises(ValueError, match="missing mutant"):
        shard_names([*names, "new"], (0, 2))


@pytest.mark.parametrize("reason", ["", "over budget"])
def test_reports_distinguish_complete_survivor_grades_from_budget_exhaustion(tmp_path, monkeypatch, reason):
    from scripts.ci_mutation.runner import run_gate
    from scripts.ci_mutation.stats import SharedStats

    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts/sample.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests/test_sample.py").write_text("pass\n")
    stats = tmp_path / "stats"
    stats.mkdir()
    name = "scripts.sample.x_f__mutmut_1"
    (stats / "plan.json").write_text(json.dumps({"shards": 1, "assignments": {name: 0}, "costs": {name: 10}}))
    row = {"name": name, "status": "survived", "lines": [2], "fingerprint": "abc", "diff": "changed"}
    monkeypatch.setattr("scripts.ci_mutation.runner.mutate_files", lambda *args: ({"scripts/sample.py": [row]}, reason))
    report = run_gate(tmp_path, {"scripts/sample.py": {2}}, tmp_path / "output", 1080, stats=SharedStats(stats, "head"))
    assert report["failed"] is True
    assert report["shard"]["budget_seconds"] == 1080
    assert report["shard"]["expected"] == 1
    assert report["shard"]["graded"] == (0 if reason else 1)
    assert report["shard"]["complete"] is (not bool(reason))
    assert report["shard"]["budget_exhausted"] is bool(reason)
    assert report["shard"]["estimated_worker_seconds"] == 10
    assert report["shard"]["wall_seconds"] >= 0


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


def _timed_tests(tmp_path, seconds, marks=None):
    (tmp_path / "tests").mkdir(exist_ok=True)
    for name in seconds:
        mark = (
            f'import pytest\n\npytestmark = pytest.mark.xdist_group("{marks[name]}")\n' if name in (marks or {}) else ""
        )
        (tmp_path / f"tests/test_{name}.py").write_text(mark + "def test_x():\n    pass\n")
    (tmp_path / ".test_durations").write_text(
        json.dumps({f"tests/test_{name}.py::test_x": value for name, value in seconds.items()})
    )
    return [f"tests/test_{name}.py" for name in sorted(seconds)]


@pytest.mark.parametrize(
    ("target", "limit", "expected"),
    [(81, 10, (1, 80)), (41, 10, (2, 40)), (21, 10, (4, 20)), (5, 10, (4, 20)), (5, 3, (2, 40))],
)
def test_stats_parts_stop_at_the_target_or_where_one_file_sets_the_floor(tmp_path, target, limit, expected):
    tests = _timed_tests(tmp_path, {name: 20 for name in "abcdefghijklmnop"})
    parts, wall = plan.stats_part_count(tmp_path, tests, target, limit)
    assert (parts, wall) == (expected[0], pytest.approx(expected[1], abs=0.1))


def test_stats_parts_split_a_shared_group_across_parts_but_never_inside_one(tmp_path):
    tests = _timed_tests(tmp_path, {name: 10 for name in "abcdefgh"}, dict.fromkeys("abcdefgh", "port"))
    assert plan.slowest_bucket(tmp_path, tests, 1) == pytest.approx(80, abs=0.1)
    assert plan.stats_part_count(tmp_path, tests, 21, 10) == (4, pytest.approx(20, abs=0.1))
    assert plan.stats_part_count(tmp_path, [], 20, 10) == (1, 0)


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


def test_estimate_counts_each_changed_line_mutant_and_its_selected_tests(tmp_path, monkeypatch):
    from scripts.ci_mutation.selection import changed_mutations

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    timings = {"tests/test_sample.py::t1": 0.2, "tests/test_sample.py::Case::t2": 0.6, "tests/test_x.py::t": 9}
    _project(tmp_path, timings | {"tests/test_other.py::t": 2.0})
    source = (tmp_path / "scripts/sample.py").read_text()
    per_mutant = plan.MUTANT_SECONDS
    for lines in ({2}, {2, 6}):
        _, mutations = changed_mutations("scripts/sample.py", source, lines)
        seconds, mutants, tests = plan.estimate(tmp_path, {"scripts/sample.py": lines})
        assert mutants == len(mutations) > 0
        assert seconds == pytest.approx(mutants * per_mutant)
        assert tests == ["tests/test_sample.py"]
    sample = plan.estimate(tmp_path, {"scripts/sample.py": {2}})
    other = plan.estimate(tmp_path, {"scripts/other.py": {2}})
    both = plan.estimate(tmp_path, {"scripts/plain.py": {1}, "scripts/sample.py": {2}, "scripts/other.py": {2}})
    assert other[1] > 0
    assert both == (
        pytest.approx(sample[0] + other[0]),
        sample[1] + other[1],
        ["tests/test_other.py", "tests/test_sample.py"],
    )
    assert plan.estimate(tmp_path, {"scripts/sample.py": {3}}) == (0, 0, ["tests/test_sample.py"])
    assert changed_mutations("scripts/sample.py", source, {9})[1]
    assert plan.estimate(tmp_path, {"scripts/sample.py": {9}}) == (0, 0, ["tests/test_sample.py"])
    assert plan.estimate(tmp_path, {"scripts/plain.py": {1}}) == (0, 0, [])


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
        lambda root, base, head, graded: (
            ["earlier", "merged"]
            if (root, base, head, graded) == (tmp_path, "base", "HEAD", plan.graded_green)
            else None
        ),
    )
    monkeypatch.setattr(
        "scripts.ci_mutation.plan.estimate",
        lambda root, changes: (
            (1601.4, 50, ["tests/test_sample.py"])
            if (root, changes) == (tmp_path, {"scripts/sample.py": {2}})
            else None
        ),
    )
    monkeypatch.setattr(
        "scripts.ci_mutation.plan.stats_part_count",
        lambda root, tests, target, limit: (
            (3, 120.6) if (root, tests, target, limit) == (tmp_path, ["tests/test_sample.py"], 100, 4) else None
        ),
    )
    monkeypatch.setattr("sys.argv", [*__import__("sys").argv, "--stats-target", "100"])
    assert plan.main() == 0
    assert calls == [(tmp_path, ["earlier", "merged"], "HEAD")]
    assert output.read_text() == "shards=[0, 1, 2, 3]\nstats_parts=[0, 1, 2]\nbases=earlier,merged\nchanged=1\n"
    assert capsys.readouterr().out == (
        "Mutation bases: earlier merged\n"
        "Changed line mutants: 50\nEstimated mutation seconds: 1601, slowest stats bucket: 121\n"
        "Mutation shards: 4\nStats parts: 3\n"
    )


@pytest.mark.parametrize(
    ("estimated", "shards", "parts"),
    [
        ((0, 0, {}), "[0]", "[0]"),
        ((962, 500, dict.fromkeys("abcde", 15.24)), "[0, 1, 2]", "[0, 1]"),
        ((480, 500, {}), "[0]", "[0]"),
        ((481, 500, {}), "[0, 1]", "[0]"),
        ((10**6, 10**4, {f"t{n}": 30 for n in range(40)}), json.dumps(list(range(10))), json.dumps(list(range(10)))),
    ],
)
def test_main_defaults_to_origin_dev_a_two_minute_target_a_half_minute_stats_bucket_and_ten_shards(
    tmp_path, monkeypatch, estimated, shards, parts
):
    seconds, mutants, timed = estimated
    estimated = (seconds, mutants, _timed_tests(tmp_path, timed))
    output = tmp_path / "github-output"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["plan"])
    calls = []

    def discover(root, base, head):
        calls.append((root, base, head))
        return {}

    monkeypatch.setattr("scripts.ci_mutation.plan.discover_changes", discover)
    monkeypatch.setattr("scripts.ci_mutation.plan.own_bases", lambda root, base, head, graded: [f"{base}@{head}"])
    monkeypatch.setattr("scripts.ci_mutation.plan.estimate", lambda root, changes: estimated)
    assert plan.main() == 0
    assert calls == [(tmp_path, ["origin/dev@HEAD"], "HEAD")]
    assert output.read_text() == f"shards={shards}\nstats_parts={parts}\nbases=origin/dev@HEAD\nchanged=0\n"
