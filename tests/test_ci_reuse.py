import base64
import io
import json
import os
import subprocess
import sys
import zipfile
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
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(PROGRAM),
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
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )


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
