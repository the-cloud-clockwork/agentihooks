import json
from pathlib import Path
from unittest import mock

import pytest

from scripts.swarm_v2 import resource_profiles as profiles
from tests import sv2_prf01_cases as cases

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-PRF-01"
POLICY = profiles.Policy(margin=0.25, min_repeats=2, agent_reserve_mib=512, base_storage_mib=1024)
TOOLS = {"python": "3.12.1", "workload:memory": "1"}


def run(cls, children, repeat, rss, **extra):
    cores = extra.get("cores", 1.0)
    return {
        "workload": cls,
        "class": cls,
        "children": children,
        "repeat": repeat,
        "peak_rss_mib": rss,
        "peak_pss_mib": extra.get("pss", rss * 0.8),
        "peak_parent_rss_mib": 30,
        "peak_children_rss_mib": rss - 30,
        "peak_cores": cores,
        "peak_parent_cores": 0.1,
        "peak_children_cores": cores - 0.1,
        "cpu_seconds": cores * 2,
        "parent_cpu_seconds": 0.2,
        "children_cpu_seconds": cores * 2 - 0.2,
        "wall_seconds": 3.0,
        "peak_storage_mib": extra.get("storage", 10.0),
        "peak_processes": extra.get("processes", children + 1),
        "peak_children_processes": extra.get("processes", children + 1) - 1,
        "samples": 30,
        "exit_code": extra.get("exit_code", 0),
        "timed_out": extra.get("timed_out", False),
    }


def memory_runs(scale=1.0):
    return [run("memory", c, r, rss=(100 + 200 * c + 10 * r) * scale, cores=0.5 * c) for c in (1, 2) for r in (1, 2)]


def measured(runs, generation=1, tools=None):
    return {
        "suite": "fixture",
        "generation": generation,
        "source": "fixture run",
        "tools": tools or TOOLS,
        "host": {"cpus": 4},
        "runs": runs,
    }


NODES = [
    {
        "name": "node-a",
        "allocatable_memory_mib": 16000,
        "allocatable_cpu_millis": 4000,
        "allocatable_ephemeral_mib": 90000,
        "colocated_memory_mib": 2000,
        "colocated_cpu_millis": 500,
        "colocated_pods": 6,
    }
]


def test_distribution_uses_nearest_rank_percentiles():
    assert profiles.distribution(list(range(10, 0, -1))) == {"n": 10, "min": 1, "p50": 5, "p95": 10, "max": 10}
    assert profiles.distribution([7.25]) == {"n": 1, "min": 7.25, "p50": 7.25, "p95": 7.25, "max": 7.25}


def test_round_up_reaches_the_next_step():
    assert [profiles.round_up(v, 256) for v in (0, 1, 256, 257)] == [256, 256, 256, 512]


def test_admission_refuses_idle_childless_short_and_failed_classes():
    runs = [
        *memory_runs(),
        run("idle", 0, 1, 40, processes=1),
        run("idle", 0, 2, 41, processes=1),
        run("general", 0, 1, 300, processes=1),
        run("general", 0, 2, 310, processes=1),
        run("compiler", 2, 1, 500),
        run("browser", 2, 1, 900),
        run("browser", 2, 2, 950, exit_code=1),
    ]
    admitted, refused = profiles.admitted(runs, POLICY)
    assert sorted(admitted) == ["memory"]
    assert refused == {
        "browser": "a run failed or timed out",
        "compiler": "fewer than 2 repeats at a child count",
        "general": "no child activity",
        "idle": "an idle sample cannot size a worker",
    }


def test_admission_refuses_a_timed_out_run():
    _, refused = profiles.admitted([*memory_runs()[:3], {**memory_runs()[3], "timed_out": True}], POLICY)
    assert refused == {"memory": "a run failed or timed out"}


def test_profile_takes_the_worst_measured_run_plus_reserve_and_margin():
    built = profiles.profile(memory_runs(), POLICY)
    assert built["limits"] == {"memory_mib": 1536, "cpu_millis": 1250, "ephemeral_mib": 2048}
    assert built["requests"] == {"memory_mib": 1280, "cpu_millis": 500}
    assert built["measured"]["peak_rss_mib"] == {"n": 4, "min": 310, "p50": 320, "p95": 520, "max": 520}
    assert built["measured"]["peak_processes"]["max"] == 3
    assert built["uncertainty"] == {
        "samples": 4,
        "children": [1, 2],
        "relative_spread": 0.65625,
        "margin": 0.25,
        "declared": {"agent_reserve_mib": 512, "base_storage_mib": 1024},
    }


def test_coexistence_counts_whole_workers_in_free_node_capacity():
    built = {"memory": profiles.profile(memory_runs(), POLICY)}
    assert profiles.coexistence(built, NODES) == [
        {
            "node": "node-a",
            "free_memory_mib": 14000,
            "free_cpu_millis": 3500,
            "fits": {"memory": 7},
            "all_profiles_together": True,
        }
    ]


def test_coexistence_reports_a_node_that_cannot_hold_every_profile_at_once():
    built = {
        "memory": profiles.profile(memory_runs(), POLICY),
        "big": {
            "limits": {"memory_mib": 14000, "cpu_millis": 2000, "ephemeral_mib": 1024},
            "requests": {"memory_mib": 14000, "cpu_millis": 1000},
        },
    }
    (row,) = profiles.coexistence(built, NODES)
    assert row["fits"] == {"big": 1, "memory": 7}
    assert row["all_profiles_together"] is False


def test_coexistence_never_reports_a_negative_fit():
    full = [{**NODES[0], "colocated_memory_mib": 17000}]
    (row,) = profiles.coexistence({"memory": profiles.profile(memory_runs(), POLICY)}, full)
    assert row["free_memory_mib"] == -1000 and row["fits"] == {"memory": 0}


def test_derive_reports_profiles_refusals_coexistence_and_the_peak_measurement():
    runs = [*memory_runs(), run("idle", 0, 1, 40, processes=1)]
    report = profiles.derive(measured(runs), NODES, POLICY)
    assert report["generation"] == 1
    assert sorted(report["profiles"]) == ["memory"]
    assert report["refused"] == {"idle": "an idle sample cannot size a worker"}
    assert report["coexistence"][0]["fits"] == {"memory": 7}
    assert report["worker_process_tree_peak_memory"] == {"idle": 40, "memory": 520}
    assert report["tools"] == TOOLS
    assert report["policy"]["margin"] == 0.25


def test_derive_without_nodes_leaves_coexistence_unmeasured():
    report = profiles.derive(measured(memory_runs()), [], POLICY)
    assert report["coexistence"] == "unmeasured: no node capacity was recorded"


def test_an_idle_cli_sample_cannot_justify_a_concurrency_claim():
    idle = measured([run("idle", 0, r, 45, processes=1) for r in (1, 2, 3)])
    with pytest.raises(profiles.ClaimRefused) as refused:
        profiles.derive(idle, NODES, POLICY)
    assert refused.value.refused == {"idle": "an idle sample cannot size a worker"}
    assert "no class was admitted" in str(refused.value)


def test_compare_flags_material_changes_missing_classes_and_tool_changes():
    accepted = profiles.derive(measured(memory_runs()), NODES, POLICY)
    grown = profiles.derive(measured(memory_runs(1.5), 2, {**TOOLS, "workload:memory": "2"}), NODES, POLICY)
    changes = profiles.compare(accepted, grown, POLICY)
    assert changes["tools_changed"] == {"workload:memory": ["1", "2"]}
    assert changes["missing"] == []
    assert ["memory", "peak_rss_mib", 520, 780] in changes["material"]
    other = {**accepted, "profiles": {}}
    assert profiles.compare(accepted, other, POLICY)["missing"] == ["memory"]


def test_compare_ignores_changes_within_tolerance():
    accepted = profiles.derive(measured(memory_runs()), NODES, POLICY)
    near = profiles.derive(measured(memory_runs(1.05), 2), NODES, POLICY)
    assert profiles.compare(accepted, near, POLICY) == {"material": [], "missing": [], "tools_changed": {}}


def test_promote_decides_first_replay_stale_conflict_requalify_and_upgrade():
    accepted = profiles.derive(measured(memory_runs()), NODES, POLICY)
    first = profiles.promote(None, accepted, POLICY, "")
    assert first == {"decision": "promoted", "accepted": accepted, "changes": None}
    replay = json.loads(json.dumps(accepted))
    assert profiles.promote(accepted, replay, POLICY, "") == {
        "decision": "replayed",
        "accepted": accepted,
        "changes": None,
    }
    stale = profiles.derive(measured(memory_runs(), 0), NODES, POLICY)
    assert profiles.promote(accepted, stale, POLICY, "op") == {
        "decision": "stale",
        "accepted": accepted,
        "changes": None,
    }
    clash = profiles.derive(measured(memory_runs(2.0), 1), NODES, POLICY)
    assert profiles.promote(accepted, clash, POLICY, "op") == {
        "decision": "conflict",
        "accepted": accepted,
        "changes": None,
    }
    grown = profiles.derive(measured(memory_runs(1.5), 2, {**TOOLS, "workload:memory": "2"}), NODES, POLICY)
    changes = profiles.compare(accepted, grown, POLICY)
    held = profiles.promote(accepted, grown, POLICY, "")
    assert held == {"decision": "requalify", "accepted": accepted, "changes": changes}
    assert held["accepted"] is accepted
    upgraded = profiles.derive(measured(memory_runs(), 3, {**TOOLS, "python": "3.12.2"}), NODES, POLICY)
    tools_only = profiles.promote(accepted, upgraded, POLICY, "")
    assert tools_only["decision"] == "requalify" and tools_only["accepted"] is accepted
    assert tools_only["changes"] == {"material": [], "missing": [], "tools_changed": {"python": ["3.12.1", "3.12.2"]}}
    same = profiles.derive(measured(memory_runs(1.05), 4), NODES, POLICY)
    assert profiles.promote(accepted, same, POLICY, "") == {
        "decision": "promoted",
        "accepted": same,
        "changes": {"material": [], "missing": [], "tools_changed": {}},
    }


def test_a_named_reviewer_requalifies_a_held_report():
    accepted = profiles.derive(measured(memory_runs()), NODES, POLICY)
    grown = profiles.derive(measured(memory_runs(1.5), 2), NODES, POLICY)
    outcome = profiles.promote(accepted, grown, POLICY, " operator ")
    assert outcome == {
        "decision": "requalified",
        "accepted": {**grown, "requalified_by": "operator"},
        "changes": {**profiles.compare(accepted, grown, POLICY), "requalified_by": "operator"},
    }
    blank = profiles.promote(accepted, grown, POLICY, "  ")
    assert blank["decision"] == "requalify" and blank["accepted"] is accepted
    replay = profiles.promote(outcome["accepted"], grown, POLICY, "")
    assert replay["decision"] == "replayed"


def test_a_candidate_sizing_an_unmeasured_class_is_refused(tmp_path):
    accepted = profiles.derive(measured(memory_runs()), NODES, POLICY)
    path = tmp_path / "accepted.json"
    profiles.accept(path, accepted, POLICY, "")
    before = path.read_bytes()
    forged = profiles.derive(measured(memory_runs(), 2), NODES, POLICY)
    forged["profiles"]["idle"] = forged["profiles"]["memory"]
    outcome = profiles.accept(path, forged, POLICY, "operator")
    assert outcome == {"decision": "refused", "accepted": accepted, "changes": {"unsized": ["idle"]}}
    assert path.read_bytes() == before
    assert profiles.promote(None, forged, POLICY, "")["decision"] == "refused"


def test_accept_writes_only_a_promoted_report_and_rollback_restores_the_one_it_replaced(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    path = folder / "accepted.json"
    assert profiles.rollback(path) is None
    accepted = profiles.derive(measured(memory_runs()), NODES, POLICY)
    assert profiles.accept(path, accepted, POLICY, "")["decision"] == "promoted"
    assert path.read_text() == json.dumps(accepted, indent=2, sort_keys=True) + "\n"
    assert sorted(p.name for p in folder.iterdir()) == ["accepted.json"]
    before = path.read_bytes()
    grown = profiles.derive(measured(memory_runs(1.5), 2), NODES, POLICY)
    assert profiles.accept(path, grown, POLICY, "")["decision"] == "requalify"
    assert profiles.accept(path, accepted, POLICY, "")["decision"] == "replayed"
    assert path.read_bytes() == before
    assert sorted(p.name for p in folder.iterdir()) == ["accepted.json"]
    assert (
        profiles.accept(
            path, grown, POLICY, "operator", reviewer=profiles.Principal("operator", profiles.Role.OPERATOR)
        )["decision"]
        == "requalified"
    )
    installed = path.read_bytes()
    assert json.loads(installed) == {**grown, "requalified_by": "operator"}
    assert (folder / "accepted.json.previous").read_bytes() == before
    assert profiles.rollback(path) == 1
    assert path.read_bytes() == before
    assert (folder / "accepted.json.previous").read_bytes() == installed
    assert sorted(p.name for p in folder.iterdir()) == ["accepted.json", "accepted.json.previous"]
    assert profiles.rollback(path) == 2
    assert path.read_bytes() == installed


@pytest.mark.parametrize(
    "role,name,decision",
    [
        (profiles.Role.MASTER, "operator", "requalify"),
        (profiles.Role.OPERATOR, "other", "requalify"),
        (profiles.Role.OPERATOR, "operator", "requalified"),
    ],
)
def test_accept_checks_the_reviewer_role_and_identity(tmp_path, role, name, decision):
    path = tmp_path / "accepted.json"
    profiles.accept(path, profiles.derive(measured(memory_runs()), NODES, POLICY), POLICY, "")
    candidate = profiles.derive(measured(memory_runs(2), 2), NODES, POLICY)
    outcome = profiles.accept(path, candidate, POLICY, "operator", reviewer=profiles.Principal(name, role))
    assert outcome["decision"] == decision
    assert json.loads(path.read_text())["generation"] == (2 if decision == "requalified" else 1)


def test_accept_rederives_profiles_instead_of_installing_idle_figures_under_a_real_class(tmp_path):
    candidate = profiles.derive(measured(memory_runs()), NODES, POLICY)
    candidate["profiles"]["memory"]["measured"]["peak_rss_mib"]["max"] = 40
    path = tmp_path / "accepted.json"
    outcome = profiles.accept(path, candidate, POLICY, "operator")
    assert outcome["decision"] == "refused"
    assert not path.exists()


@pytest.mark.parametrize("tamper", ["digest", "runs", "missing", "malformed"])
def test_accept_requires_the_recorded_run_digest_and_reproducible_evidence(tmp_path, tamper):
    path = tmp_path / "accepted.json"
    profiles.accept(path, profiles.derive(measured(memory_runs()), NODES, POLICY), POLICY, "")
    before = path.read_bytes()
    candidate = profiles.derive(measured(memory_runs(2), 2), NODES, POLICY)
    if tamper == "digest":
        candidate["runs_digest"] = "wrong"
    elif tamper == "runs":
        for row in candidate["evidence"]["measurements"]["runs"]:
            row["children"] = 0
            row["peak_processes"] = 1
    elif tamper == "missing":
        del candidate["evidence"]
    else:
        candidate["evidence"] = None
    outcome = profiles.accept(
        path, candidate, POLICY, "operator", reviewer=profiles.Principal("operator", profiles.Role.OPERATOR)
    )
    assert outcome == {
        "decision": "refused",
        "accepted": json.loads(before),
        "changes": {"evidence": "candidate does not match its recorded runs"},
    }
    assert path.read_bytes() == before
    assert not path.with_name("accepted.json.previous").exists()


def test_accept_installs_recomputed_output_without_a_self_declared_reviewer(tmp_path):
    expected = profiles.derive(measured(memory_runs()), NODES, POLICY)
    candidate = {**expected, "requalified_by": "untrusted"}
    path = tmp_path / "accepted.json"
    outcome = profiles.accept(path, candidate, POLICY, "")
    assert outcome == {"decision": "promoted", "accepted": expected, "changes": None}
    assert json.loads(path.read_text()) == expected


def test_accept_preserves_provenance_across_json_key_order(tmp_path):
    source = measured(memory_runs())
    candidate = profiles.derive(source, NODES, POLICY)
    reordered_source = json.loads(json.dumps(source, sort_keys=True))
    assert profiles.derive(reordered_source, NODES, POLICY)["runs_digest"] == candidate["runs_digest"]
    candidate = json.loads(json.dumps(candidate, sort_keys=True))
    outcome = profiles.accept(tmp_path / "accepted.json", candidate, POLICY, "")
    assert outcome["decision"] == "promoted"
    assert outcome["accepted"] == candidate


def test_derived_evidence_is_detached_and_digest_changes_with_runs():
    source = measured(memory_runs())
    report = profiles.derive(source, NODES, POLICY)
    snapshot = json.loads(json.dumps(report["evidence"]))
    source["runs"][0]["peak_rss_mib"] += 1
    assert report["evidence"] == snapshot
    assert profiles.derive(source, NODES, POLICY)["runs_digest"] != report["runs_digest"]
    assert len(report["runs_digest"]) == 64


def test_accept_does_not_trust_a_requalifying_reviewer_name(tmp_path):
    path = tmp_path / "accepted.json"
    accepted = profiles.derive(measured(memory_runs()), NODES, POLICY)
    candidate = profiles.derive(measured(memory_runs(2), 2), NODES, POLICY)
    profiles.accept(path, accepted, POLICY, "")
    before = path.read_bytes()
    outcome = profiles.accept(path, candidate, POLICY, "operator")
    assert outcome["decision"] == "requalify"
    assert path.read_bytes() == before
    assert not path.with_name("accepted.json.previous").exists()


def test_an_interrupted_rollback_keeps_both_reports_on_disk(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    path = folder / "accepted.json"
    accepted = profiles.derive(measured(memory_runs()), NODES, POLICY)
    grown = profiles.derive(measured(memory_runs(1.5), 2), NODES, POLICY)
    profiles.accept(path, accepted, POLICY, "")
    profiles.accept(path, grown, POLICY, "operator", reviewer=profiles.Principal("operator", profiles.Role.OPERATOR))
    reports = {p.read_bytes() for p in folder.iterdir()}
    real = Path.replace
    calls = []

    def stop_after_first(self, target):
        calls.append(self.name)
        if len(calls) > 1:
            raise OSError("interrupted")
        return real(self, target)

    with mock.patch.object(Path, "replace", stop_after_first), pytest.raises(OSError):
        profiles.rollback(path)
    assert calls == ["accepted.json", "accepted.json.previous"]
    assert {p.read_bytes() for p in folder.iterdir()} == reports


def test_markdown_names_every_profile_refusal_and_node():
    report = profiles.derive(measured([*memory_runs(), run("idle", 0, 1, 40, processes=1)]), NODES, POLICY)
    text = profiles.markdown(report)
    assert "| memory | 1536 | 1250 | 2048 |" in text
    assert "idle: an idle sample cannot size a worker" in text
    assert "| node-a | 14000 | 3500 | memory 7 | yes |" in text


@pytest.mark.parametrize(
    ("children", "processes", "reason"),
    [(1, 2, None), (2, 3, None), (0, 2, profiles.CHILDLESS_REFUSAL), (1, 1, profiles.CHILDLESS_REFUSAL)],
)
def test_child_activity_needs_children_and_more_than_one_process(children, processes, reason):
    runs = [run("memory", children, r, 100, processes=processes) for r in (1, 2)]
    assert profiles._refusal("memory", runs, POLICY) == reason


def test_coexistence_holds_profiles_that_exactly_fill_a_node():
    half = {
        "limits": {"memory_mib": 7000, "cpu_millis": 4000, "ephemeral_mib": 1024},
        "requests": {"memory_mib": 7000, "cpu_millis": 1750},
    }
    (row,) = profiles.coexistence({"a": half, "b": half}, NODES)
    assert row["fits"] == {"a": 2, "b": 2} and row["all_profiles_together"] is True


def test_refusal_names_every_refused_class():
    runs = [run("idle", 0, 1, 40, processes=1), run("general", 0, 1, 90, processes=1)]
    with pytest.raises(profiles.ClaimRefused) as refused:
        profiles.derive(measured(runs), NODES, POLICY)
    assert (
        str(refused.value)
        == "no class was admitted: general: no child activity; idle: an idle sample cannot size a worker"
    )


def test_compare_keeps_reading_classes_after_a_missing_one():
    compiler = [run("compiler", c, r, 200) for c in (1, 2) for r in (1, 2)]
    accepted = profiles.derive(measured([*compiler, *memory_runs()]), NODES, POLICY)
    candidate = profiles.derive(measured(memory_runs(1.5), 2), NODES, POLICY)
    changes = profiles.compare(accepted, candidate, POLICY)
    assert changes["missing"] == ["compiler"]
    assert changes["material"] == [["memory", "peak_rss_mib", 520, 780]]


def test_compare_treats_a_change_of_exactly_the_tolerance_as_immaterial():
    loose = profiles.Policy(margin=0.25, min_repeats=2, agent_reserve_mib=512, base_storage_mib=1024, tolerance=0.25)
    accepted = profiles.derive(measured(memory_runs()), NODES, loose)
    edge = profiles.derive(measured(memory_runs(1.25), 2), NODES, loose)
    assert profiles.compare(accepted, edge, loose)["material"] == []


def test_compare_reports_a_tool_only_the_candidate_has():
    accepted = profiles.derive(measured(memory_runs()), NODES, POLICY)
    candidate = profiles.derive(measured(memory_runs(), 2, {**TOOLS, "cc": "13"}), NODES, POLICY)
    assert profiles.compare(accepted, candidate, POLICY)["tools_changed"] == {"cc": [None, "13"]}


def test_markdown_marks_an_unrecorded_source_no_refusals_and_unmeasured_coexistence():
    report = profiles.derive({**measured(memory_runs()), "source": ""}, [], POLICY)
    text = profiles.markdown(report)
    assert "Source: not recorded." in text
    assert "## Refused\n\n- none\n" in text
    assert "## Coexistence\n\nunmeasured: no node capacity was recorded\n" in text
    assert "## Limits of this measurement" not in text
    assert text.endswith("\n") and "- tool python: 3.12.1" in text


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)


def test_committed_results_page_matches_the_measured_fixture():
    assert cases.results_page() == (Path(__file__).parents[1] / "docs" / "swarm-v2" / "resource-results.md").read_text()
