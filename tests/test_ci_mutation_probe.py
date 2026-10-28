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
