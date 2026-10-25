import json
import subprocess
from pathlib import Path

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


@pytest.mark.parametrize("changed", [{2}, {6}, {10, 13, 16}, set(range(1, 22)), set()])
def test_refinement_selects_names_without_rendering_mutant_source(tmp_path, monkeypatch, changed):
    from scripts.ci_mutation import measured
    from scripts.ci_mutation.selection import selected_mutants
    from scripts.ci_mutation.stats import SharedStats, stats_key, write_part

    source = (
        "VALUE = 0\n"
        "def outer(value):\n"
        "    return value + 1\n"
        "\n"
        "class Example:\n"
        "    def __init__(self, value):\n"
        "        self.value = value + 2\n"
        "\n"
        "    @classmethod\n"
        "    def make(cls, value):\n"
        "        return cls(value + 3)\n"
        "\n"
        "    async def result(self):\n"
        "        return self.value + 4\n"
        "\n"
        "    class Nested:\n"
        "        def ignored(self): return 5\n"
        "\n"
        "async def later(value):\n"
        "    return value + 6\n"
        "\n"
        "if True:\n"
        "    def conditional():\n"
        "        return 7\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    _project(tmp_path, {})
    (tmp_path / "sample.py").write_text(source)
    selected = {"sample.py": (changed, ["tests/test_sample.py"])}
    stats = SharedStats(tmp_path / "stats", "head")
    key = stats_key(stats.head, selected)
    write_part(stats.group(0).folder / "part-0.json", key, (0, 1), [])
    _, expected = selected_mutants("sample.py", source, changed)

    def refuse_render(*args, **kwargs):
        pytest.fail("Refinement must not render mutated source")

    monkeypatch.setattr("mutmut.mutation.file_mutation.combine_mutations_to_source", refuse_render)
    plan = measured.refine_plan(tmp_path, selected, stats)
    assert sorted(plan["assignments"]) == sorted(f"sample.{name}" for name in expected)
    assert plan["keys"] == [key]


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


def test_a_mutant_with_a_recorded_killer_costs_that_test_and_one_without_keeps_its_traced_sum(tmp_path):
    from types import SimpleNamespace

    from scripts.ci_mutation.killers import KillMemory
    from scripts.ci_mutation.measured import measured_costs

    memory = KillMemory(tmp_path / "killers")
    killers = {"x_f": "fast", "x_gone": "dropped", "x_new": "unmeasured"}
    for function, killer in killers.items():
        memory.mutant = f"scripts.sample.{function}__mutmut_1"
        memory.pytest_runtest_logreport(SimpleNamespace(failed=True, nodeid=killer))
    names = [
        "scripts.sample.x_f__mutmut_2",
        "scripts.sample.x_g__mutmut_1",
        "scripts.sample.x_gone__mutmut_3",
        "scripts.sample.x_new__mutmut_2",
    ]
    results = [
        {
            "tests": {
                "scripts.sample.x_f": ["fast", "slow"],
                "scripts.sample.x_g": ["fast", "slow"],
                "scripts.sample.x_gone": ["fast", "slow"],
                "scripts.sample.x_new": ["fast", "unmeasured"],
            },
            "durations": {"fast": 2.0, "slow": 200.0},
        }
    ]
    traced = 5.6 + 1.25 * 202.0
    # A killer the stats never timed costs as much as the slowest timed test.
    unmeasured = 5.6 + 1.25 * 200.0
    assert measured_costs(names, results, memory) == pytest.approx(
        {names[0]: 8.1, names[1]: traced, names[2]: traced, names[3]: unmeasured}
    )
    assert measured_costs(names, results) == pytest.approx(
        {**dict.fromkeys(names[:3], traced), names[3]: 5.6 + 1.25 * 202.0}
    )


def test_refinement_prices_mutants_from_killers_kept_beside_the_shared_stats(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts.ci_mutation import measured
    from scripts.ci_mutation.killers import KillMemory
    from scripts.ci_mutation.stats import SharedStats, stats_key, write_part

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    _project(tmp_path, {})
    (tmp_path / "sample.py").write_text("def outer(value):\n    return value + 1\n")
    selected = {"sample.py": ({2}, ["tests/test_sample.py"])}
    stats = SharedStats(tmp_path / "stats", "head")
    key = stats_key(stats.head, selected)
    results = [{"tests": {"sample.x_outer": ["fast", "slow"]}, "durations": {"fast": 2.0, "slow": 100.0}}]
    write_part(stats.group(0).folder / "part-0.json", key, (0, 1), results)
    traced = measured.refine_plan(tmp_path, selected, stats)["costs"]
    memory = KillMemory(stats.folder / "killers")
    memory.mutant = next(iter(traced))
    memory.pytest_runtest_logreport(SimpleNamespace(failed=True, nodeid="fast"))
    killed = measured.refine_plan(tmp_path, selected, stats)["costs"]
    assert traced
    assert set(killed) == set(traced)
    assert list(traced.values()) == pytest.approx([5.6 + 1.25 * 102.0] * len(traced))
    assert list(killed.values()) == pytest.approx([8.1] * len(killed))


def test_measured_costs_price_tests_by_the_cpu_their_contended_walls_took():
    from scripts.ci_mutation.measured import measured_costs, measured_splits

    name = "scripts.sample.x_f__mutmut_1"
    tests = {"scripts.sample.x_f": ["a", "b"]}
    contended = 5.6 + 1.25 * (30.0 + 40.0) / 3
    results = [
        {"tests": tests, "durations": {"a": 30.0}, "cpu": 10.0},
        {"tests": {}, "durations": {"b": 40.0}, "cpu": 10.0},
        {"tests": {}, "durations": {"c": 20.0}, "cpu": 10.0},
    ]
    assert measured_costs([name], results) == pytest.approx({name: contended})
    cached = [{**result, "cpu": cpu} for result, cpu in zip(results, (15.0, 5.0, 5.0), strict=True)]
    cached += [{"tests": {}, "durations": {"c": 2.0}, "cpu": 5.0}, {"tests": {}, "durations": {}, "cpu": 4.0}]
    assert measured_costs([name], cached) == pytest.approx({name: contended})
    uncontended = [{"tests": tests, "durations": {"a": 3.0, "b": 4.0}, "cpu": 10.0}]
    assert measured_costs([name], uncontended) == pytest.approx({name: 5.6 + 1.25 * 7.0})
    quick = [{"tests": tests, "durations": {"a": 0.3, "b": 0.3}, "cpu": 0.2}]
    assert measured_costs([name], quick) == pytest.approx({name: 5.6 + 1.25 * 0.2})
    untimed = [{"tests": tests, "durations": {"a": 30.0, "b": 40.0}}]
    assert measured_costs([name], untimed) == pytest.approx({name: 5.6 + 1.25 * 70.0})
    long = [{"tests": tests, "durations": {"a": 300.0, "b": 300.0}, "cpu": 200.0}]
    assert measured_splits([name], long) == {}
    assert measured_splits([name], [{**long[0], "cpu": 600.0}])["scripts.sample.x_f"]["head"] == ["a"]


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
        "splits": {},
    }
    assert plan_shards({"a": 480, "b": 481})["shards"] == 2
    assert plan_shards({str(i): 10000 for i in range(11)})["shards"] == 10


def test_a_mutant_whose_tests_outlast_one_slice_runs_the_fast_head_with_each_slice_on_its_own_shard():
    from scripts.ci_mutation.measured import HEAD_SECONDS, SLICE_SECONDS, measured_costs, measured_splits, plan_shards

    slow = [f"tests/test_s.py::t{i}" for i in range(30)]
    fast = [f"tests/test_f.py::t{i}" for i in range(10)]
    durations = {**dict.fromkeys(slow, 20.0), **dict.fromkeys(fast, 1.0), "tests/test_c.py::t": 1.0}
    results = [
        {
            "tests": {"scripts.sample.x_heavy": [*slow, *fast], "scripts.sample.x_light": ["tests/test_c.py::t"]},
            "durations": durations,
        }
    ]
    names = [f"scripts.sample.x_heavy__mutmut_{i}" for i in range(3)] + ["scripts.sample.x_light__mutmut_1"]
    splits = measured_splits(names, results, limit=10)
    assert list(splits) == ["scripts.sample.x_heavy"]
    split = splits["scripts.sample.x_heavy"]
    assert split["head"] == sorted(fast) and sum(durations[test] for test in split["head"]) <= HEAD_SECONDS
    assert len(split["slices"]) == 5 == -(-600 // SLICE_SECONDS)
    tail = [test for chosen in split["slices"] for test in chosen]
    assert sorted(tail) == sorted(slow) and len(tail) == len(set(tail))
    assert all(sum(durations[test] for test in chosen) == 120.0 for chosen in split["slices"])
    plan = plan_shards(measured_costs(names, results), splits=splits)
    assert plan["splits"] == splits
    for name in names[:3]:
        assert sorted(plan["assignments"][name]) == sorted(set(plan["assignments"][name]))
        assert len(plan["assignments"][name]) == 5
        assert plan["costs"][name] == pytest.approx(sum(split["costs"]))
    assert isinstance(plan["assignments"]["scripts.sample.x_light__mutmut_1"], int)
    assert plan["shards"] >= 5
    assert max(plan["shard_worker_seconds"]) - min(plan["shard_worker_seconds"]) <= max(split["costs"])
    assert len(measured_splits(names, results, limit=3)["scripts.sample.x_heavy"]["slices"]) == 3
    assert measured_splits(["scripts.sample.x_light__mutmut_1"], results) == {}


def test_the_head_takes_the_fastest_tests_to_its_budget_and_slices_balance_the_rest_by_duration():
    from scripts.ci_mutation.measured import split_tests

    seconds = {"h1": 5.0, "h2": 5.0, "h3": 10.0, "a4": 100.0, "a3": 60.0, "a2": 50.0, "a1": 30.0}
    assert split_tests(set(seconds), seconds, 10) == {
        "head": ["h1", "h2", "h3"],
        "slices": [["a1", "a4"], ["a2", "a3"]],
        "costs": [pytest.approx(5.6 + 1.25 * 150), pytest.approx(5.6 + 1.25 * 130)],
    }
    assert split_tests(set(seconds) - {"a1", "a2", "a3"}, seconds, 10) is None
    assert split_tests({"a4", "a3", "a2"}, seconds, 10)["head"] == ["a2"]


def test_splits_cover_untimed_tests_cap_at_ten_slices_and_reach_the_refined_plan(tmp_path, monkeypatch):
    from scripts.ci_mutation import measured
    from scripts.ci_mutation.stats import SharedStats, stats_key, write_part

    nodes = [f"tests/test_sample.py::t{i:02}" for i in range(13)]
    results = [{"tests": {"scripts.sample.x_f": nodes}, "durations": dict.fromkeys(nodes[:12], 130.0)}]
    names = ["scripts.sample.x_f__mutmut_1"]
    split = measured.measured_splits(names, results)["scripts.sample.x_f"]
    assert len(split["slices"]) == 10
    assert sorted([*split["head"], *(node for chosen in split["slices"] for node in chosen)]) == nodes
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    _project(tmp_path, {})
    selected = {"scripts/sample.py": ({2}, ["tests/test_sample.py"])}
    stats = SharedStats(tmp_path / "stats", "head")
    write_part(stats.group(0).folder / "part-0.json", stats_key("head", selected), (0, 1), results)
    refined = measured.refine_plan(tmp_path, selected, stats)
    assert list(refined["splits"]) == ["scripts.sample.x_f"]
    assert refined["assignments"] and all(len(shards) == 10 for shards in refined["assignments"].values())


def test_each_unit_runs_the_head_and_its_own_slice_and_only_on_its_shard(tmp_path, monkeypatch):
    from scripts.ci_mutation.mutant_shards import (
        shard_names,
        shard_tests,
        split_names,
        unit_cost,
        unit_count,
        unit_tests,
    )

    monkeypatch.chdir(tmp_path)
    split = {"head": ["h"], "slices": [["a"], ["b"]], "costs": [3.0, 4.0]}
    plan = {
        "shards": 3,
        "assignments": {"scripts.s.x_f__mutmut_1": [2, 0], "scripts.s.x_g__mutmut_1": 1},
        "costs": {"scripts.s.x_f__mutmut_1": 7.0, "scripts.s.x_g__mutmut_1": 5.0},
        "splits": {"scripts.s.x_f": split},
    }
    name = "scripts.s.x_f__mutmut_1"
    assert unit_tests(plan, name, 2) == {"h", "a"}
    assert unit_tests(plan, name, 0) == {"h", "b"}
    assert unit_tests(plan, name, 1) is None
    assert unit_tests(plan, "scripts.s.x_g__mutmut_1", 1) is None
    assert (unit_cost(plan, name, 0), unit_cost(plan, name, 2), unit_cost(plan, "scripts.s.x_g__mutmut_1", 1)) == (
        4.0,
        3.0,
        5.0,
    )
    assert split_names(plan) == {name}
    assert unit_count(plan) == 3
    twice = "scripts.s.x_f__mutmut_tag__mutmut_1"
    nested = {"assignments": {twice: [0, 1]}, "splits": {"scripts.s.x_f__mutmut_tag": split}}
    assert (unit_tests(nested, twice, 1), unit_cost(nested, twice, 1)) == ({"h", "b"}, 4.0)
    assert shard_tests(["h", "a", "b"], name, 0) == ["h", "a", "b"]
    (tmp_path / "shard-plan.json").write_text(json.dumps(plan))
    assert [shard_names(plan["assignments"], (index, 3)) for index in range(3)] == [
        {name},
        {"scripts.s.x_g__mutmut_1"},
        {name},
    ]
    assert shard_tests(["b", "h", "a"], name, 0) == ["b", "h"]
    assert shard_tests(["b", "h", "a"], name, 2) == ["h", "a"]
    assert shard_tests(["b", "a"], "scripts.s.x_g__mutmut_1", 1) == ["b", "a"]
    assert shard_tests(["b", "a"], None, 0) == ["b", "a"]


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


def _git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        text=True,
    ).strip()


@pytest.mark.parametrize("stale", [False, True])
def test_measured_cli_refines_complete_statistics_and_rejects_another_head(tmp_path, monkeypatch, capsys, stale):
    from scripts.ci_mutation import measured
    from scripts.ci_mutation.stats import stats_key, write_part

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    _project(tmp_path, {})
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "scripts", "tests", "pyproject.toml", ".test_durations")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "branch", "base")
    _git(tmp_path, "branch", "earlier")
    path = tmp_path / "scripts/sample.py"
    path.write_text(path.read_text().replace("return a - b", "return a + b"))
    _git(tmp_path, "add", "scripts/sample.py")
    _git(tmp_path, "commit", "-qm", "change")
    head = _git(tmp_path, "rev-parse", "HEAD")
    selected = {"scripts/sample.py": ({2}, ["tests/test_sample.py"])}
    folder = tmp_path / "stats"
    key = stats_key("other" if stale else head, selected)
    write_part(
        folder / "group-0/part-0.json",
        key,
        (0, 1),
        [{"tests": {"scripts.sample.x_f": ["test"]}, "durations": {"test": 40.0}}],
    )
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["measured", "--bases", "base,earlier", "--stats", str(folder)])
    if stale:
        with pytest.raises(ValueError, match="stats stale"):
            measured.main()
        assert not (folder / "plan.json").exists()
        assert not output.exists()
    else:
        assert measured.main() == 0
        result = json.loads((folder / "plan.json").read_text())
        assert result["head"] == head
        assert result["keys"] == [key]
        assert result["assignments"]
        assert all(name.startswith("scripts.sample.x_f__mutmut_") for name in result["assignments"])
        assert set(result["costs"].values()) == {55.6}
        assert result["units"] == "worker seconds"
        assert output.read_text() == "shards=[0]\n"
        text = capsys.readouterr().out
        assert "Measured mutation worker seconds:" in text
        assert f"Measured mutation shards: 1; assigned mutants: {len(result['assignments'])}" in text


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


def test_a_shard_grades_its_whole_mutants_and_hands_split_units_to_the_merge(tmp_path, monkeypatch):
    from scripts.ci_mutation.runner import run_gate
    from scripts.ci_mutation.stats import SharedStats

    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts/sample.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests/test_sample.py").write_text("pass\n")
    stats = tmp_path / "stats"
    stats.mkdir()
    whole, split, unchecked, killed = (f"scripts.sample.x_f__mutmut_{index}" for index in (1, 2, 3, 4))
    plan = {
        "shards": 2,
        "assignments": {whole: 1, split: [1, 0], unchecked: [0, 1], killed: [1, 0]},
        "costs": {whole: 10, split: 7, unchecked: 7, killed: 7},
        "splits": {"scripts.sample.x_f": {"head": ["h"], "slices": [["a"], ["b"]], "costs": [3, 4]}},
    }
    (stats / "plan.json").write_text(json.dumps(plan))
    rows = [
        {"name": whole, "status": "killed", "lines": [], "fingerprint": ""},
        {"name": split, "status": "survived", "lines": [2], "fingerprint": "abc", "diff": "changed"},
        {"name": unchecked, "status": "not checked", "lines": [2], "fingerprint": "def", "diff": "changed"},
        {"name": killed, "status": "killed", "lines": [], "fingerprint": ""},
    ]
    monkeypatch.setattr("scripts.ci_mutation.runner.mutate_files", lambda *args: ({"scripts/sample.py": rows}, ""))
    report = run_gate(
        tmp_path, {"scripts/sample.py": {2}}, tmp_path / "output", 1080, (1, 2), SharedStats(stats, "head")
    )
    assert report["failed"] is False
    assert report["files"][0]["counts"] == {"killed": 1}
    assert report["units"] == [{"path": "scripts/sample.py", "lines": [2], "rows": rows[1:]}]
    assert (report["shard"]["expected"], report["shard"]["graded"]) == (4, 3)
    assert report["shard"]["estimated_worker_seconds"] == 20


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


@pytest.mark.parametrize(("quota", "workers"), [("3", 3), ("4", 4)])
def test_shards_are_sized_by_the_workers_the_batch_runner_quota_starts(tmp_path, monkeypatch, quota, workers):
    from scripts.ci_mutation.measured import plan_shards

    monkeypatch.setenv("MUTATION_SHARD_CPU_QUOTA", quota)
    assert plan.shard_count(workers * 240 * 4, 500, 240, 10) == 4
    tests = _timed_tests(tmp_path, {name: 10 for name in "abcdefghijkl"})
    assert plan.slowest_bucket(tmp_path, tests, 1) == pytest.approx(120 / workers, abs=0.1)
    assert plan_shards({"scripts.sample.x__mutmut_1": 1.0})["workers"] == workers


def test_every_mutation_planning_step_reads_the_batch_runner_cpu_quota():
    import yaml

    planners = (
        "scripts.ci_mutation.plan ",
        "scripts.ci_mutation.measured ",
        "scripts.ci_mutation.replay prepare",
        "python .refinement-proof/run.py",
    )
    steps = {
        f"{path.name}/{name}/{index}": step
        for path in sorted((Path(__file__).parents[1] / ".github/workflows").glob("*.yml"))
        for name, job in (yaml.safe_load(path.read_text())["jobs"] or {}).items()
        for index, step in enumerate(job.get("steps", []))
        if any(planner in step.get("run", "") for planner in planners)
    }
    assert len(steps) >= 6
    assert [key for key, step in steps.items() if step.get("env", {}).get("MUTATION_SHARD_CPU_QUOTA") != "3"] == []


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


def test_stats_parts_read_the_stored_durations_once(tmp_path, monkeypatch):
    tests = _timed_tests(tmp_path, {name: 20 for name in "abcdefgh"})
    reads = []
    read_text = Path.read_text

    def counted(self, *args, **kwargs):
        if self.name == ".test_durations":
            reads.append(self)
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    assert plan.stats_part_count(tmp_path, tests, 21, 10) == (2, pytest.approx(20, abs=0.1))
    assert len(reads) == 1


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


def _budget_project(tmp_path, monkeypatch, seconds, timed):
    _project(tmp_path, {})
    tests = _timed_tests(tmp_path, timed)
    source = "".join(f"def f{index}():\n    return {index}\n\n" for index in range(40))
    (tmp_path / "scripts/budget.py").write_text(source)
    for path in tests:
        with (tmp_path / path).open("a") as stream:
            stream.write("from scripts.budget import f0\n")
    lines = set(range(1, len(source.splitlines()) + 1))
    monkeypatch.setattr(plan, "MUTANT_SECONDS", seconds / 40)
    return {"scripts/budget.py": lines} if seconds else {}, tests


def test_main_writes_one_matrix_entry_per_shard(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    changes, tests = _budget_project(tmp_path, monkeypatch, 1601.4, {f"t{n}": 120.6 for n in range(9)})
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(
        "sys.argv", ["plan", "--base", "base", "--target", "100", "--limit", "4", "--stats-target", "100"]
    )
    calls = []

    def bases(root, base, head, *args):
        calls.append((root, base, head))
        return ["earlier", "merged"]

    def discover(root, bases, head):
        calls.append((root, bases, head))
        return changes

    monkeypatch.setattr(plan, "own_bases", bases)
    monkeypatch.setattr(plan, "discover_changes", discover)
    assert plan.main() == 0
    assert calls == [(tmp_path, "base", "HEAD"), (tmp_path, ["earlier", "merged"], "HEAD")]
    assert json.loads((tmp_path / ".mutation-selection.json").read_text()) == {"scripts/budget.py": tests}
    assert output.read_text() == (
        "shards=[0, 1, 2, 3]\nstats_parts=[0, 1, 2]\nbases=earlier,merged\nchanged=1\nbrowser=false\n"
    )
    text = capsys.readouterr().out
    assert "Mutation bases: earlier merged\nChanged line mutants: 40\n" in text
    assert "Estimated mutation seconds: 1601, slowest stats bucket: 121\n" in text
    assert "Mutation shards: 4\nStats parts: 3\n" in text


@pytest.mark.parametrize("changed", [{}, {"scripts/sample.py": {2}}])
def test_main_reads_the_recorded_coverage_only_when_a_module_changed(tmp_path, monkeypatch, capsys, changed):
    _project(tmp_path, {})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["plan", "--base", "base", "--repository", "owner/repo"])
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github-output"))
    calls = []

    def discover(root, bases, head):
        calls.append(("discover", root, bases, head))
        return changed

    def record(repository):
        calls.append(("record", repository))
        return None, ""

    monkeypatch.setattr(plan, "read_record", record)
    monkeypatch.setattr(plan, "own_bases", lambda *args: ["base"])
    monkeypatch.setattr(plan, "discover_changes", discover)
    assert plan.main() == 0
    assert calls == [("discover", tmp_path, ["base"], "HEAD"), *[("record", "owner/repo")] * bool(changed)]
    assert f"changed={len(changed)}\n" in (tmp_path / "github-output").read_text()
    if not changed:
        assert "No change remains to mutate, so the recorded dev coverage is not read.\n" in capsys.readouterr().out


@pytest.mark.parametrize("source, matched", [("sample", False), ("sample", True), ("plain", False)])
def test_planning_refuses_an_unmatched_module_before_publishing_statistics(tmp_path, monkeypatch, source, matched):
    _project(tmp_path, {})
    (tmp_path / "tests/test_sample.py").unlink()
    if matched:
        (tmp_path / "tests/test_sample.py").write_text("from scripts.sample import f\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["plan"])
    monkeypatch.setattr(plan, "own_bases", lambda *args: ["base"])
    monkeypatch.setattr(plan, "discover_changes", lambda *args: {f"scripts/{source}.py": {2}})
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    if source == "sample" and not matched:
        with pytest.raises(SystemExit) as refusal:
            plan.main()
        assert str(refusal.value) == "scripts/sample.py: not mutated, no matching or importing test modules"
        assert not output.exists()
        assert not (tmp_path / ".mutation-selection.json").exists()
    else:
        assert plan.main() == 0
        assert "stats_parts=" in output.read_text()


@pytest.mark.parametrize(
    "imports, grouped, refused",
    [
        ("import mcp\n", False, True),
        ("import mcp\n", True, True),
        ("def test_sdk():\n    import mcp\n", False, True),
        ("def test_sdk():\n    import mcp\n", True, False),
    ],
)
def test_planning_checks_selected_test_collection_and_worker_contracts(
    tmp_path, monkeypatch, imports, grouped, refused
):
    from tests import test_collection_imports as contracts

    _project(tmp_path, {})
    monkeypatch.setattr(contracts, "_ROOT", tmp_path)
    monkeypatch.setattr(contracts, "_module_file", contracts._module_file.__wrapped__)
    group = 'pytestmark = pytest.mark.xdist_group("mcp-sdk")\n' if grouped else ""
    (tmp_path / "tests/test_sample.py").write_text("from scripts.sample import f\n" + group + imports)
    (tmp_path / "tests/test_unrelated.py").write_text("import mcp\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["plan"])
    monkeypatch.setattr(plan, "own_bases", lambda *args: ["base"])
    monkeypatch.setattr(plan, "discover_changes", lambda *args: {"scripts/sample.py": {2}})
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    if refused:
        with pytest.raises(SystemExit, match="tests/test_sample.py"):
            plan.main()
        assert not output.exists()
        assert not (tmp_path / ".mutation-selection.json").exists()
    else:
        assert plan.main() == 0
        assert "stats_parts=" in output.read_text()


def test_planning_follows_selected_test_package_imports(tmp_path, monkeypatch):
    from tests import test_collection_imports as contracts

    _project(tmp_path, {})
    (tmp_path / "tests/__init__.py").write_text("import mcp\n")
    monkeypatch.setattr(contracts, "_ROOT", tmp_path)
    monkeypatch.setattr(contracts, "_module_file", contracts._module_file.__wrapped__)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["plan"])
    monkeypatch.setattr(plan, "own_bases", lambda *args: ["base"])
    monkeypatch.setattr(plan, "discover_changes", lambda *args: {"scripts/sample.py": {2}})
    with pytest.raises(SystemExit, match="tests/test_sample.py"):
        plan.main()


@pytest.mark.parametrize(
    ("seconds", "timed", "shards", "parts"),
    [
        (0, {}, "[0]", "[0]"),
        (962, dict.fromkeys("abcde", 15.24), "[0, 1, 2]", "[0]"),
        (480, {"a": 1}, "[0]", "[0]"),
        (481, {"a": 1}, "[0, 1]", "[0]"),
        (10**6, {f"t{n}": 30 for n in range(40)}, json.dumps(list(range(10))), json.dumps(list(range(10)))),
    ],
)
def test_main_defaults_to_origin_dev_a_two_minute_target_a_one_minute_stats_bucket_and_ten_shards(
    tmp_path, monkeypatch, seconds, timed, shards, parts
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    changes, tests = _budget_project(tmp_path, monkeypatch, seconds, timed)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["plan"])
    calls = []

    def bases(root, base, head, *args):
        calls.append((root, base, head))
        return ["base"]

    monkeypatch.setattr(plan, "own_bases", bases)
    monkeypatch.setattr(plan, "discover_changes", lambda *args: changes)
    assert plan.main() == 0
    assert calls == [(tmp_path, "origin/dev", "HEAD")]
    assert json.loads((tmp_path / ".mutation-selection.json").read_text()) == (
        {"scripts/budget.py": tests} if seconds else {}
    )
    assert output.read_text() == (
        f"shards={shards}\nstats_parts={parts}\nbases=base\nchanged={int(bool(seconds))}\nbrowser=false\n"
    )


@pytest.mark.parametrize(
    ("ceiling", "shards", "run_id", "expected"),
    [
        (1, 1, 1000, (1, 0)),
        (30, 3, 1000, (10, 0)),
        (30, 5, 1001, (6, 5)),
        (10, 3, 1002, (3, 0)),
        (31, 3, 1009, (10, 9)),
        (2, 3, 1000, (1, 0)),
    ],
)
def test_mutation_lanes_follow_batch_capacity_and_actual_stage_width(ceiling, shards, run_id, expected):
    from scripts.ci_mutation.lanes import allocation

    assert allocation(ceiling, shards, run_id) == expected


@pytest.mark.parametrize(("ceiling", "shards"), [(0, 3), (-1, 3), (30, 0), (30, -1)])
def test_mutation_lanes_reject_missing_capacity_or_stage_width(ceiling, shards):
    from scripts.ci_mutation.lanes import allocation

    with pytest.raises(ValueError, match="^Batch ceiling and stage width must be positive$"):
        allocation(ceiling, shards, 1000)


def test_mutation_lanes_read_the_authoritative_public_batch_ceiling(tmp_path, monkeypatch, capsys):
    from scripts.ci_mutation.lanes import main

    values = tmp_path / "values.yaml"
    values.write_text("gha-runner-scale-set:\n  maxRunners: 30\n")
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["lanes", "--values", str(values), "--shards", "[0, 1, 2]", "--run-id", "1009"])
    assert main() == 0
    assert output.read_text() == "ceiling=30\nlanes=10\nlane=9\n"
    assert capsys.readouterr().out == "ceiling=30\nlanes=10\nlane=9\n"


def test_mutation_lanes_reuse_the_same_ceiling_after_refining_shards(tmp_path, monkeypatch):
    from scripts.ci_mutation.lanes import main

    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["lanes", "--ceiling", "30", "--shards", "[0, 1, 2, 3, 4]", "--run-id", "1009"])
    assert main() == 0
    assert output.read_text() == "ceiling=30\nlanes=6\nlane=1\n"


@pytest.mark.parametrize("missing", ["--ceiling", "--shards", "--run-id"])
def test_mutation_lane_cli_requires_capacity_width_and_run(missing, monkeypatch):
    from scripts.ci_mutation.lanes import main

    arguments = {"--ceiling": "30", "--shards": "[0, 1, 2]", "--run-id": "1009"}
    monkeypatch.setattr(
        "sys.argv", ["lanes", *(value for pair in arguments.items() if pair[0] != missing for value in pair)]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
