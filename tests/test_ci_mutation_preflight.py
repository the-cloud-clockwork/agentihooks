from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/mutation-preflight.yml").read_text())


def test_preflight_runs_on_task_branch_pushes_only():
    workflow = _workflow()
    assert list(workflow[True]) == ["push"]
    assert workflow[True]["push"] == {"branches-ignore": ["dev", "main", "gh-readonly-queue/**", "wip/**"]}
    assert workflow["concurrency"] == {"group": "mutation-preflight-${{ github.ref }}", "cancel-in-progress": True}
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}


def test_preflight_skips_a_branch_with_an_open_pull_request():
    jobs = _workflow()["jobs"]
    check = jobs["pull-request"]
    assert check["if"] == "${{ !github.event.deleted }}"
    assert check["outputs"] == {"open": "${{ steps.open.outputs.open }}"}
    [step] = check["steps"]
    assert step["id"] == "open"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "head=$GITHUB_REPOSITORY_OWNER:$GITHUB_REF_NAME" in step["run"]
    assert "state=open" in step["run"]
    assert """--jq '[.[] | select(.base.ref == "dev")] | length'""" in step["run"]
    assert jobs["mutation"]["needs"] == "pull-request"
    assert jobs["mutation"]["if"] == "${{ needs.pull-request.outputs.open == 'false' }}"


def test_preflight_mutates_against_dev_with_the_pull_request_budget():
    job = _workflow()["jobs"]["mutation"]
    assert job["timeout-minutes"] == 20
    steps = job["steps"]
    assert steps[0]["with"] == {"fetch-depth": 0}
    names = [step.get("name") for step in steps]
    select = steps[names.index("Select mutation tests before browser setup")]
    mutate = steps[names.index("Mutate changed Python files")]
    assert select["run"] == "python -m scripts.ci_mutation.browser --base origin/dev"
    assert mutate["run"] == "python -m scripts.ci_mutation --base origin/dev --budget 1080"
    assert names.index("Install the browser that page tests drive") < names.index("Mutate changed Python files")
    assert steps[-1]["if"] == "always()"
    assert steps[-1]["with"]["name"] == "mutation-preflight-report"
