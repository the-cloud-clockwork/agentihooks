from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/mutation-preflight.yml").read_text())


def test_preflight_runs_on_task_branch_pushes_only():
    workflow = _workflow()
    events = workflow[True]
    assert set(events) in ({"push"}, {"push", "workflow_call"})
    assert events["push"] == {"branches-ignore": ["dev", "main", "gh-readonly-queue/**", "wip/**"]}
    if "workflow_call" in events:
        assert events["workflow_call"]["inputs"]["base"] == {"type": "string", "required": True}
        assert workflow["concurrency"] == {
            "group": "mutation-preflight-${{ github.event_name == 'push' && github.ref || github.run_id }}",
            "cancel-in-progress": "${{ github.event_name == 'push' }}",
        }
    else:
        assert workflow["concurrency"] == {"group": "mutation-preflight-${{ github.ref }}", "cancel-in-progress": True}
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read", "actions": "read"}


def test_preflight_skips_a_branch_with_an_open_pull_request():
    workflow = _workflow()
    jobs = workflow["jobs"]
    check = jobs["pull-request"]
    if "workflow_call" in workflow[True]:
        assert check["if"] == "${{ github.event_name == 'push' && !github.event.deleted }}"
        assert jobs["mutation"]["if"] == (
            "${{ !cancelled() && (github.event_name != 'push' || needs.pull-request.outputs.open == 'false') }}"
        )
    else:
        assert check["if"] == "${{ !github.event.deleted }}"
        assert jobs["mutation"]["if"] == "${{ needs.pull-request.outputs.open == 'false' }}"
    assert check["outputs"] == {"open": "${{ steps.open.outputs.open }}"}
    [step] = check["steps"]
    assert step["id"] == "open"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "head=$GITHUB_REPOSITORY_OWNER:$GITHUB_REF_NAME" in step["run"]
    assert "state=open" in step["run"]
    assert """--jq '[.[] | select(.base.ref == "dev")] | length'""" in step["run"]
    assert jobs["mutation"]["needs"] == "pull-request"


def test_preflight_mutates_against_dev_with_the_pull_request_budget():
    workflow = _workflow()
    job = workflow["jobs"]["mutation"]
    assert job["timeout-minutes"] == 20
    steps = job["steps"]
    assert steps[0]["with"] == {"fetch-depth": 0}
    names = [step.get("name") for step in steps]
    select = steps[names.index("Select mutation tests before browser setup")]
    mutate = steps[names.index("Mutate changed Python files")]
    plan = steps[names.index("Resolve the branch's own mutation bases")]
    assert job["env"]["BASE"] == "${{ inputs.base || 'origin/dev' }}"
    assert plan["id"] == "plan"
    assert plan["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert plan["run"] == "python -m scripts.ci_mutation." + 'plan --base "$BASE"'
    assert names.index("Resolve the branch's own mutation bases") < names.index(select["name"])
    for step in (select, mutate):
        assert step["env"]["BASES"] == "${{ steps.plan.outputs.bases }}"
    assert select["run"] == "python -m scripts.ci_mutation." + 'browser --bases "$BASES"'
    assert mutate["run"] == 'python -m scripts.ci_mutation --bases "$BASES" --budget 1080'
    assert names.index("Install the browser that page tests drive") < names.index("Mutate changed Python files")
    assert steps[-1]["if"] == "always()"
    assert steps[-1]["with"]["name"] == "mutation-preflight-report"


def test_only_the_push_preflight_reports_the_check_that_proves_a_commit_graded():
    tests = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    assert "name" not in tests["mutation"]
    assert tests["mutation"]["strategy"]["matrix"] == {"shard": "${{ fromJSON(needs.mutation-plan.outputs.shards) }}"}
    assert "name" not in _workflow()["jobs"]["mutation"]
    assert "strategy" not in _workflow()["jobs"]["mutation"]
    proofs = yaml.safe_load((_ROOT / ".github/workflows/proofs.yml").read_text())["jobs"]
    assert proofs["mutation"]["uses"] == "./.github/workflows/mutation-preflight.yml"
