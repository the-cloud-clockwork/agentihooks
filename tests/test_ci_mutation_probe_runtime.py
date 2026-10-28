import json

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

    def mutate(root, work, selected, deadline, shard, shared):
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

    def mutate(root, work, selected, deadline, shard, shared):
        calls.append(deadline - 100)
        return {"scripts/a.py": []}, ""

    monkeypatch.setattr(runner, "mutate_files", mutate)
    runner.run_gate(tmp_path, {"scripts/a.py": {2}}, tmp_path / "output", 1080, (0, 2), stats)
    assert calls == [1071]


@pytest.mark.parametrize("planned", [False, True])
def test_probe_command_uses_the_declared_scope_budget_and_shard_and_writes_its_record(tmp_path, monkeypatch, planned):
    import sys

    from scripts.ci_mutation import scope

    plan = _plan()
    stats = _shared(tmp_path, plan)
    monkeypatch.chdir(tmp_path)
    if planned:
        (tmp_path / ".mutation-selection.json").write_text(
            json.dumps({"scripts/a.py": ["tests/a.py"], "scripts/b.py": []})
        )
    seen = []
    monkeypatch.setattr(
        scope, "discover_changes", lambda *args: seen.append(args) or {"scripts/a.py": {2}, "scripts/b.py": {3}}
    )
    monkeypatch.setattr(
        scope, "select_tests", lambda root, path: ["tests/a.py"] if path.as_posix() == "scripts/a.py" else []
    )
    monkeypatch.setattr(probe.subprocess, "check_output", lambda *args, **kwargs: "revision\n")
    record = _records(plan)[0]

    def run(root, selected, shared, shard, budget):
        seen.append((root, selected, shared, shard, budget))
        return record

    monkeypatch.setattr(probe, "run_probe", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "--stats",
            str(stats.folder),
            "--bases",
            "base-one,base-two",
            "--shard",
            "0",
            "--shards",
            "2",
            "--budget",
            "100",
        ],
    )
    assert probe.main() == 0
    assert seen == [
        (tmp_path, ["base-one", "base-two"], "HEAD"),
        (tmp_path, {"scripts/a.py": ({2}, ["tests/a.py"])}, stats, (0, 2), 100),
    ]
    assert json.loads((stats.folder / "probe-0.json").read_text()) == record


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
