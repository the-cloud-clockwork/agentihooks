import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit
LINT_CONTROLS = [
    "Lint check",
    "Format check",
    "Check every pull request job is a need of Gate Required",
    "Hold the size and complexity limits",
    "Hold swallowed exceptions in hooks and gates",
    "Hold dead code in hooks and scripts",
    "Hold the locked guard tests",
    "Fail on known vulnerabilities new against the base",
    "Run every Swarm Ledger dashboard query against this head's metrics tables",
]
LINT_SETUP = [
    "Install ruff",
    "Install PyYAML",
    "Install vulture",
    "Set up uv",
    "Check out the base revision",
    "Check out the protected grader",
]


def _workflow():
    from scripts import ci_gate_queue

    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    return ci_gate_queue.normalize({"test.yml": workflow})["test.yml"]


def test_stored_durations_share_selection_admission_before_both_shard_paths():
    jobs = _workflow()["jobs"]
    assert "durations" not in jobs
    admission = jobs["select"]
    assert "github.event_name != 'merge_group'" not in admission["if"]
    assert admission["outputs"]["key"] == "${{ steps.stored.outputs.cache-matched-key }}"
    assert admission["outputs"]["queued"] == "${{ steps.republished.outputs.queued }}"
    assert admission["outputs"]["source"] == "${{ steps.unit-source.outputs.source }}"
    assert "select" in jobs["split"]["needs"]
    assert "select" in jobs["unit-selected"]["needs"]
    assert jobs["split"]["env"]["UNIT_SOURCE"] == "${{ needs.select.outputs.source }}"
    assert any(
        step.get("with", {}).get("key") == "${{ needs.select.outputs.key }}" for step in jobs["unit-selected"]["steps"]
    )
    published = next(step for step in admission["steps"] if step.get("name") == "Republish the dev durations")
    assert published["with"]["name"] == "dev-durations"
    assert published["with"]["if-no-files-found"] == "error"


@pytest.mark.parametrize("shard_path", ["split", "unit-selected"])
def test_each_shard_path_restores_selected_durations_red_on_a_cache_miss(shard_path):
    steps = _workflow()["jobs"][shard_path]["steps"]
    restore = next(step for step in steps if step.get("name") == "Restore latest dev durations")
    assert restore["with"]["key"] == "${{ needs.select.outputs.key }}"
    assert restore["with"]["fail-on-cache-miss"] is True
    assert "needs.select.outputs.key != ''" in restore["if"]
    adopt = next(step for step in steps if step.get("name") == "Adopt latest dev durations")
    assert "tests.dev_durations" in adopt["run"]


@pytest.mark.parametrize("gate_plant,allowed", [("", False), ("false", False), ("true", True)])
def test_full_tests_dispatch_requires_explicit_gate_plant_intent(gate_plant, allowed):
    workflow = _workflow()
    assert workflow[True]["workflow_dispatch"]["inputs"]["gate_plant"] == {
        "description": "Run the full suite only for a gate plant proof; focused tests and mutation use Proofs",
        "type": "boolean",
        "required": False,
        "default": False,
    }
    admission = workflow["jobs"]["admission"]
    assert admission["if"] == "github.event_name == 'workflow_dispatch'"
    step = admission["steps"][0]
    assert step["name"] == "Require explicit gate plant intent for a full Tests dispatch"
    assert step["env"] == {"GATE_PLANT": "${{ inputs.gate_plant }}"}
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, GATE_PLANT=gate_plant),
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if allowed else 1)
    assert result.stdout == (
        ""
        if allowed
        else "::error::Use the Proofs workflow for focused tests or mutation; full Tests dispatch requires gate_plant=true.\n"
    )
    assert result.stderr == ""


def test_required_gate_runs_after_parallel_unit_and_lint():
    jobs = _workflow()["jobs"]
    gate = jobs["gate-required"]
    required = {"unit", "lint", "sonar", "mutation", "test-count", "coverage-ratchet", "semgrep"}
    assert gate["name"] == "Gate — Required"
    assert (
        required
        <= set(gate["needs"])
        <= required
        | {
            "swarm-image",
            "worker-image",
            "shard-check",
            "brain-smoke",
            "writer-lock",
            "install-smoke",
            "wiring",
            "size",
            "dependency-audit",
            "durations",
            "split",
            "kind-due",
            "helm-kind",
            "ledger-load",
            "mutation-plan",
            "mutation-stats",
            "reuse",
            "queue-fail-fast",
            "unit-lost",
            "unit-rerun",
            "stage-budget",
            "select",
            "unit-selected",
            "docs",
        }
    )
    assert gate["if"] == "${{ always() }}"
    assert jobs["unit"]["needs"] == ["split", "select", "reuse", "admission"]
    assert jobs["split"]["needs"] in (
        ["durations", "select", "reuse", "admission"],
        ["select", "reuse", "admission"],
        ["test-count", "select", "reuse", "admission"],
    )
    assert jobs["lint"]["needs"] in (["reuse"], ["reuse", "admission"])
    if "swarm-image" in gate["needs"]:
        assert jobs["swarm-image"]["uses"] == "./.github/workflows/swarm-smoke.yml"


def test_cheap_gates_share_the_lint_job_and_each_grades_after_an_earlier_red():
    jobs = _workflow()["jobs"]
    assert not {"size", "wiring", "dependency-audit"} & set(jobs)
    assert not {"size", "wiring", "dependency-audit"} & set(jobs["gate-required"]["needs"])
    assert jobs["lint"]["timeout-minutes"] == 10
    steps = {step.get("name"): step for step in jobs["lint"]["steps"]}
    assert all("continue-on-error" not in step for step in steps.values())
    names = list(steps)
    assert all("if" not in steps[name] for name in LINT_SETUP)
    after = names[names.index(LINT_CONTROLS[-1]) + 1 :]
    assert after and all(steps[name]["if"].startswith("${{ !cancelled()") for name in after)


@pytest.mark.parametrize("control", LINT_CONTROLS)
def test_each_cheap_gate_grades_in_lint_after_its_setup_even_after_an_earlier_red(control):
    names = [step.get("name") for step in _workflow()["jobs"]["lint"]["steps"]]
    step = _workflow()["jobs"]["lint"]["steps"][names.index(control)]
    assert step["if"] == "${{ !cancelled() }}"
    assert max(names.index(name) for name in LINT_SETUP) < names.index(control)


def test_lint_grades_dashboard_columns_with_the_antoncore_dev_checker():
    steps = _workflow()["jobs"]["lint"]["steps"]
    token = next((s for s in steps if s.get("id") == "app-token"), None)
    assert token is not None
    assert token["with"]["repositories"] == "antoncore"
    assert token["with"]["permission-contents"] == "read"
    checkout = next((s for s in steps if s.get("env", {}).get("REPOSITORY") == "the-cloud-clockwork/antoncore"), None)
    assert checkout is not None
    assert checkout["if"] == "${{ !cancelled() && steps.app-token.outcome == 'success' }}"
    assert checkout["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert checkout["env"]["REF"] == "dev"
    assert checkout["env"]["DESTINATION"] == ".antoncore"
    assert "uses" not in checkout
    assert "git " not in checkout["run"]
    assert "scripts/swarm_dashboard_columns.py" in checkout["env"]["SELECTED_PATHS"]
    check = next((s for s in steps if "swarm_dashboard_columns.py check" in s.get("run", "")), None)
    assert check is not None
    assert steps.index(token) < steps.index(checkout) < steps.index(check)
    assert '--agentihooks "$GITHUB_WORKSPACE"' in check["run"]
    assert "No module named" in check["run"]


@pytest.mark.parametrize("failure", ["", "missing", "api"])
def test_rest_checkout_reads_selected_files_from_one_revision_and_fails_closed(tmp_path, failure):
    import io
    import os
    import tarfile

    script = next(step for step in _workflow()["jobs"]["lint"]["steps"] if step.get("id") == "antoncore-checkout")[
        "run"
    ]
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name in ("checker.py", "dashboards/one.json", "unselected.txt"):
            if failure == "missing" and name == "checker.py":
                continue
            data = name.encode()
            member = tarfile.TarInfo(f"source-revision/{name}")
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
    tools = tmp_path / "bin"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'case "$2" in\n'
        "  repos/owner/repo/commits/dev) echo source-revision ;;\n"
        "  repos/owner/repo/tarball/source-revision)\n"
        '    [[ "$FAILURE" != api ]] || exit 19\n'
        '    cat "$ARCHIVE" ;;\n'
        "  *) exit 17 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    output = tmp_path / "output"
    destination = tmp_path / "selected"
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "REPOSITORY": "owner/repo",
            "REF": "dev",
            "DESTINATION": str(destination),
            "SELECTED_PATHS": "checker.py\ndashboards/",
            "GITHUB_OUTPUT": str(output),
            "ARCHIVE": str(archive),
            "FAILURE": failure,
        },
        capture_output=True,
        text=True,
    )
    if failure:
        assert result.returncode != 0
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (destination / "checker.py").read_text() == "checker.py"
        assert (destination / "dashboards/one.json").read_text() == "dashboards/one.json"
        assert not (destination / "unselected.txt").exists()
        assert output.read_text() == "revision=source-revision\n"


def test_lint_prepares_the_dashboard_checker_in_the_background_before_the_other_lint_steps():
    steps = _workflow()["jobs"]["lint"]["steps"]
    names = [step.get("name") for step in steps]
    prepare = steps[names.index("Prepare the dashboard query checker in the background")]
    check = steps[names.index("Run every Swarm Ledger dashboard query against this head's metrics tables")]
    assert names.index(prepare["name"]) < min(names.index(name) for name in LINT_SETUP)
    assert prepare["env"]["CLICKHOUSE"] == check["env"]["CLICKHOUSE"]
    assert "orjson==" in prepare["run"]
    assert "-m pip install" not in check["run"]
    assert "docker run" not in check["run"]
    for status in ("columns-venv.status", "clickhouse.status"):
        assert f'echo $? > "$RUNNER_TEMP/{status}"' in prepare["run"]
        assert f'-s "$RUNNER_TEMP/{status}"' in check["run"]
        assert f'[[ "$(cat "$RUNNER_TEMP/{status}" 2>/dev/null)" == 0 ]] || {{' in check["run"]
    assert check["run"].splitlines()[1].startswith('trap \'docker rm -f "$CLICKHOUSE"')
    background = [line for line in prepare["run"].splitlines() if line.rstrip().endswith("&")]
    assert len(background) == 2
    assert all("2>&1 < /dev/null &" in line for line in background)


def test_post_shard_graders_do_not_wait_on_each_other_and_the_gate_needs_each():
    jobs = _workflow()["jobs"]
    graders = {"shard-check", "coverage-ratchet", "sonar"}
    for name in graders:
        needs = jobs[name]["needs"]
        if "unit" not in needs:
            assert name == "shard-check" and "split" in needs
            assert any("python -m scripts.ci_gate_shards" in step.get("run", "") for step in jobs[name]["steps"])
        assert not graders & set(needs), f"{name} waits on another post shard grader"
    assert graders <= set(jobs["gate-required"]["needs"])


def test_semgrep_grades_registry_pack_findings_new_against_the_base_in_parallel():
    job = _workflow()["jobs"]["semgrep"]
    assert job["needs"] in (["reuse"], ["reuse", "admission"])
    assert job["uses"] == "./.github/workflows/semgrep.yml"
    assert job["with"]["base"] == (
        "${{ github.event_name == 'pull_request' && github.base_ref == 'dev' && 'origin/dev'"
        " || github.event.pull_request.base.sha || github.event.merge_group.base_sha"
        " || github.event.before || inputs.base }}"
    )
    assert "continue-on-error" not in job
    assert job["if"] in (
        "${{ !cancelled() && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}",
        "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}",
        "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && github.event_name != 'merge_group' && github.event_name != 'push' }}",
    )


def test_no_workflow_run_script_embeds_an_expression_semgrep_cannot_parse():
    paths = sorted((_ROOT / ".github").glob("workflows/*.yml")) + sorted(
        (_ROOT / ".github").glob("actions/**/action.yml")
    )
    embedded = []
    for path in paths:
        document = yaml.safe_load(path.read_text())
        steps = [step for job in document.get("jobs", {}).values() for step in job.get("steps", [])]
        steps += document.get("runs", {}).get("steps", [])
        embedded += [(path.name, step.get("name")) for step in steps if "${{" in step.get("run", "")]
    assert len(paths) > 10
    assert embedded == []


def test_unit_matrix_does_not_fail_fast():
    assert _workflow()["jobs"]["unit"]["strategy"]["fail-fast"] is False


def test_test_count_floor_runs_per_suite_beside_unit_against_the_base():
    job = _workflow()["jobs"]["test-count"]
    assert job["needs"] in (["reuse"], ["reuse", "admission"])
    assert (
        job["strategy"]["matrix"]["python-version"]
        == _workflow()["jobs"]["unit"]["strategy"]["matrix"]["python-version"]
    )
    base = next(step for step in job["steps"] if step.get("name") == "Check out the base revision")
    floor = next(step for step in job["steps"] if step.get("name") == "Hold the base revision's test count")
    assert base["env"]["BASE"] == (
        "${{ github.event_name == 'pull_request' && github.base_ref == 'dev' && 'origin/dev'"
        " || github.event.pull_request.base.sha || github.event.merge_group.base_sha"
        " || github.event.before || inputs.base }}"
    )
    assert base["run"].replace('echo "BASE: $BASE"\n', "") == (
        'BASE="$(git merge-base "$BASE" HEAD)"\ngit worktree add --detach "$RUNNER_TEMP/base" "$BASE"\n'
    )
    serial = (
        'if [[ ! -f "$RUNNER_TEMP/base/tests/count_floor.py" ]]; then\n'
        '  echo "::error::The base carries no tests/count_floor.py, so nothing trusted can grade."\n'
        "  exit 1\n"
        "fi\n"
        'cd "$RUNNER_TEMP/base"\n'
        'python -m tests.count_floor --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"\n'
    )
    parallel = serial.replace(
        'cd "$RUNNER_TEMP/base"\npython -m tests.count_floor --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"\n',
        """python -c 'import json; from pathlib import Path; from tests.duration_coverage import collected_tests; Path("collected.json").write_text(json.dumps(collected_tests(Path.cwd())))' > "$RUNNER_TEMP/collection.log" 2>&1 &
collection_pid=$!
(
  cd "$RUNNER_TEMP/base"
  python -m tests.count_floor --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"
) &
floor_pid=$!
status=0
wait "$collection_pid" || status=$?
wait "$floor_pid" || status=$?
cat "$RUNNER_TEMP/collection.log"
exit "$status"
""",
    )
    shared = serial + (
        'if [[ ! -f "$TEST_COUNT_HEAD_COLLECTION" ]]; then\n'
        '  cd "$GITHUB_WORKSPACE"\n'
        "  python -c 'import json, os; from pathlib import Path; from tests.duration_coverage import collected_tests; "
        'Path(os.environ["TEST_COUNT_HEAD_COLLECTION"]).write_text(json.dumps(collected_tests(Path.cwd())))\'\n'
        "fi\n"
    )
    assert floor["run"] in (serial, parallel, shared)


def test_dev_records_the_suite_count_and_the_count_floor_reads_it_only_through_the_base_grader():
    jobs = _workflow()["jobs"]
    recorder_path = _ROOT / ".github/workflows/count-record.yml"
    recorder = (
        yaml.safe_load(recorder_path.read_text())["jobs"]["count-record"]
        if recorder_path.exists()
        else jobs["count-record"]
    )
    assert recorder["if"] == "${{ github.event_name == 'push' && github.ref == 'refs/heads/dev' }}"
    assert recorder["continue-on-error"] is True
    assert recorder["strategy"]["matrix"] == jobs["test-count"]["strategy"]["matrix"]
    steps = {step.get("name"): step for step in recorder["steps"]}
    record, upload = steps["Record the suite count for later bases"], steps["Upload the suite count record"]
    assert record["continue-on-error"] is True
    assert "Record the suite count for later bases" not in str(jobs["split"]["steps"])
    assert 'python -m tests.count_floor --record "$RUNNER_TEMP/count-record/record.json"' in record["run"]
    assert 'git worktree add --detach "$RUNNER_TEMP/base" HEAD' in record["run"]
    assert upload["with"]["name"] == "count-record-${{ matrix.python-version }}"
    count = jobs["test-count"]["steps"]
    names = [step.get("name") for step in count]
    fetch = count[names.index("Fetch the base suite count recorded on dev")]
    floor = count[names.index("Hold the base revision's test count")]
    assert names.index("Check out the base revision") < names.index(fetch["name"]) < names.index(floor["name"])
    assert fetch["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert fetch["env"]["RECORD"] == upload["with"]["name"]
    assert 'cd "$RUNNER_TEMP/base" && python -c' in fetch["run"]
    assert "from tests.count_floor import dev_record" in fetch["run"]
    assert floor["env"]["TEST_COUNT_BASE_RECORD"] == "${{ runner.temp }}/base-record.json"
    assert jobs["test-count"]["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("event_name", ["pull_request", "merge_group"])
def test_pull_requests_and_merge_groups_read_the_dev_count_record(event_name):
    from tests.test_ci_hosted_lanes import _evaluate

    fetch = _step("test-count", "Fetch the base suite count recorded on dev")
    condition = fetch["if"].replace("steps.app-token.outcome", "'success'")
    assert _evaluate("${{ " + condition + " }}", event_name=event_name) is True


def test_a_merge_group_records_its_head_count_for_the_next_group_after_its_early_collection():
    from tests.test_ci_hosted_lanes import _evaluate

    recorder = yaml.safe_load((_ROOT / ".github/workflows/count-record.yml").read_text())["jobs"]["count-record"]
    recorded = next(step for step in recorder["steps"] if step.get("name") == "Upload the suite count record")
    count = _workflow()["jobs"]["test-count"]["steps"]
    names = [step.get("name") for step in count]
    record = count[names.index("Record the head count for the next queue group")]
    upload = count[names.index("Upload the head count for the next queue group")]
    assert names.index("Upload the early collection") < names.index(record["name"]) < names.index(upload["name"])
    for step in (record, upload):
        assert _evaluate("${{ " + step["if"] + " }}", event_name="merge_group") is True
        assert _evaluate("${{ " + step["if"] + " }}", event_name="pull_request") is False
    assert record["continue-on-error"] is True
    assert 'cmp -s "$RUNNER_TEMP/base/tests/count_floor.py" tests/count_floor.py' in record["run"]
    assert '--collected "$RUNNER_TEMP/collected.json"' in record["run"]
    assert upload["with"]["name"] == recorded["with"]["name"]
    assert upload["with"]["if-no-files-found"] == "ignore"
    assert upload["uses"] == recorded["uses"] == "./.github/actions/retry-upload"


def test_every_dev_head_records_its_count_outside_the_promotion_queue():
    from scripts.ci_branch_lanes import problems

    workflow = yaml.safe_load((_ROOT / ".github/workflows/count-record.yml").read_text())
    assert workflow[True] == {"push": {"branches": ["dev"]}}
    assert problems(workflow) == []
    assert workflow["concurrency"]["queue"] == "max"
    assert workflow["concurrency"].get("cancel-in-progress", False) is False
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"count-record"}
    assert "needs" not in workflow["jobs"]["count-record"]
    assert "count-record" not in _workflow()["jobs"]


@pytest.mark.parametrize("answer", ["fail", "none", "branch", "badzip", "match"])
def test_fetching_the_dev_count_record_never_fails_and_writes_only_a_dev_record(tmp_path, answer):
    runner = tmp_path / "runner"
    base = runner / "base"
    (base / "tests").mkdir(parents=True)
    (base / "tests" / "count_floor.py").write_text((_ROOT / "tests" / "count_floor.py").read_text())
    git = ["git", "-C", str(base), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "tests"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    run = {"head_branch": "ci-plant" if answer == "branch" else "dev", "head_sha": sha}
    listing = {"artifacts": [{"id": 5, "workflow_run": {**run, "repository_id": 1, "head_repository_id": 1}}]}
    archive = tmp_path / "record.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("record.json", '{"identity": "x", "nodeids": ["t"]}')
    (tmp_path / "listing.json").write_text(json.dumps({"artifacts": []} if answer == "none" else listing))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'[[ "{answer}" == fail ]] && exit 1\n'
        f'if [[ "$2" == *"/zip" ]]; then [[ "{answer}" == badzip ]] && echo junk || cat "{archive}"; exit 0; fi\n'
        f'cat "{tmp_path / "listing.json"}"\n'
    )
    gh.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{Path(sys.executable).parent}:{os.environ['PATH']}",
        RUNNER_TEMP=str(runner),
        GITHUB_REPOSITORY="o/r",
        RECORD="count-record-3.12",
    )
    step = _step("test-count", "Fetch the base suite count recorded on dev")
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    record = runner / "base-record.json"
    assert record.is_file() == (answer == "match")
    if answer == "match":
        assert json.loads(record.read_text())["nodeids"] == ["t"]


def test_lint_and_test_count_grade_a_pull_request_that_merged_dev_from_the_current_merge_base(tmp_path):
    from tests.test_ci_hosted_lanes import _evaluate

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    pinned = _commit(repo, {"docs/base.md": "a"})
    _git(repo, "checkout", "-q", "-b", "branch")
    _commit(repo, {"docs/own.md": "b"})
    _git(repo, "checkout", "-q", "dev")
    merged = _commit(repo, {"docs/dev.md": "c", ".github/dev.yml": "d"})
    _git(repo, "update-ref", "refs/remotes/origin/dev", merged)
    _git(repo, "checkout", "-q", "branch")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-edit", "dev")
    selector = repo / ".github/actions/browser-cache/select-artifacts.sh"
    selector.parent.mkdir(parents=True)
    selector.write_text((_ROOT / ".github/actions/browser-cache/select-artifacts.sh").read_text())
    jobs = _workflow()["jobs"]
    steps = {
        "test-count": _step("test-count", "Check out the base revision"),
        "lint": _step("lint", "Check out the base revision"),
        "docs": next(s for s in jobs.get("docs", jobs["lint"])["steps"] if s.get("id") == "docs"),
        "artifacts": next(s for s in jobs["lint"]["steps"] if s.get("id") == "artifacts"),
    }
    event = {"pull_request": {"base": {"sha": pinned}}}
    logs = {}
    for name, step in steps.items():
        (tmp_path / name).mkdir()
        base = _evaluate(step["env"]["BASE"], event_name="pull_request", base_ref="dev", event=event)
        env = dict(os.environ, BASE=base, RUNNER_TEMP=str(tmp_path / name), GITHUB_OUTPUT=str(tmp_path / name / "out"))
        done = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=repo, env=env, check=True, capture_output=True)
        logs[name] = done.stdout.decode()
    assert [line for line in logs["test-count"].splitlines() if line.startswith("BASE:")] in ([], [f"BASE: {merged}"])
    assert _git(tmp_path / "test-count" / "base", "rev-parse", "HEAD") == merged
    assert _git(tmp_path / "lint" / "base", "rev-parse", "HEAD") == merged
    assert (tmp_path / "docs" / "docs-changes").read_text() == "docs/own.md\n"
    assert (tmp_path / "artifacts" / "out").read_text() == "browser=false\n"


def test_coverage_ratchet_grades_the_merged_shards_from_the_base_copy():
    jobs = _workflow()["jobs"]
    job = jobs["coverage-ratchet"]
    assert job["needs"] in (["durations", "unit", "reuse"], ["select", "unit", "reuse"])
    download = next(step for step in job["steps"] if step.get("name") == "Download shard coverage")
    assert download["with"]["pattern"] == "coverage-3.12-*"
    grade = next(step for step in job["steps"] if step.get("name") == "Hold every line the base ran")
    assert grade["run"].index('cd "$RUNNER_TEMP/base"') < grade["run"].index("python -m tests.coverage_ratchet")
    assert f"--shards {jobs['unit']['strategy']['matrix']['shard'][-1]}" in grade["run"]
    report = job["steps"][-1]
    assert report["if"].startswith("always()")
    assert report["with"]["path"] == "coverage-ratchet/report.txt"


def test_every_coverage_baseline_records_all_the_shards_the_unit_job_splits():
    jobs = _workflow()["jobs"]
    record = next(
        entry for entry in jobs["coverage-baseline"]["steps"] if entry.get("name") == "Compute the coverage baseline"
    )
    assert f"--shards {jobs['unit']['strategy']['matrix']['shard'][-1]} " in record["run"]


def test_coverage_ratchet_grades_a_merge_group_against_the_branch_it_queues_onto():
    job = _workflow()["jobs"]["coverage-ratchet"]
    base = next(step for step in job["steps"] if step.get("name") == "Resolve the measured base tree")
    assert base["env"]["QUEUE_BASE"] == "${{ github.event.merge_group.base_sha }}"
    assert '"${QUEUE_BASE:-' in base["run"]


def _step(job, name):
    return next(step for step in _workflow()["jobs"][job]["steps"] if step.get("name") == name)


@pytest.mark.parametrize(
    ("queue", "dispatched", "graded"),
    [("", "", "parent"), ("", "parent", "parent"), ("newer", "", "newer")],
)
def test_coverage_ratchet_grades_against_the_base_its_baseline_was_restored_for(tmp_path, queue, dispatched, graded):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    commits = {
        "parent": _commit(repo, {"a": "1"}),
        "head": _commit(repo, {"a": "2"}),
        "newer": _commit(repo, {"a": "3"}),
    }
    _git(repo, "checkout", "-q", "--detach", commits["head"])
    output = tmp_path / "output"
    output.write_text("")
    env = dict(
        os.environ,
        QUEUE_BASE=commits.get(queue, ""),
        DISPATCHED_BASE=commits.get(dispatched, ""),
        GITHUB_OUTPUT=str(output),
    )
    job = _workflow()["jobs"]["coverage-ratchet"]
    assert job["needs"][0] in ("durations", "select")
    step = _step("coverage-ratchet", "Resolve the measured base tree")
    producer = "durations" if "durations" in _workflow()["jobs"] else "select"
    assert step["env"]["DISPATCHED_BASE"] == "${{ needs." + producer + ".outputs.base }}"
    subprocess.run(["bash", "-e", "-c", step["run"]], cwd=repo, env=env, check=True)
    assert f"commit={commits[graded]}\n" in output.read_text()


@pytest.mark.parametrize(
    ("dispatched", "ref", "restored"),
    [("head", "feature", "parent"), ("other", "feature", "other"), ("origin/dev", "dev", "parent")],
)
def test_durations_restores_the_parent_baseline_when_the_dispatched_base_is_the_head(
    tmp_path, dispatched, ref, restored
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$2" in\n'
        "  */compare/*) echo ahead ;;\n"
        '  */jobs*) jq -nc \'[range(1;13) | {name: ("unit (3.12, " + tostring + ")"), conclusion: "success"}] + [range(1;7) | {name: ("unit (3.11, " + tostring + ")"), conclusion: "success"}] + [{name: "coverage-baseline", conclusion: "success", steps: [{name: "Compute the coverage baseline", conclusion: "success"}, {name: "Publish the passed dev coverage baseline", conclusion: "success"}]}]\' | jq -c \'.[]\' ;;\n'
        "  */commits/head) [[ $4 == .sha ]] && echo head || echo parent ;;\n"
        "  */commits/*) echo other ;;\n"
        '  */runs\\?*) s="${2##*head_sha=}"; s="${s%%&*}"; echo "run-$s $s" ;;\n'
        "  *) echo durations-merged coverage-baseline ;;\n"
        "esac\n"
    )
    (bin_dir / "gh").chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        BASE=dispatched,
        GITHUB_SHA="head",
        GITHUB_REF_NAME=ref,
        GITHUB_REPOSITORY="o/r",
        GITHUB_OUTPUT=str(output),
    )
    step = _step(
        "durations" if "durations" in _workflow()["jobs"] else "select", "Find the dev push run of the dispatched base"
    )
    subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, env=env, check=True)
    assert output.read_text() == f"id=run-{restored}\nsha={restored}\n"


def test_coverage_ratchet_restores_a_missed_base_baseline_from_a_passed_run_of_the_base():
    steps = _workflow()["jobs"]["coverage-ratchet"]["steps"]
    names = [step.get("name") for step in steps]
    cached = _step("coverage-ratchet", "Restore the exact coverage baseline")
    mint = _step("coverage-ratchet", "Mint the tcc main ci App token")
    find = _step("coverage-ratchet", "Find the run that published the base baseline")
    download = _step("coverage-ratchet", "Download the base baseline its run published")
    assert names.index(cached["name"]) < names.index(mint["name"]) < names.index(find["name"])
    assert names.index(find["name"]) < names.index(download["name"]) < names.index("Hold every line the base ran")
    assert mint["if"] == (
        "steps.cached.outcome == 'success' && steps.cached.outputs.cache-hit != 'true'"
        " && steps.restored.outputs.commit == ''"
    )
    assert download["with"]["run-id"] == "${{ steps.base-run.outputs.id }}"
    assert download["with"]["name"] == "coverage-baseline"


def test_a_superseded_base_falls_back_to_the_newest_dev_baseline_before_the_artifact_lookup():
    steps = _workflow()["jobs"]["coverage-ratchet"]["steps"]
    names = [step.get("name") for step in steps]
    cached = _step("coverage-ratchet", "Restore the exact coverage baseline")
    restored = _step("coverage-ratchet", "Grade from the newest dev baseline when the base run published none")
    hold = _step("coverage-ratchet", "Hold every line the base ran")
    assert cached["with"]["restore-keys"] == "coverage-baseline-"
    assert names.index(cached["name"]) < names.index(restored["name"]) < names.index("Mint the tcc main ci App token")
    assert restored["if"] == (
        "steps.cached.outputs.cache-hit != 'true' && steps.cached.outputs.cache-matched-key != ''"
    )
    assert hold["env"]["BASE"] == "${{ steps.restored.outputs.commit || steps.base.outputs.commit }}"


@pytest.mark.parametrize("measured", ["ancestor", "unrelated", "unrecorded"])
def test_the_newest_dev_baseline_grades_only_from_an_ancestor_of_the_head(tmp_path, measured):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)

    def commit(message):
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", message], check=True)
        return subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    ancestor = commit("ancestor")
    head = commit("head")
    subprocess.run([*git, "checkout", "-q", "--orphan", "side"], check=True)
    unrelated = commit("unrelated")
    subprocess.run([*git, "checkout", "-q", head], check=True)
    baseline = tmp_path / "coverage-baseline"
    baseline.mkdir()
    recorded = {"ancestor": {"commit": ancestor}, "unrelated": {"commit": unrelated}, "unrecorded": {}}[measured]
    (baseline / "baseline.json").write_text(json.dumps({"tree": "t", **recorded}))
    output = tmp_path / "output"
    output.write_text("")
    env = dict(os.environ, HOME=str(tmp_path), GITHUB_OUTPUT=str(output))
    step = _step("coverage-ratchet", "Grade from the newest dev baseline when the base run published none")
    subprocess.run(["bash", "-e", "-c", step["run"]], cwd=repo, env=env, check=True)
    assert output.read_text() == (f"commit={ancestor}\n" if measured == "ancestor" else "")


@pytest.mark.parametrize(("kept", "found"), [("0 1", "id=run-2\n"), ("0 0", None)])
def test_the_missed_baseline_comes_from_the_first_passed_run_that_kept_it(tmp_path, kept, found):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    first, second = kept.split()
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$2" in\n'
        "  */compare/*) echo ahead ;;\n"
        '  */jobs*) jq -nc \'[range(1;13) | {name: ("unit (3.12, " + tostring + ")"), conclusion: "success"}] + [range(1;7) | {name: ("unit (3.11, " + tostring + ")"), conclusion: "success"}] + [{name: "coverage-baseline", conclusion: "success", steps: [{name: "Compute the coverage baseline", conclusion: "success"}, {name: "Publish the passed dev coverage baseline", conclusion: "success"}]}]\' | jq -c \'.[]\' ;;\n'
        '  */runs\\?*) if [[ $2 == *"status=success"* ]]; then printf "run-1\\nrun-2\\n"; else printf "run-1 push\\nrun-2 push\\n"; fi ;;\n'
        f"  */run-1/*) echo {first} ;;\n"
        f"  */run-2/*) echo {second} ;;\n"
        "esac\n"
    )
    (bin_dir / "gh").chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", BASE="base", GITHUB_REPOSITORY="o/r")
    env["GITHUB_OUTPUT"] = str(output)
    step = _step("coverage-ratchet", "Find the run that published the base baseline")
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (found is not None)
    assert output.read_text() == (found or "")


def test_coverage_ratchet_grades_a_batched_dev_push_against_the_previous_tip(tmp_path):
    base = next(
        step
        for step in _workflow()["jobs"]["coverage-ratchet"]["steps"]
        if step.get("name") == "Resolve the measured base tree"
    )
    assert base["env"]["PUSH_BEFORE"] == "${{ github.event_name == 'push' && github.event.before || '' }}"
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    tips = []
    for _ in range(3):
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "c"], check=True)
        tips.append(subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
    output = tmp_path / "output"
    env = dict(os.environ, PUSH_BEFORE=tips[0], QUEUE_BASE="", DISPATCHED_BASE="", GITHUB_OUTPUT=str(output))
    subprocess.run(["bash", "-e", "-c", base["run"]], cwd=tmp_path, env=env, check=True)
    assert f"commit={tips[0]}" in output.read_text().splitlines()


def test_test_count_refuses_a_base_without_its_grader(tmp_path):
    floor = _step("test-count", "Hold the base revision's test count")
    (tmp_path / "base").mkdir()
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), GITHUB_WORKSPACE=str(tmp_path))
    result = subprocess.run(["bash", "-e", "-c", floor["run"]], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "nothing trusted can grade" in result.stdout


def test_coverage_ratchet_refuses_a_base_without_its_grader(tmp_path):
    graders = [
        step["run"]
        for job in _workflow()["jobs"].values()
        for step in job.get("steps", [])
        if re.search(r"-m (tests\.(coverage_ratchet|count_floor)|scripts\.ci_dependency_audit)\b", step.get("run", ""))
    ]
    assert len(graders) >= 3
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    base = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "coverage_ratchet.py").write_text("from pathlib import Path\nPath('head-graded').touch()\n")
    (tmp_path / "runner").mkdir()
    env = dict(os.environ, BASE=base, RUNNER_TEMP=str(tmp_path / "runner"), GITHUB_WORKSPACE=str(tmp_path))
    step = _step("coverage-ratchet", "Hold every line the base ran")
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "The base carries no tests/coverage_ratchet.py, so nothing trusted can grade." in result.stdout
    assert not (tmp_path / "head-graded").exists()
    prefixes = [re.split(r"-m (?:tests|scripts)\.\w+", run)[0] for run in graders]
    assert all("GITHUB_WORKSPACE" not in prefix and 'cd "$RUNNER_TEMP/base"' in prefix for prefix in prefixes)


@pytest.mark.parametrize("unit", ["success", "failure", "skipped", "cancelled", "pending"])
@pytest.mark.parametrize("lint", ["success", "failure", "skipped", "cancelled", "pending"])
def test_required_gate_rejects_every_non_success_result(unit, lint):
    step = _workflow()["jobs"]["gate-required"]["steps"][0]
    assert step["env"]["NEEDS"] == "${{ toJSON(needs) }}"
    env = dict(os.environ, NEEDS=json.dumps({"unit": {"result": unit}, "lint": {"result": lint}}))
    result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (unit == lint == "success"), result.stdout + result.stderr
    if result.returncode:
        assert "::error::" in result.stdout


@pytest.mark.parametrize("durations", ["success", "failure", "skipped", "cancelled"])
def test_required_gate_is_red_when_the_durations_lookup_did_not_succeed(durations):
    jobs = _workflow()["jobs"]
    gate = jobs["gate-required"]
    producer = "durations" if "durations" in jobs else "select"
    assert producer in gate["needs"]
    needs = {producer: {"result": durations}, "unit": {"result": "skipped" if durations != "success" else "success"}}
    env = dict(os.environ, NEEDS=json.dumps(needs), MUTATION="false")
    result = subprocess.run(["bash", "-e", "-c", gate["steps"][0]["run"]], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (durations == "success"), result.stdout + result.stderr


@pytest.mark.parametrize("job", ["mutation", "mutation-plan", "mutation-stats"])
@pytest.mark.parametrize("mutation", ["success", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("expected", ["true", "false", ""])
def test_required_gate_is_red_unless_mutation_passed_or_was_not_due(job, mutation, expected):
    jobs = _workflow()["jobs"]
    gate = jobs["gate-required"]
    step = gate["steps"][0]
    assert job in gate["needs"]
    assert jobs[job]["if"].startswith("${{")
    assert step["env"]["MUTATION"] == (
        "${{ (github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch' }}"
    )
    needs = {name: {"result": "success"} for name in ("unit", "lint", "sonar")}
    needs[job] = {"result": mutation}
    env = dict(os.environ, NEEDS=json.dumps(needs), MUTATION=expected)
    result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    passes = mutation == "success" or (mutation == "skipped" and expected == "false")
    assert (result.returncode == 0) == passes, result.stdout + result.stderr
    if result.returncode:
        assert "::error::" in result.stdout


@pytest.mark.parametrize("due_job", ["success", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("kind", ["success", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("due", ["true", "false", ""])
def test_required_gate_accepts_a_skipped_chart_proof_only_when_its_path_rule_said_not_due(due_job, kind, due):
    jobs = _workflow()["jobs"]
    gate = jobs["gate-required"]
    step = gate["steps"][0]
    assert {"kind-due", "helm-kind"} <= set(gate["needs"])
    assert jobs["helm-kind"]["needs"] == "kind-due"
    assert jobs["helm-kind"]["if"] in (
        "${{ needs.kind-due.outputs.due == 'true' }}",
        "${{ !cancelled() && needs.kind-due.result == 'success' && needs.kind-due.outputs.due == 'true' }}",
        "${{ !cancelled() && github.event_name != 'merge_group' && github.event_name != 'pull_request' && needs.kind-due.result == 'success' && needs.kind-due.outputs.due == 'true' }}",
    )
    assert step["env"]["KIND"] == "${{ needs.kind-due.outputs.due }}"
    needs = {"kind-due": {"result": due_job}, "helm-kind": {"result": kind}}
    env = dict(os.environ, NEEDS=json.dumps(needs), MUTATION="false", KIND=due)
    result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    passes = due_job == "success" and (kind == "success" or (kind == "skipped" and due == "false"))
    assert (result.returncode == 0) == passes, result.stdout + result.stderr


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo, files):
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return _git(repo, "rev-parse", "HEAD")


def test_chart_proof_is_due_only_when_its_input_key_has_no_passed_run():
    job = _workflow()["jobs"]["kind-due"]
    assert job["outputs"]["due"] == "${{ steps.key.outputs.hit != 'true' }}"
    key = job["steps"][-1]
    assert key["id"] == "key"
    assert key["uses"] == "./.github/actions/input-key"
    assert key["with"] == {"job": "helm-kind"}
    assert not (_ROOT / "deploy/helm/agentihooks-swarm/ci/kind-due.sh").exists()


@pytest.mark.parametrize(
    ("path", "keyed"),
    [
        ("deploy/helm/agentihooks-swarm/templates/controller.yaml", True),
        ("deploy/helm/agentihooks-swarm/ci/kind-smoke.sh", True),
        ("deploy/helm/agentihooks-swarm/ci/terminal-worker.Dockerfile", True),
        ("Dockerfile", True),
        (".dockerignore", True),
        ("pyproject.toml", True),
        ("README.md", True),
        (".github/workflows/helm-kind.yml", True),
        ("docker/swarm/requirements.lock", True),
        ("docker/swarm-node/versions.lock", True),
        ("docker/swarm-node/layout.json", True),
        ("scripts/swarm/lease.py", True),
        ("scripts/swarm_ledger/new_ledger.py", True),
        ("hooks/hook_manager.py", True),
        ("profiles/package/rules/x.md", True),
        ("media/agentihooks-logo.png", True),
        ("docs/swarm-v2/schemas/x.json", True),
        ("tests/kind_request.py", True),
        ("tests/chart_redis.py", True),
        ("tests/fixtures/swarm_v2/pod-rendered.json", True),
        ("tests/test_x.py", False),
        ("docs/x.md", False),
        (".github/workflows/semgrep.yml", False),
        (".github/workflows/test.yml", False),
        ("deploy/helm/other/values.yaml", False),
        ("deploy/helm/agentihooks-swarm-other/values.yaml", False),
    ],
)
def test_chart_proof_input_key_declares_the_chart_image_and_proof_files_only(path, keyed):
    listed = json.loads((_ROOT / ".github/input-keys.json").read_text())["helm-kind"]["paths"]
    assert any(path == p or path.startswith(f"{p}/") for p in listed) is keyed


def test_chart_proof_input_key_covers_every_file_the_swarm_image_copies():
    listed = json.loads((_ROOT / ".github/input-keys.json").read_text())["helm-kind"]["paths"]
    sources = []
    for line in (_ROOT / "Dockerfile").read_text().splitlines():
        words = line.split()
        if words[:1] == ["COPY"] and not any(w.startswith("--from") for w in words):
            sources += words[1:-1]
    assert sources
    assert all(any(s.rstrip("/") == p or s.startswith(f"{p}/") for p in listed) for s in sources), sources
    assert {"Dockerfile", ".dockerignore", "deploy/helm/agentihooks-swarm", ".github/workflows/helm-kind.yml"} <= set(
        listed
    )


def test_unit_and_lint_run_on_every_event_and_feed_the_required_gate():
    jobs = _workflow()["jobs"]
    every_event = "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}"
    expected = {
        "lint": every_event,
        "unit": every_event.removesuffix(" }}")
        + " && (github.event_name != 'pull_request' || needs.select.outputs.scope == 'full') }}",
    }
    for name in ("unit", "lint"):
        job = jobs[name]
        assert job["if"] in (
            expected[name],
            "${{ !cancelled() && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}",
            "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}",
            "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'pull_request' || needs.select.outputs.scope == 'full') }}",
            "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && github.event_name != 'merge_group' && github.event_name != 'push' }}",
        )

        assert all("steps.lookup" not in step.get("if", "") for step in job["steps"])
    step = jobs["gate-required"]["steps"][0]
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, NEEDS=json.dumps({"unit": {"result": "success"}, "lint": {"result": "success"}})),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_mutation_runs_in_tests_beside_unit_and_lint():
    workflow = _workflow()
    job = workflow["jobs"]["mutation"]
    assert job["needs"] in (["mutation-plan", "mutation-stats"], ["mutation-plan", "mutation-stats", "admission"])
    assert workflow["jobs"]["mutation-stats"]["needs"] in ("mutation-plan", ["mutation-plan", "admission"])
    assert workflow["jobs"]["mutation-plan"].get("needs", []) in ([], ["admission"])
    assert job["if"] in (
        "${{ (github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch' }}",
        "${{ !cancelled() && needs.mutation-plan.result == 'success' && (needs.mutation-plan.outputs.shards != '[0]' || needs.mutation-plan.outputs.stats_parts != '[0]') && needs.mutation-stats.result == 'success' && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && ((github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch') }}",
    )
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]["base"]
    assert dispatch["required"] is True
    assert dispatch["default"] == "origin/dev"
    assert not (_ROOT / ".github/workflows/mutation.yml").exists()
