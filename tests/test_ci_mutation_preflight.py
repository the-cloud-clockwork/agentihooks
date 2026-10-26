import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from string import Template

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/mutation-preflight.yml").read_text())


def test_preflight_runs_on_task_branch_pushes_only():
    workflow = _workflow()
    events = workflow[True]
    assert set(events) in ({"push"}, {"push", "workflow_call"})
    ignored = ["dev", "main", "gh-readonly-queue/**", "wip/**", "diffcheck/**"]
    assert events["push"] in ({"branches-ignore": ignored}, {"branches-ignore": [*ignored, "promotion/**"]})
    if "workflow_call" in events:
        assert events["workflow_call"]["inputs"]["base"] == {"type": "string", "required": True}
        assert workflow["concurrency"] == {
            "group": "mutation-preflight-${{ github.event_name == 'push' && github.ref || github.run_id }}",
            "cancel-in-progress": "${{ github.event_name == 'push' }}",
        }
    else:
        assert workflow["concurrency"] == {"group": "mutation-preflight-${{ github.ref }}", "cancel-in-progress": True}
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read", "actions": "read"}


def test_preflight_does_not_repeat_grading_for_promotion_snapshots():
    assert "promotion/**" in _workflow()[True]["push"]["branches-ignore"]


def test_preflight_skips_a_branch_with_an_open_pull_request():
    jobs = _workflow()["jobs"]
    assert "pull-request" not in jobs
    plan = jobs["plan"]
    if "capacity" in jobs:
        assert plan["needs"] == "capacity"
        assert plan["if"] == (
            "${{ !cancelled() && (needs.capacity.result == 'success' || needs.capacity.result == 'skipped')"
            " && (github.event_name != 'push' || !github.event.deleted) }}"
        )
        assert "secrets." not in str(plan)
        capacity = jobs["capacity"]
        assert [step.get("id") for step in capacity["steps"]] == ["capacity-token", "capacity"]
        assert capacity["steps"][0]["uses"] == "actions/create-github-app-token@v3.2.0"
        assert capacity["steps"][1]["run"].startswith("gh api ")
        assert "checkout" not in str(capacity)
    else:
        assert "needs" not in plan
        assert plan["if"] == "${{ github.event_name != 'push' || !github.event.deleted }}"
    assert plan["outputs"]["graded"] == "${{ steps.plan.conclusion == 'success' }}"
    step = plan["steps"][0]
    assert step["id"] == "open"
    assert step["if"] == "github.event_name == 'push'"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "head=$GITHUB_REPOSITORY_OWNER:$GITHUB_REF_NAME" in step["run"]
    assert "state=open" in step["run"]
    assert """--jq '[.[] | select(.base.ref == "dev")] | length'""" in step["run"]
    for name in ("stats", "mutation-shard", "mutation"):
        assert jobs[name]["if"].startswith("${{ !cancelled() && needs.plan.outputs.graded == 'true'"), name
    for name in ("stats", "mutation-shard"):
        assert "needs.plan.outputs.changed != '0'" in jobs[name]["if"], name
    assert "changed" not in jobs["mutation"]["if"]


def test_preflight_reuses_a_green_report_before_the_plan_installs_anything():
    plan = _workflow()["jobs"]["plan"]
    steps, names = _steps(plan)
    assert "concurrency" not in plan
    assert steps[1]["uses"] == "actions/checkout@v4"
    assert steps[1]["if"] == "steps.open.outputs.open != 'true'"
    assert steps[1]["with"] == {"fetch-depth": 0, "filter": "blob:none", "persist-credentials": False}
    scope = steps[names.index("Record the revisions the report grades")]
    assert scope["if"] == "steps.open.outputs.open == 'false'"
    assert '"$(git merge-base origin/dev HEAD)" "$(git rev-parse HEAD)" > .mutation-gate/scope.json' in scope["run"]
    reuse = steps[names.index("Reuse a green report when this head only adds tests to it")]
    assert reuse["id"] == "reuse"
    assert reuse["if"] == "steps.open.outputs.open == 'false'"
    reuse["env"]["UNRELATED_SETTING"] = "ignored"
    assert reuse["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert reuse["run"] == "python3 -m scripts.ci_mutation." + 'reuse "$GITHUB_REF_NAME" "$GITHUB_RUN_ID"'
    upload_at = names.index("Upload the reused mutation evidence")
    upload = steps[upload_at]
    assert upload["if"] == "steps.reuse.outputs.reused == 'true'"
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"] == {
        "name": "mutation-preflight-report",
        "path": ".mutation-gate/",
        "include-hidden-files": True,
        "retention-days": 7,
    }
    gated = "steps.open.outputs.open != 'true' && steps.reuse.outputs.reused != 'true'"
    assert steps[upload_at + 1 :]
    for step in steps[upload_at + 1 :]:
        if step.get("id") in {"capacity-token", "lanes"}:
            assert (
                step["if"]
                == "steps.plan.conclusion == 'success' && steps.plan.outputs.changed != '0' && github.workflow == 'Mutation preflight' && runner.environment == 'self-hosted'"
            )
        else:
            assert step["if"] == gated, step


def test_preflight_runs_the_suite_speed_contracts_after_the_touched_tests_when_a_test_module_changes():
    workflow = _workflow()
    plan_steps, plan_names = _steps(workflow["jobs"]["plan"])
    assert "Run the suite speed contracts" not in plan_names
    assert not any("steps.suite" in step.get("if", "") for step in plan_steps)
    steps, names = _steps(workflow["jobs"]["touched-tests"])
    run_at = names.index("Run the suite speed contracts")
    assert run_at == names.index("Run the tests the branch touches") + 1
    assert steps[run_at]["if"] in (
        "steps.touched.outputs.files != ''",
        "steps.open.outputs.open == 'false' && steps.touched.outputs.files != ''",
    )
    assert steps[run_at]["run"] == "python -m pytest tests/test_suite_speed.py --tb=short -p no:cacheprovider"
    assert not any(step.get("continue-on-error") for step in steps)


def test_pull_request_reuse_does_not_grade_the_skipped_empty_plan():
    plan = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["mutation-plan"]
    steps, names = _steps(plan)
    grade = steps[names.index("Grade the empty change set")]
    install = steps[names.index("Install dependencies")]
    assert grade["if"] == f"{install['if']} && steps.plan.outputs.changed == '0'"


def _steps(job):
    steps = job["steps"]
    return steps, [step.get("name") for step in steps]


def test_preflight_sizes_its_stats_parts_and_shards_from_the_plan(tmp_path, monkeypatch):
    jobs = _workflow()["jobs"]
    plan = jobs["plan"]
    assert plan["env"]["BASE"] == "${{ inputs.base || 'origin/dev' }}"
    outputs = dict(plan["outputs"])
    if "stats_lane" in outputs:
        assert outputs.pop("stats_lane") == "${{ steps.lanes.outputs.lane }}"
        assert outputs.pop("batch_ceiling") == "${{ steps.lanes.outputs.ceiling }}"
    assert outputs == {
        "shards": "${{ steps.plan.outputs.shards }}",
        "stats_parts": "${{ steps.plan.outputs.stats_parts }}",
        "bases": "${{ steps.plan.outputs.bases }}",
        "browser": "${{ steps.plan.outputs.browser }}",
        "changed": "${{ steps.plan.outputs.changed }}",
        "graded": "${{ steps.plan.conclusion == 'success' }}",
        "open": "${{ steps.open.outputs.open }}",
    }
    steps, names = _steps(plan)
    assert steps[names.index(None)]["with"] == {"fetch-depth": 0, "filter": "blob:none", "persist-credentials": False}
    sized = steps[names.index("Size mutation shards from stored timings")]
    assert sized["id"] == "plan"
    sized["env"]["UNRELATED_SETTING"] = "ignored"
    assert sized["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert sized["env"]["MUTATION_SHARD_CPU_QUOTA"] == "3"
    from scripts.ci_mutation import plan as planner
    from tests.test_ci_mutation_plan import _project

    _project(tmp_path, {})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mutmut.configuration._config", None)
    command = shlex.split(Template(sized["run"]).substitute(BASE="chosen-base", GITHUB_REPOSITORY="org/repo"))
    module = command.index("-m") + 1
    assert command[module] == "scripts.ci_mutation.plan"
    monkeypatch.setattr("sys.argv", command[module:])
    calls, records, graders = [], [], []

    def bases(root, base, head, *args):
        calls.append((root, base, head))
        graders.append(args)
        return ["resolved-base"]

    def record(repository, *args):
        records.append(repository)
        return None

    monkeypatch.setattr(planner, "own_bases", bases)
    monkeypatch.setattr(planner, "find_record", record)
    monkeypatch.setattr(planner, "discover_changes", lambda *args: {"scripts/sample.py": {2}})
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert planner.main() == 0
    assert calls == [(tmp_path, "chosen-base", "HEAD")]
    assert records == ["org/repo"]
    assert graders == [(planner.graded_green,)]
    assert json.loads((tmp_path / ".mutation-selection.json").read_text()) == {
        "scripts/sample.py": ["tests/test_sample.py"]
    }
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
        "shards": "[0]",
        "stats_parts": "[0]",
        "bases": "resolved-base",
        "changed": "1",
        "browser": "false",
    }
    assert jobs["stats"]["needs"] == "plan"
    assert jobs["stats"]["strategy"] == {
        "fail-fast": False,
        "matrix": {"part": "${{ fromJSON(needs.plan.outputs.stats_parts) }}"},
    }
    assert jobs["mutation-shard"]["needs"] == ["plan", "stats", "refine"]
    assert jobs["mutation-shard"]["strategy"] == {
        "fail-fast": False,
        "matrix": {"shard": "${{ fromJSON(needs.refine.outputs.shards || needs.stats.outputs.shards) }}"},
    }
    assert jobs["mutation-shard"]["if"] == (
        "${{ !cancelled() && needs.plan.outputs.graded == 'true' && needs.plan.outputs.changed != '0'"
        " && needs.stats.result == 'success' && (needs.refine.result == 'success'"
        " || needs.refine.result == 'skipped' && needs.plan.outputs.stats_parts == '[0]') }}"
    )


def test_mutation_matrix_and_merge_use_the_plan_refined_after_statistics():
    jobs = _workflow()["jobs"]
    refine = jobs["refine"]
    assert refine["needs"] == ["plan", "stats"]
    assert refine["if"] == (
        "${{ !cancelled() && needs.plan.outputs.graded == 'true' && needs.plan.outputs.changed != '0'"
        " && needs.stats.result == 'success' && needs.plan.outputs.stats_parts != '[0]' }}"
    )
    assert refine["outputs"]["shards"] == "${{ steps.refine.outputs.shards }}"
    steps, names = _steps(refine)
    measured = steps[names.index("Refine mutation shards from measured statistics")]
    assert measured["run"] == 'python -m scripts.ci_mutation.measured --bases "$BASES" --stats .mutation-stats'
    assert names.index("Download the shared mutation stats") < names.index(measured["name"])
    selection = steps[names.index("Download planned mutation test selection")]
    assert selection["with"] == {"name": "mutation-selection"}
    assert names.index(selection["name"]) < names.index(measured["name"])
    assert jobs["mutation-shard"]["strategy"]["matrix"]["shard"] == (
        "${{ fromJSON(needs.refine.outputs.shards || needs.stats.outputs.shards) }}"
    )
    assert "refine" in jobs["mutation-shard"]["needs"]
    assert "refine" in jobs["mutation"]["needs"]
    assert "stats" in jobs["mutation"]["needs"]
    shard_steps, shard_names = _steps(jobs["mutation-shard"])
    artifact = shard_steps[shard_names.index("Download the measured mutation plan")]
    assert artifact["with"] == {"name": "mutation-plan", "path": ".mutation-stats"}
    merge_steps, merge_names = _steps(jobs["mutation"])
    shards = merge_steps[merge_names.index("Merge the shard reports")]["env"]["SHARDS"]
    assert "needs.refine.outputs.shards || needs.stats.outputs.shards" in shards


def test_a_single_stats_part_refines_inside_the_statistics_worker():
    jobs = _workflow()["jobs"]
    stats = jobs["stats"]
    assert stats["outputs"] == {"shards": "${{ steps.refine.outputs.shards }}"}
    steps, names = _steps(stats)
    refine_steps, refine_names = _steps(jobs["refine"])
    single = "strategy.job-total == 1"
    killers = steps[names.index("Restore the tests that killed this branch's earlier mutants")]
    assert killers["if"] == single
    assert {k: v for k, v in killers.items() if k != "if"} == refine_steps[
        refine_names.index("Restore the tests that killed this branch's earlier mutants")
    ]
    measured = steps[names.index("Refine mutation shards from measured statistics")]
    assert measured["if"] == single
    assert measured["id"] == "refine"
    measured["env"]["UNRELATED_SETTING"] = "ignored"
    assert measured["env"]["BASES"] == "${{ needs.plan.outputs.bases }}"
    assert measured["env"]["MUTATION_SHARD_CPU_QUOTA"] == "3"
    assert measured["run"] == refine_steps[refine_names.index(measured["name"])]["run"]
    upload = steps[names.index("Upload the measured mutation plan")]
    assert upload["if"] == single
    assert {k: v for k, v in upload.items() if k != "if"} == refine_steps[refine_names.index(upload["name"])]
    order = [
        "Collect one stats part for every mutation shard",
        "Upload mutation stats part",
        killers["name"],
        measured["name"],
        upload["name"],
    ]
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
    assert "Download the shared mutation stats" not in names


def test_every_stats_part_restores_one_merged_cache_saved_once_after_all_parts_finish():
    jobs = _workflow()["jobs"]
    key = "mutation-test-stats-v2-${{ runner.os }}-${{ github.run_id }}-${{ github.run_attempt }}"
    steps, names = _steps(jobs["stats"])
    restore = steps[names.index("Restore the merged test stats")]
    assert restore["uses"] == "actions/cache/restore@v4"
    assert restore["with"] == {
        "path": ".mutation-stats-cache",
        "key": key,
        "restore-keys": "mutation-test-stats-v2-${{ runner.os }}-",
    }
    assert "if" not in restore
    single = steps[names.index("Save the merged test stats")]
    assert single["uses"] == "actions/cache/save@v4"
    assert single["if"] == "success() && strategy.job-total == 1"
    assert single["with"] == {"path": ".mutation-stats-cache", "key": key}
    part = steps[names.index("Upload this part's test stats cache")]
    assert part["if"] == "success() && strategy.job-total != 1"
    assert part["uses"] == "actions/upload-artifact@v4"
    assert part["with"] == {
        "name": "mutation-test-stats-cache-${{ matrix.part }}",
        "path": ".mutation-stats-cache/",
        "include-hidden-files": True,
        "if-no-files-found": "ignore",
        "retention-days": 1,
        "overwrite": True,
    }
    collect = names.index("Collect one stats part for every mutation shard")
    assert names.index(restore["name"]) < collect < names.index(single["name"])
    assert collect < names.index(part["name"])
    assert [step for step in steps if step.get("uses", "").startswith("actions/cache/save")] == [single]
    assert not part["with"]["name"].startswith("mutation-stats-")
    refine_steps, refine_names = _steps(jobs["refine"])
    download = refine_steps[refine_names.index("Download every part's test stats cache")]
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"] == {
        "pattern": "mutation-test-stats-cache-*",
        "path": ".mutation-stats-cache",
        "merge-multiple": True,
    }
    merged = refine_steps[refine_names.index("Save the merged test stats")]
    assert merged["uses"] == "actions/cache/save@v4"
    assert merged["if"] == "success() && hashFiles('.mutation-stats-cache/**') != ''"
    assert merged["with"] == {"path": ".mutation-stats-cache", "key": key}
    assert refine_names.index(download["name"]) < refine_names.index(merged["name"])
    assert "matrix.part" not in str([restore, single, merged])


def test_preflight_collects_one_stats_part_per_leg_with_the_pull_request_budget():
    job = _workflow()["jobs"]["stats"]
    assert job["timeout-minutes"] == 26
    steps, names = _steps(job)
    collect = steps[names.index("Collect one stats part for every mutation shard")]
    assert collect["env"]["BASES"] == "${{ needs.plan.outputs.bases }}"
    selection = steps[names.index("Download planned mutation test selection")]
    assert selection["with"]["name"] == "mutation-selection"
    assert "Select mutation tests before browser setup" not in names
    browser = steps[names.index("Install the browser that page tests drive")]
    assert browser["if"] == "needs.plan.outputs.browser == 'true'"
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
    assert job["timeout-minutes"] == 26
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
    assert steps[-1]["with"]["path"] == (
        "${{ job.status == 'success' && '.mutation-gate/report.json' || '.mutation-gate/' }}"
    )


def test_single_shard_collection_preserves_both_supported_workflow_states():
    jobs = _workflow()["jobs"]
    steps, names = _steps(jobs["mutation-shard"])
    inline_name = "Collect stats and mutate the only shard"
    if inline_name not in names:
        assert jobs["stats"]["if"] == (
            "${{ !cancelled() && needs.plan.outputs.graded == 'true' && needs.plan.outputs.changed != '0' }}"
        )
        return
    assert jobs["stats"]["if"] == (
        "${{ !cancelled() && needs.plan.outputs.graded == 'true' && needs.plan.outputs.changed != '0'"
        " && needs.plan.outputs.shards != '[0]' }}"
    )
    assert jobs["mutation-shard"]["if"] == (
        "${{ !cancelled() && needs.plan.outputs.graded == 'true' && needs.plan.outputs.changed != '0'"
        " && (needs.stats.result == 'success' || (needs.stats.result == 'skipped'"
        " && needs.plan.outputs.shards == '[0]')) }}"
    )
    inline = steps[names.index(inline_name)]
    assert inline["if"] == "needs.plan.outputs.shards == '[0]'"
    assert inline["env"]["BASES"] == "${{ needs.plan.outputs.bases }}"
    assert inline["run"] == 'python -m scripts.ci_mutation --bases "$BASES" --budget 1080'
    for name in ("Download the shared mutation stats", "Mutate changed Python files"):
        assert steps[names.index(name)]["if"] == "needs.plan.outputs.shards != '[0]'"


def test_preflight_merges_every_shard_into_the_one_report_its_readers_take():
    workflow = _workflow()
    job = workflow["jobs"]["mutation"]
    assert job["needs"] == ["plan", "stats", "refine", "mutation-shard"]
    assert job["if"] == "${{ !cancelled() && needs.plan.outputs.graded == 'true' }}"
    assert job["env"]["BASE"] == "${{ inputs.base || 'origin/dev' }}"
    steps, names = _steps(job)
    assert steps[0]["with"] == {"fetch-depth": 0}
    download = steps[names.index("Download every mutation shard report")]
    assert download["with"] == {"pattern": "mutation-preflight-shard-*", "path": ".mutation-shards"}
    assert download["if"] == "needs.plan.outputs.changed != '0'"
    scope = steps[names.index("Record the revisions the report grades")]
    assert '"$(git merge-base "$BASE" HEAD)" "$(git rev-parse HEAD)" > .mutation-gate/scope.json' in scope["run"]
    merge = steps[names.index("Merge the shard reports")]
    merge["env"]["UNRELATED_SETTING"] = "ignored"
    assert merge["env"]["SHARDS"] == (
        "${{ needs.plan.outputs.changed != '0'"
        " && (needs.refine.outputs.shards || needs.stats.outputs.shards) || '[]' }}"
    )
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


def test_touched_tests_job_selects_and_runs_the_tests_the_branch_touches():
    job = _workflow()["jobs"]["touched-tests"]
    assert job["timeout-minutes"] == 15
    steps, names = _steps(job)
    select = steps[names.index("Select the tests the branch touches")]
    assert select["id"] == "touched"
    assert 'changed_files(root, "origin/dev", "HEAD")' in select["run"]
    assert "if is_test(path) and (root / path).is_file()" in select["run"]
    browser = steps[names.index("Install the browser that page tests drive")]
    assert browser["if"] in (
        "steps.touched.outputs.browser == 'true'",
        "steps.open.outputs.open == 'false' && steps.touched.outputs.browser == 'true'",
    )
    run = steps[names.index("Run the tests the branch touches")]
    assert run["if"] in (
        "steps.touched.outputs.files != ''",
        "steps.open.outputs.open == 'false' && steps.touched.outputs.files != ''",
    )
    assert run["env"]["FILES"] == "${{ steps.touched.outputs.files }}"
    assert run["run"] == (
        'read -ra files <<< "$FILES"\npython -m pytest "${files[@]}" -n auto --dist loadgroup --tb=short -p no:cacheprovider\n'
    )
    assert not any(step.get("continue-on-error") for step in steps)


def test_touched_tests_admission_preserves_both_supported_job_graphs():
    jobs = _workflow()["jobs"]
    job = jobs["touched-tests"]
    if job.get("needs") == "plan":
        assert job["if"] == "${{ !cancelled() && needs.plan.outputs.open == 'false' }}"
        return
    assert "needs" not in job
    assert job["if"] == "${{ github.event_name == 'push' && !github.event.deleted }}"
    assert job["steps"][0] == jobs["plan"]["steps"][0]
    for step in job["steps"][1:]:
        assert step["if"].startswith("steps.open.outputs.open == 'false'")
    assert not any(step.get("continue-on-error") for step in job["steps"])


def test_touched_test_selection_supplies_the_automatic_worker_limit():
    steps, names = _steps(_workflow()["jobs"]["touched-tests"])
    select = steps[names.index("Select the tests the branch touches")]
    assert "from scripts.ci_touched_tests import worker_count" in select["run"]
    assert "workers = worker_count(tests) if tests else 1" in select["run"]
    assert "workers={workers}" in select["run"]
    run = steps[names.index("Run the tests the branch touches")]
    assert run["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "${{ steps.touched.outputs.workers }}"


_GROUPED = """
import os, pytest

@pytest.mark.xdist_group("client")
@pytest.mark.parametrize("n", range(4))
def test_grouped(n):
    with open(os.path.join(os.environ["GROUP_OUT"], str(n)), "w") as stream:
        stream.write(os.environ["PYTEST_XDIST_WORKER"])
"""


def test_touched_tests_keep_one_xdist_group_on_one_worker(tmp_path):
    steps, names = _steps(_workflow()["jobs"]["touched-tests"])
    run = steps[names.index("Run the tests the branch touches")]["run"]
    line = next(line for line in run.splitlines() if line.startswith("python -m pytest "))
    command = shlex.split(line.replace('"${files[@]}"', "test_cases.py"))
    assert command[:3] == ["python", "-m", "pytest"]
    command[0] = sys.executable
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_cases.py").write_text(_GROUPED)
    out = tmp_path / "out"
    out.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COV_CORE_", "COVERAGE_", "PYTEST_"))}
    env |= {"PYTEST_XDIST_AUTO_NUM_WORKERS": "2", "GROUP_OUT": str(out)}
    process = subprocess.Popen(
        command,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output = process.communicate(timeout=120)[0]
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output = "timed out\n" + process.communicate()[0]
    assert process.returncode == 0, output
    workers = [path.read_text() for path in sorted(out.iterdir())]
    assert len(workers) == 4, output
    assert len(set(workers)) == 1, (workers, output)
