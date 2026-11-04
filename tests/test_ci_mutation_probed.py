import json
from importlib import import_module
from pathlib import Path

import pytest

from scripts.ci_mutation.killers import KillMemory

measured = import_module("scripts.ci_mutation.measured")
probe = import_module("scripts.ci_mutation.probe")
probed = import_module("scripts.ci_mutation.probed")

M1, M2, M3 = (f"scripts.a.x_f__mutmut_{index}" for index in (1, 2, 3))
G, H = "scripts.a.x_g__mutmut_1", "scripts.a.x_h__mutmut_1"
OVERRUN = (
    f"::error title=Mutation plan over budget::Mutant {M2} alone needs 146 worker seconds on one shard, against the"
    " 228 second shard budget less 180 seconds of setup, so no shard count or pull request split can grade it;"
    " shorten or split the tests that cover it."
)


def _plan() -> dict:
    return {
        "head": "revision",
        "keys": ["selection"],
        "shards": 12,
        "probe_shards": 2,
        "assignments": {M1: [0, 1], M2: [1, 0], M3: [0, 1], G: 0, H: [0, 1]},
        "costs": {M1: 60.0, M2: 60.0, M3: 60.0, G: 40.0, H: 40.0},
        "unit_costs": {M1: [30.0, 30.0], M2: [30.0, 30.0], M3: [30.0, 30.0], H: [20.0, 20.0]},
        "splits": {
            "scripts.a.x_f": {
                "head": ["early"],
                "slices": [["left"], ["right"]],
                "costs": [105.6, 155.6],
                "head_seconds": 8.0,
            },
            "scripts.a.x_h": {"head": ["quick"], "slices": [["a"], ["b"]], "costs": [20.0, 20.0], "head_seconds": 2.0},
        },
    }


def _records(plan: dict) -> list[dict]:
    fingerprint = probe.plan_fingerprint(plan)
    return [
        {"plan": fingerprint, "index": 0, "total": 2, "seconds": 7, "results": {M1: "killed", M3: "killed"}},
        {"plan": fingerprint, "index": 1, "total": 2, "seconds": 9, "results": {M2: "survived", H: "killed"}},
    ]


def _merged() -> dict:
    plan = _plan()
    return probe.merge_probes(plan, _records(plan))


@pytest.fixture(autouse=True)
def _three_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUTATION_SHARD_CPU_QUOTA", "3")


def test_the_early_stage_runs_on_its_own_shard_count_and_names_it_in_each_early_plan():
    plan = _plan()
    early = probe.probe_plan(plan, (1, 2))
    assert early["shards"] == 2
    assert early["probe_mode"] is True
    assert early["assignments"] == {M1: 0, M2: 1, M3: 0, G: [], H: 1}
    with pytest.raises(ValueError, match="^early mutation shard does not match the measured plan$"):
        probe.probe_plan(plan, (0, 12))
    assert probe.merge_probes(plan, _records(plan))["probe"]["seconds"] == 9


def test_shards_after_the_early_stage_hold_whole_mutants_and_survivor_slices_while_kills_cost_nothing():
    merged = _merged()
    replanned = probed.probed_plan(merged, 1080)
    assert replanned["shards"] == 2
    assert replanned["assignments"] == {M2: [1, 0], G: 1, M1: 0, M3: 1, H: 0}
    assert replanned["costs"] == {G: 40.0, M2: pytest.approx(241.2), M1: 0.0, M3: 0.0, H: 0.0}
    assert replanned["unit_costs"] == {M2: [pytest.approx(95.6), pytest.approx(145.6)]}
    assert replanned["estimated_worker_seconds"] == pytest.approx(281.2)
    assert replanned["shard_worker_seconds"] == [pytest.approx(145.6), pytest.approx(135.6)]
    assert replanned["splits"] == merged["splits"]
    assert replanned["probe_shards"] == 2
    assert set(replanned) == {
        "head",
        "keys",
        "shards",
        "probe_shards",
        "assignments",
        "costs",
        "unit_costs",
        "splits",
        "probe",
        "units",
        "estimated_worker_seconds",
        "shard_worker_seconds",
        "workers",
    }
    assert probe.probe_results(replanned) == ({M1: "killed", M3: "killed", M2: "survived", H: "killed"}, 9)


def test_the_shards_size_against_the_budget_the_early_stage_left():
    merged = _merged()
    assert probed.probed_plan(merged, 237.6)["shards"] == 2
    assert probed.probed_plan(merged, 237.0)["shards"] == 3


def test_a_large_remainder_spreads_until_no_shard_holds_over_six_minutes_per_worker():
    plan = _plan()
    whole = {f"scripts.a.x_w{index}__mutmut_1": 700.0 for index in range(12)}
    plan["assignments"].update(dict.fromkeys(whole, 0))
    plan["costs"].update(whole)
    merged = probe.merge_probes(plan, _records(plan))
    replanned = probed.probed_plan(merged, 1080)
    assert replanned["shards"] == 12
    assert max(replanned["shard_worker_seconds"]) == pytest.approx(845.6)
    assert max(replanned["shard_worker_seconds"]) <= 3 * probed.SHARD_WORK_SECONDS == 1080


def test_a_survivor_unit_runs_its_slice_alone_or_costs_what_it_last_took(tmp_path: Path):
    merged = _merged()
    memory = KillMemory(tmp_path)
    for tests, seconds in ((["right"], 12.0), (["early", "left"], 3.0)):
        path = memory._cost_path(M2, tests)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tests": tests, "seconds": seconds}))
    assert probed.survivor_units(merged, M2, memory) == [pytest.approx(95.6), 12.0]
    assert probed.survivor_units(merged, M2, None) == [pytest.approx(95.6), pytest.approx(145.6)]
    del merged["splits"]["scripts.a.x_f"]["head_seconds"]
    assert probed.survivor_units(merged, M2, None) == [105.6, 155.6]


def test_a_split_whose_costs_do_not_match_its_slices_is_refused():
    merged = _merged()
    merged["splits"]["scripts.a.x_f"]["costs"] = [105.6]
    with pytest.raises(ValueError):
        probed.survivor_units(merged, M2, None)


def _stats(root: Path, plan: dict) -> Path:
    folder = root / "stats"
    folder.mkdir(parents=True)
    (folder / "plan.json").write_text(json.dumps(plan))
    for record in _records(plan):
        (folder / f"probe-{record['index']}.json").write_text(json.dumps(record))
    return folder


def test_the_cli_writes_the_probed_plan_and_its_shard_matrix(tmp_path, monkeypatch, capsys):
    folder = _stats(tmp_path, _plan())
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["probed", "--stats", str(folder)])
    assert probed.main() == 0
    assert json.loads((folder / "plan.json").read_text()) == json.loads(json.dumps(probed.probed_plan(_merged(), 1080)))
    assert output.read_text() == "shards=[0, 1]\n"
    assert capsys.readouterr().out == (
        "Early mutation kills: 3 of 4 split mutants\nProbed mutation worker seconds: 281.2; probed shards: 2\n"
    )


def test_the_cli_prices_survivor_slices_from_the_branch_kill_memory(tmp_path, monkeypatch):
    folder = _stats(tmp_path, _plan())
    memory = KillMemory(folder / "killers")
    path = memory._cost_path(M2, ["right"])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"tests": ["right"], "seconds": 12.0}))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github-output"))
    monkeypatch.setattr("sys.argv", ["probed", "--stats", str(folder)])
    assert probed.main() == 0
    assert json.loads((folder / "plan.json").read_text())["unit_costs"] == {M2: [pytest.approx(95.6), 12.0]}


@pytest.mark.parametrize("args", [[], ["--budget", "1080"]])
def test_the_cli_requires_the_statistics_folder(monkeypatch, args):
    monkeypatch.setattr("sys.argv", ["probed", *args])
    with pytest.raises(SystemExit) as error:
        probed.main()
    assert error.value.code == 2


def test_the_cli_refuses_a_plan_the_remaining_budget_cannot_hold(tmp_path, monkeypatch, capsys):
    folder = _stats(tmp_path, _plan())
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr("sys.argv", ["probed", "--stats", str(folder), "--budget", "237"])
    assert probed.main() == 1
    assert not output.exists()
    assert capsys.readouterr().out.splitlines()[-1] == OVERRUN


@pytest.mark.parametrize(("cost", "status"), [(2674.5, 1), (2673.0, 0)])
def test_the_cli_grades_against_the_full_shard_budget_by_default(tmp_path, monkeypatch, cost, status):
    plan = _plan()
    plan["costs"][G] = cost
    folder = _stats(tmp_path, plan)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github-output"))
    monkeypatch.setattr("sys.argv", ["probed", "--stats", str(folder)])
    assert probed.main() == status


@pytest.mark.parametrize(
    ("count", "head", "expected"),
    [(20, 139.56, 11), (2000, 20.0, 40), (3, 1000.0, 3), (1, 0.0, 1)],
)
def test_the_early_stage_is_sized_by_the_heads_it_runs(count, head, expected):
    names = {f"scripts.a.x_f__mutmut_{index}": [0, 1] for index in range(count)}
    plan = {"assignments": {**names, G: 0}, "splits": {"scripts.a.x_f": {"head_seconds": head}}}
    assert measured.probe_shard_count(plan) == expected


def test_only_a_plan_with_split_mutants_names_an_early_shard_count():
    slow = [f"tests/test_s.py::t{index}" for index in range(30)]
    fast = [f"tests/test_f.py::t{index}" for index in range(10)]
    durations = {**dict.fromkeys(slow, 20.0), **dict.fromkeys(fast, 1.0), "tests/test_c.py::t": 1.0}
    results = [
        {
            "tests": {"scripts.sample.x_heavy": [*slow, *fast], "scripts.sample.x_light": ["tests/test_c.py::t"]},
            "durations": durations,
        }
    ]
    heavy = [f"scripts.sample.x_heavy__mutmut_{index}" for index in range(3)]
    split = measured.measured_plan([(heavy, results)])
    assert split["splits"]["scripts.sample.x_heavy"]["head_seconds"] == 10.0
    assert split["probe_shards"] == measured.probe_shard_count(split) == 1
    assert "probe_shards" not in measured.measured_plan([(["scripts.sample.x_light__mutmut_1"], results)])
