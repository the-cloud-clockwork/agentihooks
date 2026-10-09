import json
import os
import subprocess
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
    "Fail on known vulnerabilities new against the base",
]
LINT_SETUP = [
    "Install ruff",
    "Install PyYAML",
    "Set up uv",
    "Check out the base revision",
    "Check out the protected grader",
]


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())


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
            "wiring",
            "size",
            "dependency-audit",
            "durations",
            "split",
            "kind-due",
            "helm-kind",
        }
    )
    assert gate["if"] == "${{ always() }}"
    assert jobs["unit"]["needs"] == ["split"]
    assert jobs["split"]["needs"] == ["durations"]
    assert "needs" not in jobs["lint"]
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


def test_post_shard_graders_do_not_wait_on_each_other_and_the_gate_needs_each():
    jobs = _workflow()["jobs"]
    graders = {"shard-check", "coverage-ratchet", "sonar"}
    for name in graders:
        needs = jobs[name]["needs"]
        assert "unit" in needs
        assert not graders & set(needs), f"{name} waits on another post shard grader"
    assert graders <= set(jobs["gate-required"]["needs"])


def test_semgrep_grades_registry_pack_findings_new_against_the_base_in_parallel():
    job = _workflow()["jobs"]["semgrep"]
    assert "needs" not in job
    assert job["uses"] == "./.github/workflows/semgrep.yml"
    assert job["with"]["base"] == (
        "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha"
        " || github.event.before || inputs.base }}"
    )
    scan = yaml.safe_load((_ROOT / ".github/workflows/semgrep.yml").read_text())["jobs"]["scan"]
    assert scan["steps"][0]["with"]["fetch-depth"] == 0
    command = scan["steps"][-1]["run"].split()
    assert {"p/ci", "p/secrets", "p/python"} == {command[i + 1] for i, a in enumerate(command) if a == "--config"}
    assert command[command.index("--baseline-commit") + 1] == '"$BASE"'
    assert {"--error", "--strict", "--verbose"} <= set(command)
    assert int(command[command.index("--timeout") + 1]) >= 30
    assert command[:2] == ["semgrep", "scan"]
    assert not [s for s in ("||", "&&", ";", "exit") if s in scan["steps"][-1]["run"]]
    assert not {"set", "--exclude", "--include"} & set(command)
    assert [command[i + 1] for i, a in enumerate(command) if a == "--exclude-rule"] == [
        "yaml.github-actions.security.github-actions-mutable-action-tag.github-actions-mutable-action-tag"
    ]
    assert all("if" not in step and "continue-on-error" not in step for step in scan["steps"])
    assert not {"if", "continue-on-error"} & (set(scan) | set(job))


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
    assert "needs" not in job
    assert (
        job["strategy"]["matrix"]["python-version"]
        == _workflow()["jobs"]["unit"]["strategy"]["matrix"]["python-version"]
    )
    base, floor = job["steps"][-2:]
    assert base["env"]["BASE"] == (
        "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha"
        " || github.event.before || inputs.base }}"
    )
    assert base["run"] == 'git worktree add --detach "$RUNNER_TEMP/base" "$BASE"'
    assert floor["run"] == (
        'if [[ ! -f "$RUNNER_TEMP/base/tests/count_floor.py" ]]; then\n'
        '  echo "::error::The base carries no tests/count_floor.py, so nothing trusted can grade."\n'
        "  exit 1\n"
        "fi\n"
        'cd "$RUNNER_TEMP/base"\n'
        'python -m tests.count_floor --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"\n'
    )


def test_coverage_ratchet_grades_the_merged_shards_from_the_base_copy():
    jobs = _workflow()["jobs"]
    job = jobs["coverage-ratchet"]
    assert job["needs"] == ["durations", "unit", "queue-baseline"]
    download = next(step for step in job["steps"] if step.get("name") == "Download shard coverage")
    assert download["with"]["pattern"] == "coverage-3.12-*"
    grade = next(step for step in job["steps"] if step.get("name") == "Hold every line the base ran")
    assert '[[ -f "$grader/tests/coverage_ratchet.py" ]] || grader="$GITHUB_WORKSPACE"' in grade["run"]
    assert f"--shards {jobs['unit']['strategy']['matrix']['shard'][-1]}" in grade["run"]
    report = job["steps"][-1]
    assert report["if"].startswith("always()")
    assert report["with"]["path"] == "coverage-ratchet/report.txt"


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
    assert job["needs"][0] == "durations"
    step = _step("coverage-ratchet", "Resolve the measured base tree")
    assert step["env"]["DISPATCHED_BASE"] == "${{ needs.durations.outputs.base }}"
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
    step = _step("durations", "Find the dev push run of the dispatched base")
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
    assert mint["if"] == "steps.cached.outcome == 'success' && steps.cached.outputs.cache-hit != 'true'"
    assert download["with"]["run-id"] == "${{ steps.base-run.outputs.id }}"
    assert download["with"]["name"] == "coverage-baseline"


@pytest.mark.parametrize(("kept", "found"), [("0 1", "id=run-2\n"), ("0 0", None)])
def test_the_missed_baseline_comes_from_the_first_passed_run_that_kept_it(tmp_path, kept, found):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    first, second = kept.split()
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$2" in\n'
        '  */runs\\?*) [[ $2 == *"?head_sha=base&status=success&"* ]] && printf "run-1\\nrun-2\\n" ;;\n'
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
    floor = _workflow()["jobs"]["test-count"]["steps"][-1]
    (tmp_path / "base").mkdir()
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), GITHUB_WORKSPACE=str(tmp_path))
    result = subprocess.run(["bash", "-e", "-c", floor["run"]], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "nothing trusted can grade" in result.stdout


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
    gate = _workflow()["jobs"]["gate-required"]
    assert "durations" in gate["needs"]
    needs = {"durations": {"result": durations}, "unit": {"result": "skipped" if durations != "success" else "success"}}
    env = dict(os.environ, NEEDS=json.dumps(needs), MUTATION="false")
    result = subprocess.run(["bash", "-e", "-c", gate["steps"][0]["run"]], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (durations == "success"), result.stdout + result.stderr


@pytest.mark.parametrize("mutation", ["success", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("expected", ["true", "false", ""])
def test_required_gate_is_red_unless_mutation_passed_or_was_not_due(mutation, expected):
    jobs = _workflow()["jobs"]
    gate = jobs["gate-required"]
    step = gate["steps"][0]
    assert jobs["mutation"]["if"].startswith("${{")
    assert step["env"]["MUTATION"] == jobs["mutation"]["if"]
    needs = {name: {"result": "success"} for name in ("unit", "lint", "sonar")}
    needs["mutation"] = {"result": mutation}
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
    assert jobs["helm-kind"]["if"] == "${{ needs.kind-due.outputs.due == 'true' }}"
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


RULE = "deploy/helm/agentihooks-swarm/ci/kind-due.sh"


@pytest.fixture
def kind_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    base = _commit(repo, {RULE: (_ROOT / RULE).read_text(), "README.md": "a"})
    return repo, base


def _due_step(repo, base, head):
    step = _workflow()["jobs"]["kind-due"]["steps"][-1]
    output = repo.parent / "output"
    output.write_text("")
    env = dict(os.environ, BASE=base, HEAD=head, GITHUB_OUTPUT=str(output))
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=repo, env=env, capture_output=True, text=True)
    return result, output.read_text()


@pytest.mark.parametrize(
    ("path", "due"),
    [
        ("deploy/helm/agentihooks-swarm/templates/controller.yaml", "true"),
        ("deploy/helm/agentihooks-swarm/ci/kind-smoke.sh", "true"),
        ("Dockerfile", "true"),
        ("pyproject.toml", "true"),
        (".github/workflows/helm-kind.yml", "true"),
        ("scripts/swarm/lease.py", "true"),
        ("scripts/swarm/tick.py", "true"),
        ("scripts/swarm/store.py", "true"),
        ("scripts/swarm_ledger/new_ledger.py", "true"),
        ("hooks/hook_manager.py", "true"),
        ("profiles/package/rules/x.md", "true"),
        ("tests/test_x.py", "false"),
        ("docs/x.md", "false"),
        (".github/workflows/semgrep.yml", "false"),
        ("deploy/helm/other/values.yaml", "false"),
    ],
)
def test_chart_proof_is_due_only_when_chart_image_or_its_own_files_change(kind_repo, path, due):
    repo, base = kind_repo
    head = _commit(repo, {path: "changed"})
    result, output = _due_step(repo, base, head)
    assert result.returncode == 0, result.stderr
    assert output == f"due={due}\n"


def test_chart_proof_rule_covers_every_file_the_swarm_image_copies():
    rule = (_ROOT / RULE).read_text()
    listed = rule[rule.index("paths=(") : rule.index(")", rule.index("paths=("))].split()[1:]
    sources = []
    for line in (_ROOT / "Dockerfile").read_text().splitlines():
        words = line.split()
        if words[:1] == ["COPY"] and not any(w.startswith("--from") for w in words):
            sources += words[1:-1]
    assert sources
    assert all(any(s == p or s.startswith(p) for p in listed) for s in sources), sources
    assert {"Dockerfile", ".dockerignore", "deploy/helm/agentihooks-swarm/", ".github/workflows/helm-kind.yml"} <= set(
        listed
    )


def test_chart_proof_rule_is_read_from_the_base_so_a_head_cannot_switch_it_off(kind_repo):
    repo, base = kind_repo
    head = _commit(repo, {RULE: "#!/usr/bin/env bash\necho due=false\n", "Dockerfile": "x"})
    result, output = _due_step(repo, base, head)
    assert result.returncode == 0, result.stderr
    assert output == "due=true\n"


def test_chart_proof_runs_on_dispatch_where_no_base_is_given(kind_repo):
    repo, _ = kind_repo
    head = _commit(repo, {"README.md": "b"})
    result, output = _due_step(repo, "", head)
    assert result.returncode == 0, result.stderr
    assert output == "due=true\n"


@pytest.mark.parametrize("base", ["0" * 40, "without-rule"])
def test_chart_proof_runs_when_the_base_is_unknown_or_carries_no_rule(kind_repo, base):
    repo, _ = kind_repo
    if base == "without-rule":
        _git(repo, "rm", "-q", RULE)
        base = _commit(repo, {})
    head = _commit(repo, {"docs/x.md": "b"})
    result, output = _due_step(repo, base, head)
    assert result.returncode == 0, result.stderr
    assert output == "due=true\n"


def test_chart_proof_rule_uses_the_push_before_commit_as_its_base():
    step = _workflow()["jobs"]["kind-due"]["steps"][-1]
    assert step["env"]["BASE"] == (
        "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha || github.event.before }}"
    )
    assert step["env"]["HEAD"] == (
        "${{ github.event.pull_request.head.sha || github.event.merge_group.head_sha || github.sha }}"
    )
    assert step["run"].startswith("set -o pipefail\n")


def test_chart_proof_rule_fails_on_an_unknown_base(kind_repo):
    repo, base = kind_repo
    result = subprocess.run(["bash", str(repo / RULE), "f" * 40, base], cwd=repo, capture_output=True, text=True)
    assert result.returncode != 0
    assert "due=" not in result.stdout


def test_unit_and_lint_run_on_every_event_and_feed_the_required_gate():
    jobs = _workflow()["jobs"]
    for name in ("unit", "lint"):
        job = jobs[name]
        assert "if" not in job
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
    assert "needs" not in job
    assert (
        job["if"]
        == "${{ (github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch' }}"
    )
    dispatch = workflow[True]["workflow_dispatch"]["inputs"]["base"]
    assert dispatch["required"] is True
    assert dispatch["default"] == "origin/dev"
    assert not (_ROOT / ".github/workflows/mutation.yml").exists()
