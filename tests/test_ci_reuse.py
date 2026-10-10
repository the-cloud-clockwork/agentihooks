import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
PROGRAM = Path(__file__).resolve().parents[1] / "scripts/ci_reuse.py"
FETCH = "Fetch the evidence of recent passed runs"
RUNS = "repos/o/r/actions/workflows/test.yml/runs?event=pull_request&status=success&per_page=20"
JOBS = "repos/o/r/actions/runs/7/attempts/1/jobs?per_page=100"
ARTIFACTS = "repos/o/r/actions/runs/7/artifacts?per_page=100"


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


def fetch(env, temp):
    from scripts import ci_reuse

    [step] = [step for step in ci_reuse.reuse_job()["steps"] if step.get("name") == FETCH]
    temp.mkdir()
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(env, GITHUB_REPOSITORY="o/r", RUNNER_TEMP=str(temp)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return temp / "evidence"


def invoke(root, base, head, event, output, env=None):
    from scripts import ci_reuse

    evidence = output.parent / f"{output.stem}-absent"
    if env and "REUSE_FIXTURE" in env:
        evidence = fetch(env, output.parent / f"{output.stem}-runner")
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
        "--evidence",
        str(evidence),
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
    artifacts.append({"id": 52, "name": "split-3.12", "expired": False})
    responses = {
        RUNS: {"workflow_runs": [source]},
        JOBS: {"total_count": len(jobs), "jobs": jobs},
        ARTIFACTS: {"total_count": len(artifacts), "artifacts": artifacts},
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
        "args = [arg for arg in sys.argv[2:] if arg != '--paginate']\n"
        "value = json.loads(Path(os.environ['REUSE_FIXTURE']).read_text())[args[0]]\n"
        "if 'binary' in value: sys.stdout.buffer.write(base64.b64decode(value['binary']))\n"
        "elif args[1:2] == ['--jq']: print(*(json.dumps(item) for item in value[args[2][1:-2]]), sep='\\n')\n"
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
    jobs = responses[JOBS]["jobs"]
    next(job for job in jobs if job["name"] == name)["conclusion"] = outcome
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "failed.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout


@pytest.mark.parametrize(
    "kind",
    [
        "missing-unit",
        "missing-metadata",
        "expired-metadata",
        "expired-coverage",
        "bad-archive",
        "missing-log",
        "unlisted-runs",
        "unlisted-artifacts",
        "missing-archive",
        "unlisted-jobs",
    ],
)
def test_unavailable_source_evidence_runs_fully(full_source, tmp_path, kind):
    root, base, _, queue, env, responses, _ = full_source
    unlisted = {"unlisted-runs": RUNS, "unlisted-artifacts": ARTIFACTS, "unlisted-jobs": JOBS}
    if kind in unlisted:
        responses.pop(unlisted[kind])
    elif kind == "missing-archive":
        responses.pop("repos/o/r/actions/artifacts/42/zip")
    elif kind == "missing-unit":
        body = responses[JOBS]
        body["jobs"] = [job for job in body["jobs"] if job["name"] != "unit (3.12, 1)"]
        body["total_count"] = len(body["jobs"])
    elif kind == "bad-archive":
        responses["repos/o/r/actions/artifacts/42/zip"] = {"binary": base64.b64encode(b"invalid").decode()}
    elif kind == "missing-log":
        responses.pop("repos/o/r/actions/jobs/91/logs")
    else:
        body = responses[ARTIFACTS]
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
    source = responses[RUNS]["workflow_runs"][0]
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
            "--evidence",
            str(tmp_path / "absent"),
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


def test_an_identical_queue_tree_prints_the_source_run_outputs(full_source, tmp_path):
    root, base, _, queue, env, _, _ = full_source
    destination = tmp_path / "github-output"
    destination.write_text("earlier=kept\n")
    result = invoke(root, base, queue, "merge_group", tmp_path / "out.json", dict(env, GITHUB_OUTPUT=str(destination)))
    grader = git(root, "rev-parse", "HEAD")
    outputs = f"reused=true\nrun=7\nattempt=1\ngrader={grader}\nrecorded=true\n"
    assert result.stdout.split("\n", 1)[1] == outputs
    assert destination.read_text() == "earlier=kept\n" + outputs


def test_the_record_names_every_trusted_grading_input(reuse_repo, tmp_path, monkeypatch):
    from scripts import ci_reuse

    class Clock:
        @staticmethod
        def now(zone):
            return datetime(2026, 1, 2, tzinfo=UTC) if zone is UTC else datetime(1999, 1, 1)

    root, base, head, _ = reuse_repo
    monkeypatch.setattr(ci_reuse, "datetime", Clock)
    output = tmp_path / "record.json"
    result = invoke(root, base, head, "pull_request", output)
    grader = git(root, "rev-parse", "HEAD")
    record = {
        "version": 1,
        "run": 7,
        "attempt": 1,
        "event": "pull_request",
        "commit": head,
        "tree": git(root, "rev-parse", f"{head}^{{tree}}"),
        "grader": grader,
        "inputs": {
            "base": git(root, "rev-parse", f"{base}^{{tree}}"),
            "grader": git(root, "rev-parse", f"{grader}^{{tree}}"),
            "workflow": git(root, "rev-parse", "HEAD:.github"),
            "day": "2026-01-02",
        },
        "reused": False,
    }
    assert json.loads(output.read_text()) == record
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert (
        result.stdout
        == f"required-tree-sha256={digest}\nreused=false\nrun=\nattempt=\ngrader={grader}\nrecorded=true\n"
    )


def test_an_unreadable_run_list_names_why_it_runs_fully(full_source, tmp_path):
    root, base, _, queue, env, responses, _ = full_source
    responses.pop(RUNS)
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "unread.json", env)
    assert "No reusable full result: FileNotFoundError\n" in result.stdout
    assert "reused=false" in result.stdout


ARGUMENTS = ["--base", "--head", "--event", "--repository", "--run", "--attempt", "--evidence", "--record"]


@pytest.mark.parametrize("missing", ARGUMENTS)
def test_every_argument_is_required(tmp_path, missing):
    from scripts import ci_reuse

    values = dict(zip(ARGUMENTS, ["b", "h", "pull_request", "o/r", "7", "1", str(tmp_path), str(tmp_path / "r.json")]))
    argv = [part for flag, value in values.items() if flag != missing for part in (flag, value)]
    errors = io.StringIO()
    with pytest.raises(SystemExit) as exit, redirect_stderr(errors):
        ci_reuse.main(argv)
    assert exit.value.code == 2
    assert f"the following arguments are required: {missing}" in errors.getvalue()


@pytest.mark.parametrize("present", [True, False])
def test_a_missing_revision_alone_is_fetched_from_origin_without_tags(monkeypatch, present):
    from scripts import ci_reuse

    calls = []

    def run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0 if present else 1)

    def check_output(command, **options):
        calls.append((command, options))
        return "abc\n"

    monkeypatch.setattr(ci_reuse.subprocess, "run", run)
    monkeypatch.setattr(ci_reuse.subprocess, "check_output", check_output)
    assert ci_reuse._revision("ref") == "abc"
    fetch = [] if present else [(["git", "fetch", "--no-tags", "origin", "ref"], {"text": True})]
    assert calls == [
        (["git", "cat-file", "-e", "ref"], {"capture_output": True}),
        *fetch,
        (["git", "rev-parse", "ref"], {"text": True}),
    ]


def test_matrix_legs_follow_axes_excludes_and_includes():
    from scripts import ci_reuse

    matrix = {
        "python": ["3.11", "3.12"],
        "shard": [1, 2, 3],
        "exclude": [{"python": "3.11", "shard": 3}, {"python": "3.12", "shard": 1}],
        "include": [{"python": "3.13", "shard": 1}, {"python": "3.13", "shard": 2}],
    }
    assert ci_reuse._matrix({"strategy": {"matrix": matrix}}) == 6
    assert ci_reuse._matrix({"strategy": {"matrix": {"shard": "${{ fromJSON(x) }}"}}}) == 1
    assert ci_reuse._matrix({}) == 1


def test_a_pass_reads_display_names_and_only_optional_skips():
    from scripts import ci_reuse

    workflow = {
        "jobs": {
            "gate-required": {"name": "Gate — Required", "needs": ["queue-baseline", "helm-kind", "sonar", "unit"]},
            "queue-baseline": {},
            "helm-kind": {},
            "sonar": {"name": "SonarQube"},
            "unit": {"strategy": {"matrix": {"shard": [1, 2]}}},
        }
    }
    jobs = [
        {"name": "Gate — Required", "conclusion": "success"},
        {"name": "queue-baseline", "conclusion": "skipped"},
        {"name": "helm-kind", "conclusion": "skipped"},
        {"name": "SonarQube", "conclusion": "success"},
        {"name": "unit (1)", "conclusion": "success"},
        {"name": "unit (2)", "conclusion": "success"},
    ]
    assert ci_reuse._passed(workflow, jobs)
    jobs[-1]["conclusion"] = "failure"
    assert not ci_reuse._passed(workflow, jobs)


def test_the_digest_is_canonical_compact_json():
    from scripts import ci_reuse

    assert ci_reuse._digest({"b": 1, "a": [1, 2]}) == hashlib.sha256(b'{"a":[1,2],"b":1}').hexdigest()


def test_only_one_reuse_attestation_authenticates(tmp_path):
    from scripts import ci_reuse

    record = {"tree": "t"}
    (tmp_path / "reuse.log").write_text(f"2026-10-09T11:00:00.000Z required-tree-sha256={ci_reuse._digest(record)}\n")
    attestation = [{"name": "reuse", "conclusion": "success"}]
    assert ci_reuse._authenticated(tmp_path, record, attestation)
    assert not ci_reuse._authenticated(tmp_path, record, attestation * 2)


@pytest.mark.parametrize("size, accepted", [(65536, True), (65537, False)])
def test_source_metadata_is_bounded(tmp_path, size, accepted):
    from scripts import ci_reuse

    body = json.dumps({"a": 1})
    path = tmp_path / "proof.zip"
    with zipfile.ZipFile(path, "w") as zipped:
        zipped.writestr("provenance.json", body + " " * (size - len(body)))
    if accepted:
        assert ci_reuse._proof(path) == {"a": 1}
    else:
        with pytest.raises(ValueError) as error:
            ci_reuse._proof(path)
        assert str(error.value) == "The source metadata is too large"


def test_a_queue_tree_that_changes_workflows_runs_fully(full_source, tmp_path):
    root, base, head, _, env, responses, _ = full_source
    (root / ".github/extra.yml").write_text("planted: true\n")
    git(root, "add", ".github/extra.yml")
    planted = git(root, "commit-tree", git(root, "write-tree"), "-p", head, input="plant\n")
    source = tmp_path / "planted-source.json"
    assert invoke(root, base, planted, "pull_request", source).returncode == 0
    publish_record(env, responses, json.loads(source.read_text()))
    result = invoke(root, base, planted, "merge_group", tmp_path / "planted.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout


DECOYS = ["valid", "event", "evidence", "workflow", "metadata", "record", "tree", "coverage", "jobs"]


@pytest.mark.parametrize("decoy", DECOYS)
def test_a_rejected_newer_run_still_lets_an_older_full_pass_reuse(full_source, tmp_path, decoy):
    root, base, _, queue, env, responses, record = full_source
    decoy_source = dict(responses[RUNS]["workflow_runs"][0], id=8)
    artifacts = [dict(item, id=item["id"] + 100) for item in responses[ARTIFACTS]["artifacts"]]
    jobs = [dict(job, id=job["id"] + 100) for job in responses[JOBS]["jobs"]]
    decoy_record = dict(record, run=8)
    if decoy == "event":
        decoy_source["event"] = "push"
    elif decoy == "workflow":
        (root / ".github/other.yml").write_text("other: true\n")
        git(root, "add", ".github/other.yml")
        decoy_source["head_sha"] = git(root, "commit-tree", git(root, "write-tree"), "-p", base, input="other\n")
    elif decoy == "metadata":
        artifacts.append(dict(artifacts[0], id=150))
    elif decoy == "record":
        decoy_record["run"] = 9
    elif decoy == "tree":
        (root / "code.txt").write_text("planted\n")
        git(root, "add", "code.txt")
        decoy_record["commit"] = git(root, "commit-tree", git(root, "write-tree"), "-p", base, input="tree\n")
    elif decoy == "coverage":
        artifacts = [item for item in artifacts if not item["name"].startswith("coverage-")]
    elif decoy == "jobs":
        next(job for job in jobs if job["name"] == "lint")["conclusion"] = "failure"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("provenance.json", json.dumps(decoy_record))
    digest = hashlib.sha256(json.dumps(decoy_record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    log = f"2026-10-09T11:00:00.000Z required-tree-sha256={digest}\n"
    responses[RUNS]["workflow_runs"].insert(0, decoy_source)
    responses["repos/o/r/actions/runs/8/artifacts?per_page=100"] = {"artifacts": artifacts}
    responses["repos/o/r/actions/runs/8/attempts/1/jobs?per_page=100"] = {"jobs": jobs}
    responses["repos/o/r/actions/artifacts/142/zip"] = {"binary": base64.b64encode(archive.getvalue()).decode()}
    if decoy != "evidence":
        responses["repos/o/r/actions/jobs/191/logs"] = {"binary": base64.b64encode(log.encode()).decode()}
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "decoy.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=true" in result.stdout
    assert f"run={8 if decoy == 'valid' else 7}\n" in result.stdout
