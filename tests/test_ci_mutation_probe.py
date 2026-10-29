import copy
from importlib import import_module

import pytest

probe = import_module("scripts.ci_mutation.probe")


def _plan():
    return {
        "head": "revision",
        "keys": ["selection"],
        "shards": 2,
        "assignments": {
            "scripts.a.x_f__mutmut_1": [0, 1],
            "scripts.a.x_f__mutmut_2": [1, 0],
            "scripts.a.x_g__mutmut_1": 0,
        },
        "splits": {"scripts.a.x_f": {"head": ["early"], "slices": [["left"], ["right"]], "costs": [1, 1]}},
    }


def _records(plan):
    return [
        {
            "plan": probe.plan_fingerprint(plan),
            "index": 0,
            "total": 2,
            "seconds": 7,
            "results": {"scripts.a.x_f__mutmut_1": "killed"},
        },
        {
            "plan": probe.plan_fingerprint(plan),
            "index": 1,
            "total": 2,
            "seconds": 9,
            "results": {"scripts.a.x_f__mutmut_2": "survived"},
        },
    ]


def test_early_probes_partition_only_split_mutants_once_without_mutating_the_original_plan():
    from scripts.ci_mutation.mutant_shards import holds, unit_tests

    plan = _plan()
    before = copy.deepcopy(plan)
    owned = []
    for index in range(2):
        early = probe.probe_plan(plan, (index, 2))
        names = [name for name, assignment in early["assignments"].items() if holds(assignment, index)]
        owned.extend(names)
        assert len(names) == 1
        assert unit_tests(early, names[0], index) == {"early"}
    assert sorted(owned) == ["scripts.a.x_f__mutmut_1", "scripts.a.x_f__mutmut_2"]
    assert plan == before


def test_complete_early_results_share_kills_and_remove_early_tests_from_survivor_slices():
    from scripts.ci_mutation.mutant_shards import unit_tests

    plan = _plan()
    merged = probe.merge_probes(plan, _records(plan))
    results, seconds = probe.probe_results(merged)
    assert results == {"scripts.a.x_f__mutmut_1": "killed", "scripts.a.x_f__mutmut_2": "survived"}
    assert seconds == 9
    assert unit_tests(merged, "scripts.a.x_f__mutmut_2", 1) == {"left"}
    assert unit_tests(merged, "scripts.a.x_f__mutmut_2", 0) == {"right"}
    assert unit_tests(plan, "scripts.a.x_f__mutmut_2", 0) == {"early", "right"}
    assert probe.probe_results(plan) == ({}, 0)


@pytest.mark.parametrize(
    "fault",
    ["missing", "duplicate", "stale", "wrong_owner", "unknown", "timeout", "negative", "infinite", "wrong_total"],
)
def test_partial_stale_or_invalid_probe_results_fail_closed(fault):
    plan = _plan()
    records = _records(plan)
    if fault == "missing":
        records.pop()
    elif fault == "duplicate":
        records[1] = records[0]
    elif fault == "stale":
        records[0]["plan"] = "another revision"
    elif fault == "wrong_owner":
        records[0]["results"], records[1]["results"] = records[1]["results"], records[0]["results"]
    elif fault == "unknown":
        records[0]["results"] = {"scripts.a.x_other__mutmut_1": "killed"}
    elif fault == "timeout":
        records[0]["results"]["scripts.a.x_f__mutmut_1"] = "timeout"
    elif fault == "negative":
        records[0]["seconds"] = -1
    elif fault == "infinite":
        records[0]["seconds"] = float("inf")
    else:
        records[0]["total"] = 3
    with pytest.raises(ValueError):
        probe.merge_probes(plan, records)


def test_a_shared_probe_cannot_be_reused_after_its_plan_or_result_changes():
    plan = _plan()
    merged = probe.merge_probes(plan, _records(plan))
    merged["head"] = "new revision"
    with pytest.raises(ValueError):
        probe.probe_results(merged)
    merged = probe.merge_probes(plan, _records(plan))
    merged["probe"]["results"].pop("scripts.a.x_f__mutmut_1")
    with pytest.raises(ValueError):
        probe.probe_results(merged)


def test_the_plan_fingerprint_ignores_key_order_and_the_shared_probe():
    plan = _plan()
    reordered = dict(reversed(list(plan.items())))
    assert probe.plan_fingerprint(reordered) == probe.plan_fingerprint(plan)
    assert probe.plan_fingerprint({**plan, "probe": {"results": {}}}) == probe.plan_fingerprint(plan)
    assert probe.plan_fingerprint({**plan, "head": "other"}) != probe.plan_fingerprint(plan)


@pytest.mark.parametrize("shard", [(0, 3), (2, 2), (-1, 2)])
def test_an_early_shard_outside_the_measured_plan_is_refused(shard):
    with pytest.raises(ValueError, match="^early mutation shard does not match the measured plan$"):
        probe.probe_plan(_plan(), shard)


def test_an_early_plan_drops_the_shared_probe_of_its_source():
    plan = _plan()
    merged = probe.merge_probes(plan, _records(plan))
    early = probe.probe_plan(merged, (1, 2))
    assert "probe" not in early
    assert early == {**plan, "assignments": early["assignments"], "probe_mode": True}


@pytest.mark.parametrize("seconds", [-1, -0.5, float("inf"), float("nan")])
def test_shared_probe_seconds_must_be_finite_and_not_negative(seconds):
    plan = _plan()
    merged = probe.merge_probes(plan, _records(plan))
    merged["probe"]["seconds"] = seconds
    with pytest.raises(ValueError, match="^early mutation results are stale, incomplete or invalid$"):
        probe.probe_results(merged)


def test_a_probe_that_took_no_measurable_time_is_still_shared():
    plan = _plan()
    records = _records(plan)
    for record in records:
        record["seconds"] = 0
    merged = probe.merge_probes(plan, records)
    assert probe.probe_results(merged) == (merged["probe"]["results"], 0)


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("missing", "early mutation results are missing or duplicated"),
        ("duplicate", "early mutation results are missing or duplicated"),
        ("stale", "early mutation results do not match their revision, selection or shard"),
        ("negative", "early mutation results do not match their revision, selection or shard"),
    ],
)
def test_each_refused_probe_merge_names_its_cause(fault, message):
    plan = _plan()
    records = _records(plan)
    if fault == "missing":
        records.pop()
    elif fault == "duplicate":
        records[1] = records[0]
    elif fault == "stale":
        records[1]["plan"] = "another revision"
    else:
        records[1]["seconds"] = -0.5
    with pytest.raises(ValueError, match=f"^{message}$"):
        probe.merge_probes(plan, records)


def test_a_failed_early_worker_reports_its_reason_and_the_tail_of_its_log(tmp_path):
    assert str(probe.worker_failure(tmp_path, "over budget")) == "early mutation failed: over budget\n"
    (tmp_path / "run.log").write_text("a" * 5 + "b" * 12000)
    assert (
        str(probe.worker_failure(tmp_path, "report failed")) == f"early mutation failed: report failed\n{'b' * 12000}"
    )
