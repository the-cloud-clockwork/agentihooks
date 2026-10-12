from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/mutation-preflight.yml").read_text())


def test_preflight_runs_on_task_branch_pushes_only():
    workflow = _workflow()
    events = workflow[True]
    assert set(events) in ({"push"}, {"push", "workflow_call"})
    assert events["push"] == {"branches-ignore": ["dev", "main", "gh-readonly-queue/**", "wip/**", "diffcheck/**"]}
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
        assert jobs["plan"]["if"] == (
            "${{ !cancelled() && (github.event_name != 'push' || (needs.pull-request.outputs.open == 'false'"
            " && needs.pull-request.outputs.reused != 'true')) }}"
        )
    else:
        assert check["if"] == "${{ !github.event.deleted }}"
        assert jobs["plan"]["if"] == "${{ needs.pull-request.outputs.open == 'false' }}"
    assert check["outputs"] == {
        "open": "${{ steps.open.outputs.open }}",
        "reused": "${{ steps.reuse.outputs.reused }}",
    }
    step = check["steps"][0]
    assert step["id"] == "open"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "head=$GITHUB_REPOSITORY_OWNER:$GITHUB_REF_NAME" in step["run"]
    assert "state=open" in step["run"]
    assert """--jq '[.[] | select(.base.ref == "dev")] | length'""" in step["run"]
    assert jobs["plan"]["needs"] == "pull-request"
    for name in ("stats", "mutation-shard", "mutation"):
        assert jobs[name]["if"].startswith("${{ !cancelled() && needs.plan.result == 'success'"), name
    for name in ("stats", "mutation-shard"):
        assert "needs.plan.outputs.changed != '0'" in jobs[name]["if"], name
    assert "changed" not in jobs["mutation"]["if"]


def test_preflight_reuses_a_green_report_before_the_mutation_job_takes_a_lane():
    check = _workflow()["jobs"]["pull-request"]
    steps = check["steps"]
    names = [step.get("name") for step in steps]
    assert "runs-on" in check and "concurrency" not in check
    for step in steps[1:4]:
        assert step["if"] == "steps.open.outputs.open == 'false'"
    assert steps[1]["uses"] == "actions/checkout@v4"
    assert steps[1]["with"] == {"fetch-depth": 0, "persist-credentials": False}
    scope = steps[names.index("Record the revisions the report grades")]
    assert '"$(git merge-base origin/dev HEAD)" "$(git rev-parse HEAD)" > .mutation-gate/scope.json' in scope["run"]
    reuse = steps[names.index("Reuse a green report when this head only adds tests to it")]
    assert reuse["id"] == "reuse"
    assert reuse["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert reuse["run"] == "python3 -m scripts.ci_mutation." + 'reuse "$GITHUB_REF_NAME" "$GITHUB_RUN_ID"'
    upload = steps[-1]
    assert upload["if"] == "steps.reuse.outputs.reused == 'true'"
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"] == {
        "name": "mutation-preflight-report",
        "path": ".mutation-gate/",
        "include-hidden-files": True,
        "retention-days": 7,
    }


def _steps(job):
    steps = job["steps"]
    return steps, [step.get("name") for step in steps]


def test_preflight_sizes_its_stats_parts_and_shards_from_the_plan():
    jobs = _workflow()["jobs"]
    plan = jobs["plan"]
    assert plan["env"]["BASE"] == "${{ inputs.base || 'origin/dev' }}"
    assert plan["outputs"] == {
        "shards": "${{ steps.plan.outputs.shards }}",
        "stats_parts": "${{ steps.plan.outputs.stats_parts }}",
        "bases": "${{ steps.plan.outputs.bases }}",
        "changed": "${{ steps.plan.outputs.changed }}",
    }
    steps, names = _steps(plan)
    assert steps[0]["with"] == {"fetch-depth": 0}
    sized = steps[names.index("Size mutation shards from stored timings")]
    assert sized["id"] == "plan"
    assert sized["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert sized["run"] == "python -m scripts.ci_mutation." + 'plan --base "$BASE"'
    assert jobs["stats"]["needs"] == ["pull-request", "plan"]
    assert jobs["stats"]["strategy"] == {
        "fail-fast": False,
        "matrix": {"part": "${{ fromJSON(needs.plan.outputs.stats_parts) }}"},
    }
    assert jobs["mutation-shard"]["needs"] == ["plan", "stats"]
    assert jobs["mutation-shard"]["strategy"] == {
        "fail-fast": False,
        "matrix": {"shard": "${{ fromJSON(needs.plan.outputs.shards) }}"},
    }
    assert (
        jobs["mutation-shard"]["if"]
        == "${{ !cancelled() && needs.plan.result == 'success' && needs.plan.outputs.changed != '0'"
        " && needs.stats.result == 'success' }}"
    )


def test_preflight_collects_one_stats_part_per_leg_with_the_pull_request_budget():
    job = _workflow()["jobs"]["stats"]
    assert job["timeout-minutes"] == 20
    steps, names = _steps(job)
    select = steps[names.index("Select mutation tests before browser setup")]
    collect = steps[names.index("Collect one stats part for every mutation shard")]
    for step in (select, collect):
        assert step["env"]["BASES"] == "${{ needs.plan.outputs.bases }}"
    assert select["run"] == "python -m scripts.ci_mutation." + 'browser --bases "$BASES"'
    assert collect["env"]["PART"] == "${{ matrix.part }}"
    assert collect["env"]["PARTS"] == "${{ strategy.job-total }}"
    assert collect["run"] == (
        'mkdir -p .mutation-stats && touch ".mutation-stats/collected-$PART" && python -m scripts.ci_mutation'
        ' --bases "$BASES" --budget 1080 --stats .mutation-stats --stats-part "$PART" --stats-parts "$PARTS"'
    )
    assert names.index("Install the browser that page tests drive") < names.index(collect["name"])
    upload = steps[names.index("Upload mutation stats part")]
    assert upload["with"]["name"] == "mutation-stats-${{ matrix.part }}"
    assert upload["with"]["path"] == ".mutation-stats/"


def test_preflight_mutates_each_shard_against_the_shared_stats_with_the_pull_request_budget():
    job = _workflow()["jobs"]["mutation-shard"]
    assert job["timeout-minutes"] == 20
    steps, names = _steps(job)
    assert steps[0]["with"] == {"fetch-depth": 0}
    stats = steps[names.index("Download the shared mutation stats")]
    assert stats["with"] == {"pattern": "mutation-stats-*", "path": ".mutation-stats", "merge-multiple": True}
    mutate = steps[names.index("Mutate changed Python files")]
    assert mutate["env"]["BASES"] == "${{ needs.plan.outputs.bases }}"
    assert mutate["env"]["SHARD"] == "${{ matrix.shard }}"
    assert mutate["env"]["SHARDS"] == "${{ strategy.job-total }}"
    assert mutate["run"] == (
        'python -m scripts.ci_mutation --bases "$BASES" --budget 1080 --shard "$SHARD" --shards "$SHARDS"'
        " --stats .mutation-stats"
    )
    assert names.index(stats["name"]) < names.index(mutate["name"])
    assert names.index("Install the browser that page tests drive") < names.index(mutate["name"])
    assert steps[-1]["if"] == "always()"
    assert steps[-1]["with"]["name"] == "mutation-preflight-shard-${{ matrix.shard }}"
    assert steps[-1]["with"]["path"] == ".mutation-gate/"


def test_preflight_merges_every_shard_into_the_one_report_its_readers_take():
    workflow = _workflow()
    job = workflow["jobs"]["mutation"]
    assert job["needs"] == ["plan", "mutation-shard"]
    assert job["if"] == "${{ !cancelled() && needs.plan.result == 'success' }}"
    assert job["env"]["BASE"] == "${{ inputs.base || 'origin/dev' }}"
    steps, names = _steps(job)
    assert steps[0]["with"] == {"fetch-depth": 0}
    download = steps[names.index("Download every mutation shard report")]
    assert download["with"] == {"pattern": "mutation-preflight-shard-*", "path": ".mutation-shards"}
    assert download["if"] == "needs.plan.outputs.changed != '0'"
    scope = steps[names.index("Record the revisions the report grades")]
    assert '"$(git merge-base "$BASE" HEAD)" "$(git rev-parse HEAD)" > .mutation-gate/scope.json' in scope["run"]
    merge = steps[names.index("Merge the shard reports")]
    assert merge["env"] == {"SHARDS": "${{ needs.plan.outputs.changed != '0' && needs.plan.outputs.shards || '[]' }}"}
    assert merge["run"] == (
        "python -m scripts.ci_mutation."
        'merge .mutation-shards --shards "$(jq length <<< "$SHARDS")" --output .mutation-gate'
    )
    assert names.index(scope["name"]) < names.index(merge["name"])
    upload = steps[names.index("Upload complete mutation evidence")]
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == "mutation-preflight-report"
    shards = steps[names.index("Fail when a shard did not finish")]
    assert shards["if"] == "needs.plan.outputs.changed != '0' && needs.mutation-shard.result != 'success'"
    assert names.index(upload["name"]) < names.index(shards["name"])


def test_only_the_push_preflight_reports_the_check_that_proves_a_commit_graded():
    tests = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    assert "name" not in tests["mutation"]
    assert tests["mutation"]["strategy"]["matrix"] == {"shard": "${{ fromJSON(needs.mutation-plan.outputs.shards) }}"}
    assert "name" not in _workflow()["jobs"]["mutation"]
    assert "strategy" not in _workflow()["jobs"]["mutation"]
    assert "strategy" in _workflow()["jobs"]["mutation-shard"]
    proofs = yaml.safe_load((_ROOT / ".github/workflows/proofs.yml").read_text())["jobs"]
    assert proofs["mutation"]["uses"] == "./.github/workflows/mutation-preflight.yml"
