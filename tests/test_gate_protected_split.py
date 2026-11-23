"""The protected grader's acceptance of the Gate Required aggregation that requires the collect and lint-actions jobs."""

import copy
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_gate_queue, ci_reuse, gate_protected
from scripts.ci_branch_lanes import GROUP

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]

Workflow = dict[Any, Any]
Needs = dict[str, dict[str, Any]]
STEPS = "The head's Gate — Required changes its steps; the protected branch grades with its own."
ALONE = "The head's Gate — Required needs {} without {}; the split jobs are required together."


def _workflow(steps: list[dict[str, Any]] | None = None) -> Workflow:
    return {
        "concurrency": {"group": GROUP, "queue": "max", "cancel-in-progress": False},
        True: {"pull_request": {"branches": ["dev"]}, "merge_group": None, "push": {"branches": ["dev"]}},
        "jobs": {
            "unit": {"runs-on": "ubuntu-latest"},
            "lint": {"runs-on": "ubuntu-latest"},
            "wiring": {"runs-on": "ubuntu-latest"},
            "gate-required": {
                "name": "Gate — Required",
                "needs": ["unit", "lint", "wiring"],
                "if": "${{ always() }}",
                "steps": steps or ci_reuse.gate_steps(queue_tier=True, consolidated=True, promotion=True),
            },
        },
    }


def _passes(run: str, event: str, needs: Needs) -> bool:
    env = dict(os.environ, EVENT=event, NEEDS=json.dumps(needs), MUTATION="false", REUSED="false", KIND="false")
    return subprocess.run(["bash", "-e", "-c", run], env=env, capture_output=True, text=True).returncode == 0


@pytest.mark.parametrize(
    "jobs,wired,steps,refusal",
    [
        (("collect", "lint-actions"), True, "split", None),
        (("collect", "lint-actions"), True, "unchanged", STEPS),
        (("collect", "lint-actions"), True, "weakened", STEPS),
        (("collect", "lint-actions"), False, "split", STEPS),
        (("collect",), True, "split", ALONE.format("collect", "lint-actions")),
        (("collect",), True, "unchanged", ALONE.format("collect", "lint-actions")),
        (("lint-actions",), True, "unchanged", ALONE.format("lint-actions", "collect")),
    ],
    ids=["canonical", "unchanged", "weakened", "unwired", "one job", "collect alone", "lint-actions alone"],
)
@pytest.mark.feature("gate-required-split-jobs")
def test_split_admission_requires_the_exact_reviewed_aggregation_and_both_jobs(
    jobs: tuple[str, ...], wired: bool, steps: str, refusal: str | None
) -> None:
    base, head = _workflow(), _workflow()
    gate = head["jobs"]["gate-required"]
    for job in jobs:
        head["jobs"][job] = {"if": "github.event_name == 'pull_request'", "runs-on": "ubuntu-latest"}
    if wired:
        gate["needs"] += jobs
    if steps != "unchanged":
        gate["steps"] = ci_reuse.split_required(gate["steps"])
    if steps == "weakened":
        run = gate["steps"][0]["run"].replace('"lint-actions", ', "")
        assert run != gate["steps"][0]["run"]
        gate["steps"][0] = {**gate["steps"][0], "run": run}
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {})
    if refusal is None:
        assert problems == []
    else:
        assert refusal in problems


@pytest.mark.parametrize(
    "form", [{}, {"queue_tier": True, "consolidated": True}], ids=["no job lists", "no dev push skip list"]
)
@pytest.mark.feature("gate-required-split-jobs")
def test_split_admission_refuses_both_jobs_over_an_aggregation_without_the_three_job_lists(
    form: dict[str, bool],
) -> None:
    base, head = _workflow(ci_reuse.gate_steps(**form)), _workflow(ci_reuse.gate_steps(**form))
    for job in ("collect", "lint-actions"):
        head["jobs"][job] = {"if": "github.event_name == 'pull_request'", "runs-on": "ubuntu-latest"}
    head["jobs"]["gate-required"]["needs"] += ["collect", "lint-actions"]
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": head}, {})
    assert STEPS in problems


@pytest.mark.parametrize("job", ["collect", "lint-actions"])
@pytest.mark.feature("gate-required-split-jobs")
def test_the_repository_gate_refuses_one_split_job_through_the_early_queue_gate(job: str) -> None:
    base = {path.name: yaml.safe_load(path.read_text()) for path in (ROOT / ".github/workflows").glob("*.yml")}
    head = copy.deepcopy(base)
    gate = head["test.yml"]["jobs"]["gate-required"]
    gate["needs"] = sorted(set(gate["needs"]) - ci_gate_queue.SPLIT | {job})
    problems = gate_protected.grade(base, {}, head, {})
    assert problems == ["The early queue gate must preserve the reviewed graded inventory"]


@pytest.mark.parametrize("job", ["collect", "lint-actions"])
@pytest.mark.parametrize("status", ["success", "failure", "cancelled", "skipped", "missing"])
@pytest.mark.feature("gate-required-split-jobs")
def test_split_aggregation_requires_both_jobs_to_succeed_on_pull_requests(job: str, status: str) -> None:
    [step] = ci_reuse.split_required(ci_reuse.gate_steps(queue_tier=True, consolidated=True, promotion=True))
    required = ["select", "collect", "test-count", "lint", "lint-actions", "semgrep", "sonar"]
    needs: Needs = {name: {"result": "success"} for name in required}
    needs["select"]["outputs"] = {"scope": "none"}
    if status == "missing":
        del needs[job]
    else:
        needs[job] = {"result": status}
    assert _passes(step["run"], "pull_request", needs) == (status == "success")


@pytest.mark.parametrize(
    "event,collect,actions,expected",
    [
        ("merge_group", "success", "skipped", True),
        ("merge_group", "skipped", "skipped", False),
        ("merge_group", "failure", "skipped", False),
        ("merge_group", "success", "success", False),
        ("push", "skipped", "skipped", True),
        ("push", "failure", "skipped", False),
        ("push", "skipped", "failure", False),
    ],
)
@pytest.mark.feature("gate-required-split-jobs")
def test_split_aggregation_requires_collection_on_the_queue_and_skips_both_jobs_on_promotion(
    event: str, collect: str, actions: str, expected: bool
) -> None:
    [step] = ci_reuse.split_required(
        ci_reuse.lost_rerun(ci_reuse.gate_steps(queue_tier=True, consolidated=True, promotion=True, watcher_loss=True))
    )
    required = ["select", "split", "shard-check", "test-count"] if event == "merge_group" else ["select"]
    needs: Needs = {name: {"result": "success"} for name in required}
    needs["select"]["outputs"] = {"scope": "full"}
    if event == "merge_group":
        needs.update(
            {
                "unit": {"result": "success"},
                "unit-lost": {"result": "skipped"},
                "unit-rerun": {"result": "skipped"},
                "queue-fail-fast": {"result": "success"},
            }
        )
    needs["collect"] = {"result": collect}
    needs["lint-actions"] = {"result": actions}
    assert _passes(step["run"], event, needs) == expected


@pytest.mark.parametrize(
    "form,repeat",
    [
        ({}, 1),
        ({"queue_tier": True, "consolidated": True}, 1),
        ({"queue_tier": True, "consolidated": True, "promotion": True}, 2),
    ],
    ids=["no job lists", "no dev push skip list", "job lists twice"],
)
@pytest.mark.feature("gate-required-split-jobs")
def test_split_aggregation_refuses_a_rendering_that_does_not_hold_each_job_list_once(
    form: dict[str, bool], repeat: int
) -> None:
    [step] = ci_reuse.gate_steps(**form)
    with pytest.raises(
        ValueError, match="^The split needs an aggregation that holds each of its three job lists once$"
    ):
        ci_reuse.split_required([{**step, "run": step["run"] * repeat}])


@pytest.mark.feature("gate-required-split-jobs")
def test_split_aggregation_adds_exactly_the_split_jobs_of_the_queue_gate() -> None:
    names = [set(re.findall(r'"([a-z-]+)"', text)) for pair in ci_reuse.SPLIT_REQUIRED for text in pair]
    assert set().union(*names[1::2]) - set().union(*names[0::2]) == ci_gate_queue.SPLIT


@pytest.mark.parametrize(
    "needs,postgres,asked",
    [
        ((), False, []),
        (("docs",), False, [(False, False, True, False)]),
        (("type-gate",), False, [(True, False, False, False)]),
        (("collect", "lint-actions"), False, [(False, False, False, True)]),
        (("docs", "type-gate", "collect", "lint-actions"), True, [(True, True, True, True)]),
    ],
    ids=["no forcing need", "docs", "type gate", "split jobs", "every need over postgres"],
)
@pytest.mark.feature("gate-required-split-jobs")
def test_an_unchanged_gate_is_graded_against_the_renderings_of_its_own_needs(
    monkeypatch: pytest.MonkeyPatch, needs: tuple[str, ...], postgres: bool, asked: list[tuple[bool, ...]]
) -> None:
    steps = ci_reuse.gate_steps(queue_tier=True, consolidated=True, promotion=True)
    base = _workflow(ci_reuse.postgres_required(steps) if postgres else steps)
    for job in needs:
        base["jobs"][job] = {"if": "github.event_name == 'pull_request'", "runs-on": "ubuntu-latest"}
    base["jobs"]["gate-required"]["needs"] += needs
    calls: list[tuple[bool, ...]] = []

    def none_accepted(*combination: bool) -> list[list[dict[str, Any]]]:
        calls.append(combination)
        return []

    monkeypatch.setattr(gate_protected, "_accepted_steps_renderings", none_accepted)
    problems = gate_protected.grade({"test.yml": base}, {}, {"test.yml": copy.deepcopy(base)}, {})
    assert calls == asked
    assert problems == ([STEPS] if asked else [])


@pytest.mark.feature("gate-required-split-jobs")
def test_the_split_renderings_are_the_unsplit_ones_of_the_same_gate_that_hold_the_job_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = ci_reuse.gate_steps(queue_tier=True, consolidated=True, promotion=True)
    asked: list[tuple[bool, ...]] = []

    def unsplit(*combination: bool) -> list[list[dict[str, Any]]]:
        asked.append(combination)
        return [listed, ci_reuse.gate_steps()]

    renderings = gate_protected._accepted_steps_renderings
    monkeypatch.setattr(gate_protected, "_accepted_steps_renderings", unsplit)
    renderings.cache_clear()
    try:
        assert renderings(True, True, True, True) == [ci_reuse.split_required(listed)]
    finally:
        renderings.cache_clear()
    assert asked == [(True, True, True, False)]
