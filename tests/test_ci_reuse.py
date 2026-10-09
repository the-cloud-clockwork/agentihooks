import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
PROGRAM = Path(__file__).resolve().parents[1] / "scripts/ci_reuse.py"


def git(root, *args, input=None):
    return subprocess.run(
        ["git", "-C", str(root), *args], input=input, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def reuse_repo(tmp_path, request):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "dev")
    git(root, "config", "user.email", "ci@example.invalid")
    git(root, "config", "user.name", "CI")
    workflows = root / ".github/workflows"
    workflows.mkdir(parents=True)
    workflow = {
        "jobs": {
            "reuse": {},
            "unit": {"strategy": {"matrix": {"python-version": ["3.11", "3.12"], "shard": [1]}}},
            "lint": {},
            "queue-baseline": {},
            "gate-required": {"name": "Gate — Required", "needs": ["reuse", "unit", "lint", "queue-baseline"]},
        }
    }
    if getattr(request, "param", None) == "dynamic":
        workflow["jobs"]["mutation"] = {"strategy": {"matrix": {"shard": "${{ fromJSON(needs.plan.outputs.shards) }}"}}}
        workflow["jobs"]["gate-required"]["needs"].append("mutation")
    (workflows / "test.yml").write_text(yaml.safe_dump(workflow))
    git(root, "add", ".github/workflows/test.yml")
    git(root, "commit", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "code.txt").write_text("tested\n")
    git(root, "add", "code.txt")
    git(root, "commit", "-m", "head")
    head = git(root, "rev-parse", "HEAD")
    queue = git(root, "commit-tree", git(root, "rev-parse", "HEAD^{tree}"), "-p", base, input="queue\n")
    git(root, "switch", "--detach", base)
    return root, base, head, queue


def invoke(root, base, head, event, output, env=None):
    from scripts import ci_reuse

    args = [
        "--base",
        base,
        "--head",
        head,
        "--event",
        event,
        "--repository",
        "o/r",
        "--run",
        "7",
        "--attempt",
        "1",
        "--record",
        str(output),
    ]
    stdout, stderr = io.StringIO(), io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        patch.delenv("GITHUB_OUTPUT", raising=False)
        for key, value in (env or {}).items():
            patch.setenv(key, value)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = ci_reuse.main(args)
    return subprocess.CompletedProcess(args, code, stdout.getvalue(), stderr.getvalue())


@pytest.fixture
def full_source(reuse_repo, tmp_path):
    root, base, head, queue = reuse_repo
    original = tmp_path / "original.json"
    result = invoke(root, base, head, "pull_request", original)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(original.read_text())
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("provenance.json", json.dumps(record))
    source = {
        "id": 7,
        "run_attempt": 1,
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "head_sha": head,
    }
    names = ["reuse", "unit (3.11, 1)", "unit (3.12, 1)", "lint", "Gate — Required"]
    jobs = [{"id": 91 + n, "name": name, "conclusion": "success"} for n, name in enumerate(names)]
    if "mutation" in yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]:
        jobs.extend([{"id": 98 + n, "name": f"mutation ({n})", "conclusion": "success"} for n in range(2)])
    jobs.append({"id": 97, "name": "queue-baseline", "conclusion": "skipped"})
    artifacts = [{"id": 42, "name": "required-tree-1", "expired": False}]
    artifacts.append({"id": 51, "name": "coverage-3.12-1", "expired": False})
    responses = {
        "repos/o/r/actions/workflows/test.yml/runs?event=pull_request&status=success&per_page=20": {
            "workflow_runs": [source]
        },
        "repos/o/r/actions/runs/7/attempts/1/jobs?per_page=100&page=1": {"total_count": len(jobs), "jobs": jobs},
        "repos/o/r/actions/runs/7/artifacts?per_page=100&page=1": {
            "total_count": len(artifacts),
            "artifacts": artifacts,
        },
        "repos/o/r/actions/artifacts/42/zip": {"binary": base64.b64encode(archive.getvalue()).decode()},
        "repos/o/r/actions/jobs/91/logs": {
            "binary": base64.b64encode(
                "".join(f"2026-10-09T11:00:00.000Z {line}\n" for line in result.stdout.splitlines()).encode()
            ).decode()
        },
    }
    fixture = tmp_path / "responses.json"
    fixture.write_text(json.dumps(responses))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env python3\n"
        "import base64, json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1] != 'api': sys.exit(2)\n"
        "value = json.loads(Path(os.environ['REUSE_FIXTURE']).read_text())[sys.argv[2]]\n"
        "if 'binary' in value: sys.stdout.buffer.write(base64.b64decode(value['binary']))\n"
        "else: print(json.dumps(value))\n"
    )
    (bin_dir / "gh").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", REUSE_FIXTURE=str(fixture))
    env.pop("GITHUB_OUTPUT", None)
    return root, base, head, queue, env, responses, record


def test_an_identical_queue_tree_reuses_a_full_passed_run(full_source, tmp_path):
    root, base, head, queue, env, _, _ = full_source
    output = tmp_path / "queue.json"
    result = invoke(root, base, queue, "merge_group", output, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=true" in result.stdout
    assert "run=7" in result.stdout
    assert json.loads(output.read_text())["tree"] == git(root, "rev-parse", f"{head}^{{tree}}")


def test_a_one_line_difference_runs_fully(full_source, tmp_path):
    root, base, _, _, env, _, _ = full_source
    (root / "code.txt").write_text("planted\n")
    git(root, "add", "code.txt")
    plant = git(root, "commit-tree", git(root, "write-tree"), "-p", base, input="plant\n")
    result = invoke(root, base, plant, "merge_group", tmp_path / "plant.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout


def test_overwritten_metadata_cannot_claim_the_planted_tree_passed(full_source, tmp_path):
    root, base, _, _, env, responses, record = full_source
    (root / "code.txt").write_text("planted\n")
    git(root, "add", "code.txt")
    plant = git(root, "commit-tree", git(root, "write-tree"), "-p", base, input="plant\n")
    record["commit"] = plant
    record["tree"] = git(root, "rev-parse", f"{plant}^{{tree}}")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("provenance.json", json.dumps(record))
    responses["repos/o/r/actions/artifacts/42/zip"] = {"binary": base64.b64encode(archive.getvalue()).decode()}
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, plant, "merge_group", tmp_path / "forged.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout


def test_the_required_workflow_uses_the_protected_canonical_aggregation():
    from scripts import ci_reuse

    workflow = yaml.safe_load((PROGRAM.parents[1] / ".github/workflows/test.yml").read_text())
    assert workflow["jobs"]["gate-required"]["steps"] == ci_reuse.gate_steps()
    assert workflow["jobs"]["reuse"] == ci_reuse.reuse_job()


@pytest.mark.parametrize("reuse_repo", ["dynamic"], indirect=True)
def test_a_full_pass_includes_every_dynamic_mutation_shard(full_source, tmp_path):
    root, base, _, queue, env, _, _ = full_source
    result = invoke(root, base, queue, "merge_group", tmp_path / "dynamic.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=true" in result.stdout


def publish_record(env, responses, record):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("provenance.json", json.dumps(record))
    responses["repos/o/r/actions/artifacts/42/zip"] = {"binary": base64.b64encode(archive.getvalue()).decode()}
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    log = f"2026-10-09T11:00:00.000Z required-tree-sha256={digest}\n"
    responses["repos/o/r/actions/jobs/91/logs"] = {"binary": base64.b64encode(log.encode()).decode()}
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))


@pytest.mark.parametrize(
    "field, value",
    [("version", 0), ("event", "push"), ("reused", True), ("run", 8), ("attempt", 2), ("tree", "different")],
)
def test_incompatible_authenticated_source_metadata_runs_fully(full_source, tmp_path, field, value):
    root, base, _, queue, env, responses, record = full_source
    record[field] = value
    publish_record(env, responses, record)
    result = invoke(root, base, queue, "merge_group", tmp_path / "different.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout


@pytest.mark.parametrize("field", ["base", "grader", "workflow", "day"])
def test_any_changed_trusted_grading_input_runs_fully(full_source, tmp_path, field):
    root, base, _, queue, env, responses, record = full_source
    record["inputs"][field] = "different"
    publish_record(env, responses, record)
    result = invoke(root, base, queue, "merge_group", tmp_path / "inputs.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout


@pytest.mark.parametrize("name", ["reuse", "unit (3.11, 1)", "unit (3.12, 1)", "lint", "Gate — Required"])
@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled", None])
def test_every_required_source_job_must_have_passed(full_source, tmp_path, name, outcome):
    root, base, _, queue, env, responses, _ = full_source
    jobs = responses["repos/o/r/actions/runs/7/attempts/1/jobs?per_page=100&page=1"]["jobs"]
    next(job for job in jobs if job["name"] == name)["conclusion"] = outcome
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "failed.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout


@pytest.mark.parametrize(
    "kind", ["missing-unit", "missing-metadata", "expired-metadata", "expired-coverage", "bad-archive", "missing-log"]
)
def test_unavailable_source_evidence_runs_fully(full_source, tmp_path, kind):
    root, base, _, queue, env, responses, _ = full_source
    if kind == "missing-unit":
        body = responses["repos/o/r/actions/runs/7/attempts/1/jobs?per_page=100&page=1"]
        body["jobs"] = [job for job in body["jobs"] if job["name"] != "unit (3.12, 1)"]
        body["total_count"] = len(body["jobs"])
    elif kind == "bad-archive":
        responses["repos/o/r/actions/artifacts/42/zip"] = {"binary": base64.b64encode(b"invalid").decode()}
    elif kind == "missing-log":
        responses.pop("repos/o/r/actions/jobs/91/logs")
    else:
        body = responses["repos/o/r/actions/runs/7/artifacts?per_page=100&page=1"]
        if kind == "missing-metadata":
            body["artifacts"] = [item for item in body["artifacts"] if item["name"] != "required-tree-1"]
            body["total_count"] = len(body["artifacts"])
        else:
            name = "coverage-3.12-1" if kind == "expired-coverage" else "required-tree-1"
            next(item for item in body["artifacts"] if item["name"] == name)["expired"] = True
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "missing.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout


@pytest.mark.parametrize("field, value", [("event", "push"), ("status", "in_progress"), ("conclusion", "failure")])
def test_a_source_run_must_be_a_completed_successful_required_run(full_source, tmp_path, field, value):
    root, base, _, queue, env, responses, _ = full_source
    source = responses["repos/o/r/actions/workflows/test.yml/runs?event=pull_request&status=success&per_page=20"][
        "workflow_runs"
    ][0]
    source[field] = value
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "run.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout


def test_the_protected_script_runs_in_isolated_cli_mode(reuse_repo, tmp_path):
    root, base, head, _ = reuse_repo
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(PROGRAM),
            "--base",
            base,
            "--head",
            head,
            "--event",
            "pull_request",
            "--repository",
            "o/r",
            "--run",
            "7",
            "--attempt",
            "1",
            "--record",
            str(tmp_path / "cli.json"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout
    assert json.loads((tmp_path / "cli.json").read_text())["tree"] == git(root, "rev-parse", f"{head}^{{tree}}")


def test_reused_outputs_append_to_the_github_action_output_file(full_source, tmp_path):
    root, base, _, queue, env, _, _ = full_source
    target = tmp_path / "action-output"
    target.write_text("existing=kept\n")
    result = invoke(root, base, queue, "merge_group", tmp_path / "outputs.json", {**env, "GITHUB_OUTPUT": str(target)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_text() == f"existing=kept\nreused=true\nrun=7\nattempt=1\ngrader={base}\nrecorded=true\n"


def test_an_uncached_commit_is_fetched_before_comparing_its_tree(full_source, tmp_path):
    root, base, head, _, env, _, _ = full_source
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(remote, "config", "user.email", "ci@example.invalid")
    git(remote, "config", "user.name", "CI")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "origin", "dev")
    tree = git(root, "rev-parse", f"{head}^{{tree}}")
    unseen = git(remote, "commit-tree", tree, "-p", head, input="same remote tree\n")
    git(remote, "update-ref", "refs/heads/new", unseen)
    assert subprocess.run(["git", "-C", str(root), "cat-file", "-e", unseen], capture_output=True).returncode != 0
    result = invoke(root, base, unseen, "merge_group", tmp_path / "fetched.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=true" in result.stdout
    assert git(root, "rev-parse", unseen) == unseen


def test_source_artifacts_and_jobs_are_read_across_every_page(full_source, tmp_path):
    root, base, _, queue, env, responses, _ = full_source
    for suffix, key in [("artifacts", "artifacts"), ("attempts/1/jobs", "jobs")]:
        first = f"repos/o/r/actions/runs/7/{suffix}?per_page=100&page=1"
        second = f"repos/o/r/actions/runs/7/{suffix}?per_page=100&page=2"
        body = responses[first]
        values = body[key]
        responses[first] = {key: values[:1], "total_count": len(values)}
        responses[second] = {key: values[1:], "total_count": len(values)}
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "pages.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=true" in result.stdout


@pytest.mark.parametrize("option", ["--base", "--head", "--event", "--repository", "--run", "--attempt", "--record"])
def test_missing_required_command_input_is_rejected(option, reuse_repo, tmp_path):
    from scripts import ci_reuse

    root, base, head, _ = reuse_repo
    pairs = {
        "--base": base,
        "--head": head,
        "--event": "pull_request",
        "--repository": "o/r",
        "--run": "7",
        "--attempt": "1",
        "--record": str(tmp_path / "record.json"),
    }
    args = [part for key, value in pairs.items() if key != option for part in (key, value)]
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        with pytest.raises(SystemExit) as caught:
            ci_reuse.main(args)
    assert caught.value.code == 2


def test_cached_proof_commits_do_not_fetch_from_the_network(full_source, tmp_path):
    root, base, _, queue, env, _, _ = full_source
    real = shutil.which("git")
    trace = tmp_path / "git-commands.jsonl"
    wrapper = Path(env["PATH"].split(":")[0]) / "git"
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"with Path({str(trace)!r}).open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"os.execv({real!r}, [{real!r}, *sys.argv[1:]])\n"
    )
    wrapper.chmod(0o755)
    result = invoke(root, base, queue, "merge_group", tmp_path / "cached.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=true" in result.stdout
    commands = [json.loads(line) for line in trace.read_text().splitlines()]
    assert not any(command[0] == "fetch" for command in commands)
