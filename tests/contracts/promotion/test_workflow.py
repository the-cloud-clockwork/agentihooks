import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts import ci_budget
from tests.contracts.ci_tiers.test_workflow import enabled
from tests.test_ci_anton_runners import resolve
from tests.test_ci_proof_branch_pushes import _events, _runs_on

ROOT = Path(__file__).resolve().parents[3]


NON_GATING: dict[str, str] = {
    "access-audit.yml": "audits collaborators and installed apps and alerts only on unapproved or unreadable access",
    "ci-lab-image.yml": "builds, smokes and publishes the CI lab image",
    "count-record.yml": "records the dev suite test count that later pull request bases read",
    "coverage-stability.yml": "compares the measured lines of the newest dev commit's finished runs",
    "gate-required.yml": "follows every Tests run and reports Gate Required only for pull request runs",
    "github-access-audit.yml": "reads back the effective GitHub access controls",
    "hive-worker.yml": "proves the worker hive chart and image in kind and publishes the image",
    "swarm-image.yml": "builds and pushes the dev swarm image",
    "swarm-node-base.yml": "copies the locked worker base into the org registry",
    "swarm-node-image.yml": "builds, qualifies and publishes the worker image and moves its dev tag; a release tags it",
}


def test_promotion_requires_all_push_checks_and_never_runs_for_a_red_grade():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    promote = workflow["jobs"]["promote"]
    assert promote["uses"] == "./.github/workflows/promote.yml"
    assert {"gate-required", "refresh-durations", "coverage-baseline", "flake-budget"} <= set(promote["needs"])
    assert "github.event_name == 'push'" in promote["if"]
    assert "github.ref == 'refs/heads/dev'" in promote["if"]
    assert "needs.gate-required.result == 'success'" in promote["if"]
    assert "!contains(needs.*.result, 'failure')" in promote["if"]
    assert "!contains(needs.*.result, 'cancelled')" in promote["if"]


@pytest.mark.feature("promotion-graded-jobs-gate-or-exempt")
def test_every_graded_job_gates_promotion_or_sits_on_the_promote_script_exempt_list() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    script = json.dumps(str(ROOT / ".github/scripts/promote.cjs"))
    listed = subprocess.run(
        ["node", "-p", f"JSON.stringify(require({script}).EXEMPT)"], capture_output=True, text=True, check=True
    )
    exempt = set(json.loads(listed.stdout))
    gating = _needed(jobs, "promotion-ready")
    assert sorted(set(jobs) - gating - exempt - {"promotion-ready", "promote"}) == []
    assert sorted((exempt - set(jobs)) | (exempt & gating)) == []
    assert sorted(name for name in exempt if {"name", "strategy", "uses"} & set(jobs[name])) == []
    assert sorted(job for job, spec in jobs.items() if job not in exempt and spec.get("name") in exempt) == []


def _needed(jobs: dict[str, Any], name: str) -> set[str]:
    needs = jobs[name].get("needs", [])
    direct = {needs} if isinstance(needs, str) else set(needs)
    return direct.union(*(_needed(jobs, need) for need in direct))


@pytest.mark.feature("promotion-dev-push-workflows-non-gating")
def test_every_dev_push_workflow_outside_tests_sits_on_the_non_gating_list() -> None:
    workflows = {path.name: yaml.safe_load(path.read_text()) for path in (ROOT / ".github/workflows").glob("*.y*ml")}
    names = {file: spec.get("name", f".github/workflows/{file}") for file, spec in workflows.items()}
    triggers = {file: _events(spec) for file, spec in workflows.items()}
    fired = {file for file, events in triggers.items() if "push" in events and _runs_on(events["push"] or {}, "dev")}
    while (
        chained := {
            file
            for file, events in triggers.items()
            if (run := events.get("workflow_run"))
            and _runs_on(run, "dev")
            and {names[source] for source in fired} & set(run["workflows"])
        }
        - fired
    ):
        fired |= chained
    assert all(NON_GATING.values())
    assert sorted(fired - {"test.yml"}) == sorted(NON_GATING), (
        "a workflow that runs on a dev push outside the Tests run never gates promotion: "
        "move its gate into test.yml or list it in NON_GATING with the reason it does not gate"
    )


def test_each_dev_push_runs_the_hierarchy_proof_before_promotion_and_never_on_pull_requests_or_the_queue():
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    hierarchy = jobs["hierarchy-cpu"]
    assert hierarchy["uses"] == "./.github/workflows/hierarchy-cpu.yml"
    assert "hierarchy-cpu" in jobs["promote"]["needs"]
    assert "hierarchy-cpu" not in jobs["gate-required"]["needs"]
    assert enabled(hierarchy["if"], "push", {})
    for event in ("pull_request", "merge_group", "workflow_dispatch"):
        assert not enabled(hierarchy["if"], event, {})
    callee = yaml.safe_load((ROOT / ".github/workflows/hierarchy-cpu.yml").read_text())
    assert "workflow_call" in callee[True]
    push = {
        "event_name": "push",
        "ref": "refs/heads/dev",
        "actor": "github-merge-queue[bot]",
        "triggering_actor": "github-merge-queue[bot]",
        "repository": "the-cloud-clockwork/agentihooks",
    }
    for job in ("hierarchy", "load"):
        assert resolve(callee["jobs"][job]["runs-on"], **push) == "anton-oss-batch"


@pytest.mark.feature("tick-scale-gate-wiring")
def test_each_dev_push_runs_the_tick_scale_gate_before_promotion_and_never_on_pull_requests_or_the_queue() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    scale = jobs["tick-scale"]
    assert {"uses": "./.github/actions/tick-scale"} in scale["steps"]
    assert "tick-scale" not in jobs["gate-required"]["needs"]
    assert enabled(scale["if"], "push", {})
    for event in ("pull_request", "merge_group", "workflow_dispatch"):
        assert not enabled(scale["if"], event, {})
    for consumer in ("promotion-ready", "promote"):
        assert "tick-scale" in jobs[consumer]["needs"]
        for result in ("failure", "skipped", "cancelled"):
            needs = _graded_needs(jobs[consumer])
            needs["tick-scale"].result = result
            assert not enabled(jobs[consumer]["if"], "push", needs)
    action = yaml.safe_load((ROOT / ".github/actions/tick-scale/action.yml").read_text())
    steps = action["runs"]["steps"]
    assert any(str(step.get("run", "")).startswith("python -m tests.tick_scale.gate") for step in steps)
    plant = next(step for step in steps if step.get("run") == "git apply tests/tick_scale/whole_hash.patch")
    assert plant["if"] == "inputs.plant == 'true'"
    proofs = yaml.safe_load((ROOT / ".github/workflows/tick-scale.yml").read_text())
    assert proofs[True] == {"push": {"branches": ["diffcheck/tick-scale-*"]}, "workflow_dispatch": None}
    called = proofs["jobs"]["tick-scale"]["steps"][-1]
    assert called["uses"] == "./.github/actions/tick-scale"
    assert called["with"]["plant"] == "${{ github.ref == 'refs/heads/diffcheck/tick-scale-plant' }}"


@pytest.mark.feature("tick-scale-gate-wiring")
def test_tick_scale_profiles_registered_steps_including_the_agent_launcher() -> None:
    from scripts.tick.registry import steps

    proofs = yaml.safe_load((ROOT / ".github/workflows/tick-scale.yml").read_text())
    called = proofs["jobs"]["tick-scale"]["steps"][-1]
    names = set(called["with"]["profile"].split(" && ")[1].split(" || ")[0].strip("'").split(","))
    registered = {step.name for step in steps()}
    assert not names - registered, f"Unregistered profiled tick steps: {sorted(names - registered)}"
    assert "master scale" in names


@pytest.mark.feature("tick-scale-gate-wiring")
def test_tick_scale_promotion_page_lists_the_profiled_steps() -> None:
    import re

    proofs = yaml.safe_load((ROOT / ".github/workflows/tick-scale.yml").read_text())
    called = proofs["jobs"]["tick-scale"]["steps"][-1]
    names = set(called["with"]["profile"].split(" && ")[1].split(" || ")[0].strip("'").split(","))
    page = (ROOT / "docs/reference/delivery-tiers.md").read_text()
    described = page.split("On `diffcheck/tick-scale-profile`", 1)[1].split("with cProfile.", 1)[0]
    assert set(re.findall(r"`([^`]+)`", described)) == names
    assert "master scale" in names


def _graded_needs(promote: dict[str, Any], analysis: str = "A1") -> dict[str, SimpleNamespace]:
    return {
        name: SimpleNamespace(result="success", outputs=SimpleNamespace(analysis=analysis, promote="false"))
        for name in promote["needs"]
    }


@pytest.mark.parametrize("result", ["failure", "cancelled"])
@pytest.mark.parametrize(
    "job",
    [
        "gate-required",
        "refresh-durations",
        "coverage-baseline",
        "flake-budget",
        "sonar",
        "hierarchy-cpu",
        "tick-scale",
        "tick-pattern-tree",
    ],
)
def test_a_red_or_cancelled_promotion_dependency_prevents_all_promotion_writes(job, result):
    promote = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["promote"]
    needs = _graded_needs(promote)
    assert enabled(promote["if"], "push", needs)
    needs[job].result = result
    assert not enabled(promote["if"], "push", needs)


@pytest.mark.feature("launch-hotfix-promotion")
@pytest.mark.parametrize("job", ["gate-required", "coverage-baseline", "flake-budget", "sonar", "promotion-ready"])
def test_a_launch_hotfix_promotes_past_a_red_promotion_dependency_in_hotfix_mode(job: str) -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    promote = jobs["promote"]
    needs = _graded_needs(promote)
    needs[job].result = "failure"
    assert not enabled(promote["if"], "push", needs)
    needs["launch-hotfix"].outputs.promote = "true"
    assert enabled(promote["if"], "push", needs)
    assert not enabled(promote["if"], "pull_request", needs)
    assert "with" not in promote


@pytest.mark.feature("launch-hotfix-promotion")
def test_the_launch_hotfix_grade_reads_only_the_unit_suite_on_dev_pushes() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    grade = jobs["launch-hotfix"]
    assert grade["name"] == "Launch hotfix grade"
    assert set(grade["needs"]) == {"select", "split", "unit", "unit-lost", "unit-rerun", "shard-check", "test-count"}
    assert enabled(grade["if"], "push", {})
    for event in ("pull_request", "merge_group", "workflow_dispatch"):
        assert not enabled(grade["if"], event, {})
    assert "launch-hotfix" not in jobs["gate-required"]["needs"]
    assert grade["permissions"] == {"contents": "read"}
    token = next(step for step in grade["steps"] if step.get("id") == "app-token")["with"]
    assert {key: token[key] for key in token if key.startswith("permission-")} == {
        "permission-contents": "read",
        "permission-pull-requests": "read",
    }
    step, confirm = grade["steps"][-2:]
    assert step["id"] == "grade"
    assert step["run"].startswith("python -m scripts.ci_launch_hotfix ")
    assert step["env"] == {"NEEDS": "${{ toJSON(needs) }}", "GH_TOKEN": "${{ steps.app-token.outputs.token }}"}
    assert confirm["name"] == "Confirm the launch hotfix route"
    assert confirm["if"] == "steps.grade.outputs.promote == 'true'"
    script = (ROOT / ".github/scripts/promote.cjs").read_text()
    assert "const HOTFIX_STEP = 'Confirm the launch hotfix route';" in script
    assert "const HOTFIX_GRADE = 'Launch hotfix grade';" in script


@pytest.mark.parametrize("result", ["success", "failure", "skipped", "cancelled"])
def test_promotion_requires_a_successful_hierarchy_proof(result):
    promote = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["promote"]
    needs = _graded_needs(promote)
    needs["hierarchy-cpu"].result = result
    assert enabled(promote["if"], "push", needs) == (result == "success")


@pytest.mark.parametrize("result", ["success", "failure", "skipped", "cancelled"])
def test_promotion_requires_a_clean_whole_tree_tick_pattern(result: str) -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    tree = jobs["tick-pattern-tree"]
    assert any(step.get("run") == "python -m scripts.tick_pattern --tree" for step in tree["steps"])
    assert enabled(tree["if"], "push", {})
    for event in ("pull_request", "merge_group", "workflow_dispatch"):
        assert not enabled(tree["if"], event, {})
    assert "tick-pattern-tree" not in jobs["gate-required"]["needs"]
    for consumer in ("promotion-ready", "promote"):
        needs = _graded_needs(jobs[consumer])
        needs["tick-pattern-tree"].result = result
        assert enabled(jobs[consumer]["if"], "push", needs) == (result == "success")


@pytest.mark.parametrize("result", ["success", "failure", "skipped", "cancelled"])
def test_promotion_requires_a_clean_whole_tree_type_check(result: str) -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    tree = jobs["type-gate-tree"]
    assert any(step.get("run") == "python -m scripts.type_gate --tree" for step in tree["steps"])
    assert enabled(tree["if"], "push", {})
    for event in ("pull_request", "merge_group"):
        assert not enabled(tree["if"], event, {})
    assert enabled(tree["if"], "workflow_dispatch", {})
    assert "type-gate-tree" not in jobs["gate-required"]["needs"]
    for consumer in ("promotion-ready", "promote"):
        needs = _graded_needs(jobs[consumer])
        needs["type-gate-tree"].result = result
        assert enabled(jobs[consumer]["if"], "push", needs) == (result == "success")


def test_a_revision_sonar_graded_promotes_from_its_own_analysis():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    promote = workflow["jobs"]["promote"]
    assert "sonar" in promote["needs"]
    assert workflow["jobs"]["sonar"]["outputs"] == {"analysis": "${{ steps.gate.outputs.analysis }}"}
    assert enabled(promote["if"], "push", _graded_needs(promote))


def test_a_superseded_revision_sonar_never_graded_is_not_promoted():
    promote = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["promote"]
    assert not enabled(promote["if"], "push", _graded_needs(promote, analysis=""))


def test_promotion_reuses_the_release_environment_and_never_dispatches_or_publishes_to_pypi():
    workflow = yaml.safe_load((ROOT / ".github/workflows/promote.yml").read_text())
    assert "workflow_call" in workflow.get("on", workflow.get(True))
    job = workflow["jobs"]["promote"]
    assert job["environment"] == "release"
    assert job["timeout-minutes"] == 40
    assert ci_budget.BUDGETS["promote"] == 10 * 60
    assert job["concurrency"] == {"group": "release-version", "cancel-in-progress": False}
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "--generate-notes" in commands
    assert commands.index("ci_release.sh notes") < commands.index('--notes-file "$RUNNER_TEMP/notes.md"')
    assert "--verify-tag" in commands
    assert "workflow run" not in commands
    assert "publish-pypi" not in commands
    mint = next(step for step in job["steps"] if step.get("id") == "app-token")
    assert mint["with"]["permission-metadata"] == "read"
    assert mint["with"]["permission-contents"] == "write"
    assert mint["with"]["permission-pull-requests"] == "write"


def test_promotion_publishes_the_verified_tag_through_the_same_app_rest_token():
    workflow = yaml.safe_load((ROOT / ".github/workflows/promote.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["promote"]["steps"]
        if step.get("name") == "Tag and verify the promoted package"
    )
    assert step["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    commands = step["run"]
    assert commands.index("ci_release.sh tag") < commands.index("ci_release.sh verify") < commands.index("gh api")
    assert '"repos/$GITHUB_REPOSITORY/git/refs"' in commands
    assert "--method POST" in commands
    assert '-f "ref=refs/tags/v$NEXT"' in commands
    assert '-f "sha=$RELEASE_SHA"' in commands
    assert "git push" not in commands


def test_abandoned_promotion_cleanup_runs_last_as_the_app_even_after_a_failed_promotion():
    steps = yaml.safe_load((ROOT / ".github/workflows/promote.yml").read_text())["jobs"]["promote"]["steps"]
    cleanup = steps[-1]
    assert cleanup["if"] == "!cancelled() && steps.app-token.outcome == 'success'"
    assert cleanup["with"]["github-token"] == "${{ steps.app-token.outputs.token }}"
    assert cleanup["env"] == {"PROMOTION_APP": "${{ steps.app-token.outputs.app-slug }}"}
    assert (
        "closeAbandoned({ github, context, core, app: process.env.PROMOTION_APP, deadline: Date.now() + 180000 })"
        in cleanup["with"]["script"]
    )
