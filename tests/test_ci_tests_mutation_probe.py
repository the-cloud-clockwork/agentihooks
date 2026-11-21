from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def _jobs() -> dict[str, Any]:
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def test_tests_mutation_probes_split_mutants_before_sizing_shards_from_survivors() -> None:
    jobs = _jobs()
    refine, probe, replan, shard = (
        jobs["mutation-refine"],
        jobs["mutation-probe"],
        jobs["mutation-replan"],
        jobs["mutation-shard"],
    )
    assert refine["outputs"]["probe"] == "${{ steps.refine.outputs.probe }}"
    assert refine["outputs"]["probe_shards"] == "${{ steps.refine.outputs.probe_shards }}"
    assert probe["needs"] == ["mutation-plan", "mutation-refine"]
    assert probe["if"] == (
        "${{ !cancelled() && (needs.mutation-refine.result == 'success' || (needs.mutation-plan.result == 'success' "
        "&& needs.mutation-plan.outputs.collected == 'true')) && (needs.mutation-refine.outputs.probe || "
        "needs.mutation-plan.outputs.probe) == 'true' && (needs.mutation-refine.outputs.probe_shards || "
        "needs.mutation-plan.outputs.probe_shards) != '' }}"
    )
    assert probe["strategy"]["matrix"] == {
        "shard": "${{ fromJSON(needs.mutation-refine.outputs.probe_shards || needs.mutation-plan.outputs.probe_shards) }}"
    }
    probe_steps = {step.get("name"): step for step in probe["steps"]}
    early = probe_steps["Run each mutant's early tests once for every shard"]
    assert early["run"] == (
        'python -m scripts.ci_mutation.probe --bases "$BASES" --budget 1080 --shard "$SHARD" --shards "$SHARDS"'
        " --stats .mutation-stats"
    )
    assert early["env"]["SHARDS"] == "${{ strategy.job-total }}"
    assert probe_steps["Download the measured mutation plan"]["with"]["name"] == "mutation-measured-plan"
    assert probe["steps"][-1]["with"]["name"] == "mutation-probe-${{ matrix.shard }}"
    assert probe["steps"][-1]["with"]["path"] == ".mutation-stats/probe-${{ matrix.shard }}.json"
    assert replan["needs"] == ["mutation-refine", "mutation-probe"]
    assert replan["if"] == "${{ !cancelled() && needs.mutation-probe.result == 'success' }}"
    assert replan["outputs"] == {"shards": "${{ steps.replan.outputs.shards }}"}
    replan_steps = {step.get("name"): step for step in replan["steps"]}
    assert replan_steps["Download the early mutation results"]["with"] == {
        "pattern": "mutation-probe-*",
        "path": ".mutation-stats",
        "merge-multiple": True,
    }
    sized = replan_steps["Size the shards from the mutants the early tests left"]
    assert sized["run"] == "python -m scripts.ci_mutation.probed --stats .mutation-stats --budget 1080"
    assert replan_steps["Upload the probed mutation plan"]["with"]["name"] == "mutation-plan-probed"
    assert shard["needs"] == [
        "mutation-plan",
        "mutation-stats",
        "mutation-refine",
        "mutation-probe",
        "mutation-replan",
        "admission",
    ]
    assert shard["if"].endswith(
        "&& (needs.mutation-replan.result == 'success' || needs.mutation-probe.result == 'skipped') }}"
    )
    names = [step.get("name") for step in shard["steps"]]
    probed = shard["steps"][names.index("Download the probed mutation plan")]
    assert probed["if"] == "needs.mutation-replan.result == 'success'"
    assert probed["with"] == {"name": "mutation-plan-probed", "path": ".mutation-stats"}
    assert (
        names.index("Download the measured mutation plan")
        < names.index("Download the probed mutation plan")
        < names.index("Mutate changed Python files")
    )


def test_tests_mutation_joins_plan_with_the_planning_install_and_replan_checks_out_one_commit() -> None:
    jobs = _jobs()

    def install(name: str) -> dict[str, Any]:
        return next(step for step in jobs[name]["steps"] if step.get("name") == "Install dependencies")

    for name in ("mutation-refine", "mutation-replan"):
        assert install(name) == install("mutation-plan")
        assert "-e" not in install(name)["run"].split()
    assert jobs["mutation-replan"]["steps"][0]["with"] == {
        "fetch-depth": 1,
        "ref": "${{ github.event.pull_request.head.sha }}",
    }


def test_mutation_refine_checks_out_only_the_head_history_without_file_contents() -> None:
    steps = _jobs()["mutation-refine"]["steps"]
    checkouts = [step for step in steps if step.get("uses", "").startswith("actions/checkout")]
    assert checkouts
    for checkout in checkouts:
        assert checkout["with"] == {
            "fetch-depth": 2147483647,
            "filter": "blob:none",
            "ref": "${{ github.event.pull_request.head.sha }}",
        }


def test_every_mutation_refine_fetch_negotiates_from_the_head() -> None:
    fetches = [
        line.split()
        for step in _jobs()["mutation-refine"]["steps"]
        for line in step.get("run", "").splitlines()
        if {"git", "fetch"} <= set(line.split())
    ]
    assert fetches
    assert all("--negotiation-tip=HEAD" in words for words in fetches)
