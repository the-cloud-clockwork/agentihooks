import json
import os

import pytest

from scripts.ci_mutation import probe, runner
from scripts.ci_mutation.stats import SharedStats
from tests.test_ci_mutation_probe import _plan, _records


def _shared(tmp_path, plan):
    folder = tmp_path / "stats"
    folder.mkdir()
    (folder / "plan.json").write_text(json.dumps(plan))
    return SharedStats(folder, plan["head"])


def test_early_probe_runs_only_its_owned_group_with_the_original_deadline_and_keeps_evidence(tmp_path, monkeypatch):
    plan = _plan()
    stats = _shared(tmp_path, plan)
    calls = []
    monkeypatch.setattr(probe.time, "monotonic", lambda: 100)

    def mutate(root, work, selected, deadline, shard, shared, first=()):
        saved = json.loads((shared.folder.parent / "plan.json").read_text())
        calls.append((root, work.name, selected, deadline, shard, saved["probe_mode"]))
        return {"scripts/a.py": [{"name": "scripts.a.x_f__mutmut_1", "status": "killed"}]}, ""

    monkeypatch.setattr(runner, "mutate_files", mutate)
    selected = {"scripts/a.py": ({2}, ["tests/a.py"]), "scripts/ci_mutation/identity.py": ({2}, ["tests/identity.py"])}
    record = probe.run_probe(tmp_path, selected, stats, (0, 2), 1080)
    assert record == _records(plan)[0] | {"seconds": 0}
    assert calls == [(tmp_path, "group-0", {"scripts/a.py": selected["scripts/a.py"]}, 1180, (0, 2), True)]
    assert json.loads((stats.folder / "plan.json").read_text()) == plan
    assert (tmp_path / ".mutation-probe/0/group-0").is_dir()


@pytest.mark.parametrize("fault", ["worker_error", "missing_result", "unknown_status", "over_budget"])
def test_an_early_worker_failure_or_incomplete_grading_cannot_produce_a_shared_result(tmp_path, monkeypatch, fault):
    stats = _shared(tmp_path, _plan())
    monkeypatch.setattr(probe.time, "monotonic", lambda: 100)
    rows = [{"name": "scripts.a.x_f__mutmut_1", "status": "killed"}]
    reason = ""
    if fault == "worker_error":
        reason = "over budget"
    elif fault == "missing_result":
        rows = []
    elif fault == "unknown_status":
        rows[0]["status"] = "not checked"
    monkeypatch.setattr(runner, "mutate_files", lambda *args: ({"scripts/a.py": rows}, reason))
    with pytest.raises((ValueError, RuntimeError)):
        probe.run_probe(
            tmp_path, {"scripts/a.py": ({2}, ["tests/a.py"])}, stats, (0, 2), -1 if fault == "over_budget" else 1080
        )


def test_a_shard_without_split_mutants_emits_an_empty_result_without_starting_a_worker(tmp_path, monkeypatch):
    plan = _plan()
    plan["splits"] = {}
    stats = _shared(tmp_path, plan)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 100)
    monkeypatch.setattr(runner, "mutate_files", lambda *args: pytest.fail("no split mutants need an early worker"))
    record = probe.run_probe(tmp_path, {}, stats, (0, 2), 1080)
    assert record["results"] == {} and record["seconds"] == 0
    assert not (tmp_path / ".mutation-probe").exists()


def test_a_rerun_early_probe_reuses_its_folders_and_a_survivor_within_a_spent_budget_is_shared(tmp_path, monkeypatch):
    plan = _plan()
    stats = _shared(tmp_path, plan)
    (tmp_path / ".mutation-probe/0/stats").mkdir(parents=True)
    (tmp_path / ".mutation-probe/0/group-0").mkdir()
    monkeypatch.setattr(probe.time, "monotonic", lambda: 100)
    seen = []

    def mutate(root, work, selected, deadline, shard, shared, first=()):
        seen.append((work, shared))
        return {"scripts/a.py": [{"name": "scripts.a.x_f__mutmut_1", "status": "survived"}]}, ""

    monkeypatch.setattr(runner, "mutate_files", mutate)
    record = probe.run_probe(tmp_path, {"scripts/a.py": ({2}, ["tests/a.py"])}, stats, (0, 2), 0)
    assert record["results"] == {"scripts.a.x_f__mutmut_1": "survived"}
    folder = tmp_path / ".mutation-probe/0"
    assert seen == [(folder / "group-0", SharedStats(folder / "stats/group-0", "revision"))]
    assert json.loads((folder / "stats/plan.json").read_text()) == probe.probe_plan(plan, (0, 2))


def test_an_early_probe_runs_the_identity_group_alone_when_only_it_owns_split_mutants(tmp_path, monkeypatch):
    split = {"head": ["early"], "slices": [["left"]], "costs": [1]}
    plan = {
        "head": "revision",
        "keys": ["selection"],
        "shards": 1,
        "assignments": {"scripts.ci_mutation.identity.x_h__mutmut_1": [0], "scripts.a.x_g__mutmut_1": 0},
        "splits": {"scripts.ci_mutation.identity.x_h": split},
    }
    stats = _shared(tmp_path, plan)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 100)
    calls = []

    def mutate(root, work, selected, deadline, shard, shared, first=()):
        calls.append((work.name, selected, shared.folder.name))
        return {
            "scripts/ci_mutation/identity.py": [
                {"name": "scripts.ci_mutation.identity.x_h__mutmut_1", "status": "killed"}
            ]
        }, ""

    monkeypatch.setattr(runner, "mutate_files", mutate)
    selected = {"scripts/a.py": ({2}, ["tests/a.py"]), "scripts/ci_mutation/identity.py": ({3}, ["tests/identity.py"])}
    record = probe.run_probe(tmp_path, selected, stats, (0, 1), 1080)
    assert record["results"] == {"scripts.ci_mutation.identity.x_h__mutmut_1": "killed"}
    assert calls == [
        ("group-1", {"scripts/ci_mutation/identity.py": selected["scripts/ci_mutation/identity.py"]}, "group-1")
    ]


@pytest.mark.parametrize(
    ("reason", "rows", "error", "message"),
    [
        ("over budget", [], RuntimeError, "early mutation failed: over budget\n"),
        ("", [], ValueError, "early mutation did not completely grade its assigned mutants within budget"),
    ],
)
def test_a_refused_early_probe_names_its_cause(tmp_path, monkeypatch, reason, rows, error, message):
    stats = _shared(tmp_path, _plan())
    monkeypatch.setattr(probe.time, "monotonic", lambda: 100)
    monkeypatch.setattr(runner, "mutate_files", lambda *args: ({"scripts/a.py": rows}, reason))
    with pytest.raises(error) as raised:
        probe.run_probe(tmp_path, {"scripts/a.py": ({2}, ["tests/a.py"])}, stats, (0, 2), 1080)
    assert str(raised.value) == message


def test_shared_early_elapsed_time_is_charged_before_the_remaining_worker_starts(tmp_path, monkeypatch):
    plan = _plan()
    stats = _shared(tmp_path, probe.merge_probes(plan, _records(plan)))
    source = tmp_path / "scripts/a.py"
    source.parent.mkdir()
    source.write_text("def f():\n    return 1\n")
    (tmp_path / ".mutation-selection.json").write_text(json.dumps({"scripts/a.py": ["tests/a.py"]}))
    calls = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: 100)
    monkeypatch.setattr(runner, "load_clearances", lambda root: {})
    monkeypatch.setattr(runner, "record_shard_measurement", lambda *args: None)

    def mutate(root, work, selected, deadline, shard, shared, first=()):
        calls.append(deadline - 100)
        return {"scripts/a.py": []}, ""

    monkeypatch.setattr(runner, "mutate_files", mutate)
    runner.run_gate(tmp_path, {"scripts/a.py": {2}}, tmp_path / "output", 1080, (0, 2), stats)
    assert calls == [1071]


@pytest.mark.parametrize("planned", [False, True])
def test_probe_command_uses_the_declared_scope_budget_and_shard_and_writes_its_record(
    tmp_path, monkeypatch, capsys, planned
):
    import sys

    from scripts.ci_mutation import scope

    plan = _plan()
    stats = _shared(tmp_path, plan)
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "scripts/a.py"
    source.parent.mkdir()
    source.write_text("def f():\n    return 1\n")
    (tmp_path / "scripts/constant.py").write_text("VALUE = 1\n")
    if planned:
        (tmp_path / ".mutation-selection.json").write_text(
            json.dumps(
                {
                    "scripts/a.py": ["tests/planned.py"],
                    "scripts/b.py": [],
                    "scripts/constant.py": ["tests/constants.py"],
                }
            )
        )
    seen = []
    monkeypatch.setattr(
        scope,
        "discover_changes",
        lambda *args: seen.append(args) or {"scripts/a.py": {2}, "scripts/b.py": {3}, "scripts/constant.py": {1}},
    )
    monkeypatch.setattr(
        scope,
        "select_tests",
        lambda root, path: {"scripts/a.py": [f"{root}/tests/a.py"], "scripts/constant.py": ["tests/constants.py"]}.get(
            path.as_posix(), []
        ),
    )
    monkeypatch.setattr(
        probe.subprocess, "check_output", lambda *args, **kwargs: seen.append((args, kwargs)) or "revision\n"
    )
    record = _records(plan)[0]

    def run(root, selected, shared, shard, budget):
        seen.append((root, selected, shared, shard, budget))
        seen.append((os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD"), os.environ.get("MUTATION_STATS_ROOT")))
        return record

    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    monkeypatch.delenv("MUTATION_STATS_ROOT", raising=False)
    if planned:
        monkeypatch.setenv("MUTATION_STATS_CACHE", str(tmp_path / "cache"))
    else:
        monkeypatch.delenv("MUTATION_STATS_CACHE", raising=False)
    monkeypatch.setattr(probe, "run_probe", run)
    budget = ["--budget", "100"] if planned else []
    argv = ["probe", "--stats", str(stats.folder), "--bases", "base-one,base-two", "--shard", "0", "--shards", "2"]
    monkeypatch.setattr(sys, "argv", argv + budget)
    assert probe.main() == 0
    tests = ["tests/planned.py"] if planned else [f"{tmp_path}/tests/a.py"]
    assert seen == [
        (tmp_path, ["base-one", "base-two"], "HEAD"),
        ((["git", "rev-parse", "HEAD"],), {"text": True}),
        (tmp_path, {"scripts/a.py": ({2}, tests)}, stats, (0, 2), 100 if planned else 1080),
        ("1", str(tmp_path) if planned else None),
    ]
    assert json.loads((stats.folder / "probe-0.json").read_text()) == record
    assert capsys.readouterr().out == json.dumps(record) + "\n"


def test_probe_command_requires_the_shared_stats_folder(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["probe", "--bases", "base", "--shard", "0", "--shards", "1"])
    with pytest.raises(SystemExit):
        probe.main()


def test_merge_command_validates_every_part_before_publishing_the_shared_plan(tmp_path, monkeypatch, capsys):
    import sys

    plan = _plan()
    stats = _shared(tmp_path, plan)
    for record in _records(plan):
        (stats.folder / f"probe-{record['index']}.json").write_text(json.dumps(record))
    (stats.folder / "unrelated.json").write_text("{}")
    monkeypatch.setattr(sys, "argv", ["probe", "--stats", str(stats.folder), "--merge"])
    assert probe.main() == 0
    assert json.loads((stats.folder / "plan.json").read_text()) == probe.merge_probes(plan, _records(plan))
    assert capsys.readouterr().out == "Shared early mutations: 2; charged seconds: 9.000\n"


def test_reused_kills_skip_only_proven_mutants_and_keep_baseline_controls_and_survivors():
    calls = []
    tests = ["tests/a.py::test_writer"]

    def execute(worker, *, mutant_name, tests):
        calls.append((worker, mutant_name, tests))
        return 4

    cached = probe.reuse_kills(execute, {"killed": "killed", "survived": "survived"})
    assert cached("worker", mutant_name="killed", tests=tests) == 1
    assert cached("worker", mutant_name="survived", tests=tests) == 4
    assert cached("worker", mutant_name=None, tests=tests) == 4
    assert calls == [("worker", "survived", tests), ("worker", None, tests)]


def test_probe_provenance_marks_only_a_reused_kill_and_preserves_other_rows(tmp_path):
    plan = _plan()
    merged = probe.merge_probes(plan, _records(plan))
    (tmp_path / "shard-plan.json").write_text(json.dumps(merged))
    rows = {
        "scripts/a.py": [
            {"name": "scripts.a.x_f__mutmut_1", "status": "killed"},
            {"name": "scripts.a.x_f__mutmut_2", "status": "survived"},
            {"name": "scripts.a.x_g__mutmut_1", "status": "killed"},
        ]
    }
    assert probe.mark_probe_results(tmp_path, rows) is rows
    assert rows["scripts/a.py"] == [
        {"name": "scripts.a.x_f__mutmut_1", "status": "killed", "reused_early_kill": True},
        {"name": "scripts.a.x_f__mutmut_2", "status": "survived"},
        {"name": "scripts.a.x_g__mutmut_1", "status": "killed"},
    ]
    shared = SharedStats(tmp_path, "revision")
    assert probe.probe_elapsed(None) == 0
    assert probe.probe_elapsed(shared) == 0
    (tmp_path / "plan.json").write_text(json.dumps(merged))
    assert probe.probe_elapsed(shared) == 9
    assert probe.probe_elapsed(SharedStats(tmp_path, "revision", (0, 1))) == 0
