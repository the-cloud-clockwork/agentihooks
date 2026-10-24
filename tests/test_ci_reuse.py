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
QUEUE_REF = "refs/heads/gh-readonly-queue/dev/pr-5-0123abcd"
RUNS = "repos/o/r/actions/workflows/test.yml/runs?event=pull_request&status=success&head_sha={}&per_page=100"
PULL_REF = "refs/pull/5/head"
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
            "gate-required": {"name": "Gate — Required", "needs": ["reuse", "unit", "lint"]},
        }
    }
    if getattr(request, "param", None) == "dynamic":
        workflow["jobs"]["mutation"] = {"strategy": {"matrix": {"shard": "${{ fromJSON(needs.plan.outputs.shards) }}"}}}
        workflow["jobs"]["gate-required"]["needs"].append("mutation")
    if getattr(request, "param", None) == "selected":
        workflow["jobs"]["unit-selected"] = {
            "strategy": {"matrix": {"leg": "${{ fromJSON(needs.select.outputs.legs) }}"}}
        }
        workflow["jobs"]["gate-required"]["needs"].append("unit-selected")
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
        env=dict(env, GITHUB_REPOSITORY="o/r", RUNNER_TEMP=str(temp), QUEUE_REF=QUEUE_REF),
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
        "pull_requests": [{"number": 5}],
    }
    names = ["reuse", "unit (3.11, 1)", "unit (3.12, 1)", "lint", "Gate — Required"]
    jobs = [{"id": 91 + n, "name": name, "conclusion": "success"} for n, name in enumerate(names)]
    if "mutation" in yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]:
        jobs.extend([{"id": 98 + n, "name": f"mutation ({n})", "conclusion": "success"} for n in range(2)])
    if "unit-selected" in yaml.safe_load((root / ".github/workflows/test.yml").read_text())["jobs"]:
        jobs.append({"id": 96, "name": "unit-selected", "conclusion": "skipped"})
    artifacts = [{"id": 42, "name": "required-tree-1", "expired": False}]
    artifacts.append({"id": 51, "name": "coverage-3.12-1", "expired": False})
    artifacts.append({"id": 52, "name": "split-3.12", "expired": False})
    responses = {
        RUNS.format(head): {"workflow_runs": [source]},
        JOBS: {"total_count": len(jobs), "jobs": jobs},
        ARTIFACTS: {"total_count": len(artifacts), "artifacts": artifacts},
        "repos/o/r/actions/artifacts/42/zip": {"binary": base64.b64encode(archive.getvalue()).decode()},
        "repos/o/r/actions/jobs/91/logs": {
            "binary": base64.b64encode(
                (
                    "2026-10-09T11:00:00.000Z \x1b[36;1mpython -I scripts/ci_reuse.py\x1b[0m\n"
                    + "".join(f"2026-10-09T11:00:00.000Z {line}\n" for line in result.stdout.splitlines())
                ).encode()
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
        "allow = '--allow-escape-sequences' in sys.argv\n"
        "args = [arg for arg in sys.argv[2:] if arg not in ('--paginate', '--allow-escape-sequences')]\n"
        "from urllib.parse import parse_qs, urlsplit\n"
        "query = parse_qs(urlsplit(args[0]).query)\n"
        "paged = '/attempts/' in args[0] and 'page' in query\n"
        "endpoint = args[0].split('?')[0] + '?per_page=100' if paged else args[0]\n"
        "value = json.loads(Path(os.environ['REUSE_FIXTURE']).read_text())[endpoint]\n"
        "if paged:\n"
        "    size, page = int(query['per_page'][0]), int(query['page'][0])\n"
        "    start = (page - 1) * size\n"
        "    value['jobs'] = value['jobs'][start:start + size]\n"
        "data = base64.b64decode(value.get('binary', ''))\n"
        "if args[0].endswith('/logs') and b'\\x1b' in data and not allow:\n"
        "    sys.exit('the response contains terminal escape sequences; '\n"
        "             'pass --allow-escape-sequences to output it anyway')\n"
        "if 'binary' in value: sys.stdout.buffer.write(data)\n"
        "elif args[1:2] == ['--jq']: print(*(json.dumps(item) for item in value[args[2][1:-2]]), sep='\\n')\n"
        "else: print(json.dumps(value))\n"
    )
    (bin_dir / "gh").chmod(0o755)
    git(root, "remote", "add", "origin", str(root))
    git(root, "update-ref", PULL_REF, head)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", REUSE_FIXTURE=str(fixture))
    env["GIT_DIR"] = str(root / ".git")
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


def test_an_identical_queue_tree_reuses_a_full_passed_run_with_the_paged_reader(full_source, tmp_path, monkeypatch):
    from scripts import ci_reuse

    job = ci_reuse.paged_reuse_job()
    monkeypatch.setattr(ci_reuse, "reuse_job", lambda: job)
    test_an_identical_queue_tree_reuses_a_full_passed_run(full_source, tmp_path)


def test_a_passed_run_behind_a_hundred_newer_runs_still_reuses(full_source, tmp_path):
    root, base, head, queue, env, responses, _ = full_source
    source = responses[RUNS.format(head)]["workflow_runs"][0]
    newest = [dict(source, id=1000 + n, head_sha=ABSENT) for n in range(100)]
    responses[RUNS.replace("head_sha={}&", "")] = {"workflow_runs": newest}
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "older.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=true" in result.stdout
    assert "run=7\n" in result.stdout


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
    assert "Refused run 7: its reuse log does not attest the record\n" in result.stdout


def test_the_required_workflow_uses_the_protected_canonical_aggregation():
    from scripts import ci_reuse, gate_protected

    root = PROGRAM.parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/test.yml").read_text())
    policy = json.loads((root / ".github/workflows/trusted-actors.json").read_text())
    steps = workflow["jobs"]["gate-required"]["steps"]
    steps[0]["run"] = steps[0]["run"].replace('"lint", "docs", "semgrep"', '"lint", "semgrep"')
    forms = (
        ({"selected": True}, {}, {"queue_tier": True}, {"queue_tier": True, "promotion": True})
        if "durations" in workflow["jobs"]
        else ({"queue_tier": True, "consolidated": True}, {"queue_tier": True, "promotion": True, "consolidated": True})
    )
    assert steps in [
        diagnostic(
            finish(
                rerun(
                    ci_reuse.inline_mutation(
                        ci_reuse.postgres_required(ci_reuse.gate_steps(**form, summary=summary, watcher_loss=watcher_loss))
                    )
                )
            )
        )
        for diagnostic in (list, ci_reuse.selection_diagnostic)
        for form in forms
        for summary in (False, True)
        for watcher_loss in (False, True)
        for rerun in (list, ci_reuse.lost_rerun)
        for finish in (list, ci_reuse.pipefail_summary)
    ]

    assert workflow["jobs"]["gate-required"]["runs-on"] in gate_protected.runners(policy)
    assert workflow["jobs"]["reuse"] in gate_protected.reuse_jobs(policy)


@pytest.mark.parametrize("reuse_repo", ["dynamic"], indirect=True)
def test_a_full_pass_with_dynamic_mutation_shards_reuses(full_source, tmp_path):
    root, base, _, queue, env, _, _ = full_source
    result = invoke(root, base, queue, "merge_group", tmp_path / "dynamic.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=true" in result.stdout


@pytest.mark.parametrize("reuse_repo", ["selected"], indirect=True)
def test_a_full_pull_request_pass_whose_selected_tests_were_not_due_reuses(full_source, tmp_path):
    root, base, _, queue, env, _, _ = full_source
    result = invoke(root, base, queue, "merge_group", tmp_path / "selected.json", env)
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
    assert f"Refused run 7: {field} differed\n" in result.stdout


@pytest.mark.parametrize("field", ["base", "grader", "workflow", "day"])
def test_any_changed_trusted_grading_input_runs_fully(full_source, tmp_path, field):
    root, base, _, queue, env, responses, record = full_source
    record["inputs"][field] = "different"
    publish_record(env, responses, record)
    result = invoke(root, base, queue, "merge_group", tmp_path / "inputs.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout
    assert f"Refused run 7: inputs.{field} differed\n" in result.stdout


def test_a_head_with_no_passed_run_says_so(full_source, tmp_path):
    root, base, head, queue, env, responses, _ = full_source
    responses[RUNS.format(head)]["workflow_runs"] = []
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "none.json", env)
    assert "No passed pull request run for this head\n" in result.stdout
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
        "unlisted-pull",
    ],
)
def test_unavailable_source_evidence_runs_fully(full_source, tmp_path, kind):
    root, base, head, queue, env, responses, _ = full_source
    unlisted = {"unlisted-runs": RUNS.format(head), "unlisted-artifacts": ARTIFACTS, "unlisted-jobs": JOBS}
    if kind == "unlisted-pull":
        git(root, "update-ref", "-d", PULL_REF)
    elif kind in unlisted:
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
    unfetched = "Refused run 7: no proof.zip, jobs.jsonl, reuse.log evidence\n"
    reasons = {
        "missing-unit": "Refused run 7: a required job did not pass\n",
        "missing-metadata": unfetched,
        "expired-metadata": unfetched,
        "expired-coverage": "Refused run 7: no coverage-3.12-1 artifact\n",
        "bad-archive": "Refused run 7: BadZipFile\n",
        "missing-log": "Refused run 7: no reuse.log evidence\n",
        "unlisted-runs": "No reusable full result: FileNotFoundError\n",
        "unlisted-artifacts": "Refused run 7: no artifacts.jsonl, proof.zip, jobs.jsonl, reuse.log evidence\n",
        "missing-archive": unfetched,
        "unlisted-jobs": "Refused run 7: no jobs.jsonl, reuse.log evidence\n",
        "unlisted-pull": "No reusable full result: FileNotFoundError\n",
    }
    assert reasons[kind] in result.stdout


@pytest.mark.parametrize("field, value", [("event", "push"), ("status", "in_progress"), ("conclusion", "failure")])
def test_a_source_run_must_be_a_completed_successful_required_run(full_source, tmp_path, field, value):
    root, base, head, queue, env, responses, _ = full_source
    source = responses[RUNS.format(head)]["workflow_runs"][0]
    source[field] = value
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "run.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=false" in result.stdout
    assert f"Refused run 7: {field} is {value}\n" in result.stdout


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


def test_a_pull_request_record_names_the_merge_base_with_current_dev_not_the_frozen_event_base(reuse_repo, tmp_path):
    root, base, head, _ = reuse_repo
    git(root, "switch", "-q", "-c", "newer", base)
    (root / "dev.txt").write_text("newer dev\n")
    git(root, "add", "dev.txt")
    git(root, "commit", "-m", "newer dev")
    newer = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-q", "--detach", head)
    git(root, "merge", "-q", "--no-edit", "newer")
    merged = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-q", "--detach", newer)
    output = tmp_path / "record.json"
    invoke(root, base, merged, "pull_request", output)
    assert json.loads(output.read_text())["inputs"]["base"] == git(root, "rev-parse", f"{newer}^{{tree}}")


def test_an_unreadable_run_list_names_why_it_runs_fully(full_source, tmp_path):
    root, base, head, queue, env, responses, _ = full_source
    responses.pop(RUNS.format(head))
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
            "gate-required": {"name": "Gate — Required", "needs": ["unit-selected", "helm-kind", "sonar", "unit"]},
            "unit-selected": {},
            "helm-kind": {},
            "sonar": {"name": "SonarQube"},
            "unit": {"strategy": {"matrix": {"shard": [1, 2]}}},
        }
    }
    jobs = [
        {"name": "Gate — Required", "conclusion": "success"},
        {"name": "unit-selected", "conclusion": "skipped"},
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
    assert "No reuse: the queue tree changes workflow files\n" in result.stdout


DECOYS = [
    "valid",
    "event",
    "evidence",
    "head",
    "workflow",
    "metadata",
    "record",
    "commit",
    "tree",
    "coverage",
    "jobs",
    "archive",
]
ABSENT = "0123456789abcdef0123456789abcdef01234567"
DECOY_REASONS = {
    "event": "event is push",
    "evidence": "no reuse.log evidence",
    "head": "head commit unavailable",
    "workflow": "workflow files differ at its head",
    "metadata": "not exactly one required-tree-1 artifact",
    "record": "run differed",
    "commit": "commit.tree differed",
    "tree": "commit.tree differed",
    "coverage": "no coverage-3.12-1 artifact",
    "jobs": "a required job did not pass",
    "archive": "BadZipFile",
}


@pytest.mark.parametrize("decoy", DECOYS)
def test_a_rejected_newer_run_still_lets_an_older_full_pass_reuse(full_source, tmp_path, decoy):
    root, base, head, queue, env, responses, record = full_source
    decoy_source = dict(responses[RUNS.format(head)]["workflow_runs"][0], id=8)
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
    elif decoy == "head":
        decoy_source["head_sha"] = ABSENT
    elif decoy == "commit":
        decoy_record["commit"] = ABSENT
    elif decoy == "jobs":
        next(job for job in jobs if job["name"] == "lint")["conclusion"] = "failure"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("provenance.json", json.dumps(decoy_record))
    digest = hashlib.sha256(json.dumps(decoy_record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    log = f"2026-10-09T11:00:00.000Z required-tree-sha256={digest}\n"
    responses[RUNS.format(head)]["workflow_runs"].insert(0, decoy_source)
    responses["repos/o/r/actions/runs/8/artifacts?per_page=100"] = {"artifacts": artifacts}
    responses["repos/o/r/actions/runs/8/attempts/1/jobs?per_page=100"] = {"jobs": jobs}
    responses["repos/o/r/actions/artifacts/142/zip"] = {"binary": base64.b64encode(archive.getvalue()).decode()}
    if decoy == "archive":
        responses["repos/o/r/actions/artifacts/142/zip"] = {"binary": base64.b64encode(b"invalid").decode()}
    if decoy != "evidence":
        responses["repos/o/r/actions/jobs/191/logs"] = {"binary": base64.b64encode(log.encode()).decode()}
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    result = invoke(root, base, queue, "merge_group", tmp_path / "decoy.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reused=true" in result.stdout
    assert f"run={8 if decoy == 'valid' else 7}\n" in result.stdout
    assert decoy == "valid" or f"Refused run 8: {DECOY_REASONS[decoy]}\n" in result.stdout


def test_every_differing_key_is_named_together(full_source, tmp_path):
    root, base, _, queue, env, responses, record = full_source
    record["run"] = 8
    record["inputs"]["base"] = "different"
    publish_record(env, responses, record)
    result = invoke(root, base, queue, "merge_group", tmp_path / "keys.json", env)
    assert "Refused run 7: inputs.base, run differed\n" in result.stdout


def test_a_proof_that_is_not_an_object_refuses_only_its_run(full_source, tmp_path):
    root, base, _, queue, env, responses, _ = full_source
    publish_record(env, responses, [1])
    result = invoke(root, base, queue, "merge_group", tmp_path / "list.json", env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Refused run 7: ValueError\n" in result.stdout
    assert "reused=false" in result.stdout


def test_source_metadata_must_be_an_object(tmp_path):
    from scripts import ci_reuse

    path = tmp_path / "proof.zip"
    with zipfile.ZipFile(path, "w") as zipped:
        zipped.writestr("provenance.json", "[1]")
    with pytest.raises(ValueError) as error:
        ci_reuse._proof(path)
    assert str(error.value) == "The source metadata is not an object"


def test_every_missing_coverage_shard_is_named_together(full_source, tmp_path):
    root, base, _, queue, env, _, _ = full_source
    path = root / ".github/workflows/test.yml"
    workflow = yaml.safe_load(path.read_text())
    workflow["jobs"]["unit"]["strategy"]["matrix"]["shard"] = [1, 2, 3]
    path.write_text(yaml.safe_dump(workflow))
    result = invoke(root, base, queue, "merge_group", tmp_path / "shards.json", env)
    assert "Refused run 7: no coverage-3.12-2, coverage-3.12-3 artifact\n" in result.stdout


def test_differing_keys_cover_both_sides_and_nest_only_dicts():
    from scripts import ci_reuse

    expected = {"a": 1, "b": {"c": 1}, "d": 2, "f": {"g": 1}, "h": False}
    actual = {"a": 1, "b": {"c": 2}, "e": 3, "f": None, "h": 0}
    assert ci_reuse._differs(expected, actual) == ["b.c", "d", "e", "f", "h"]


@pytest.mark.parametrize("pulls", [[{"number": 6}], [], None])
def test_only_the_queued_pull_requests_runs_are_fetched(full_source, tmp_path, pulls):
    root, base, head, queue, env, responses, _ = full_source
    source = responses[RUNS.format(head)]["workflow_runs"][0]
    if pulls is None:
        del source["pull_requests"]
    else:
        source["pull_requests"] = pulls
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    evidence = fetch(env, tmp_path / "runner")
    assert not (evidence / "7").exists()
    result = invoke(root, base, queue, "merge_group", tmp_path / "other.json", env)
    assert "reused=false" in result.stdout


def test_a_queue_ref_naming_no_pull_request_fetches_nothing(full_source, tmp_path):
    from scripts import ci_reuse

    _, _, _, _, env, _, _ = full_source
    [step] = [step for step in ci_reuse.reuse_job()["steps"] if step.get("name") == FETCH]
    temp = tmp_path / "runner"
    temp.mkdir()
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(env, GITHUB_REPOSITORY="o/r", RUNNER_TEMP=str(temp), QUEUE_REF="refs/heads/dev"),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(temp.iterdir()) == []


@pytest.mark.parametrize("remote", ["origin", "absent"])
def test_an_unreadable_pull_request_head_lists_no_runs(full_source, tmp_path, remote):
    from scripts import ci_reuse

    root, _, _, _, env, responses, _ = full_source
    if remote == "origin":
        git(root, "update-ref", "-d", PULL_REF)
    else:
        git(root, "remote", "remove", "origin")
    responses[RUNS.format("")] = {"workflow_runs": []}
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    [step] = [step for step in ci_reuse.reuse_job()["steps"] if step.get("name") == FETCH]
    temp = tmp_path / "runner"
    temp.mkdir()
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(env, GITHUB_REPOSITORY="o/r", RUNNER_TEMP=str(temp), QUEUE_REF=QUEUE_REF),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "No reuse: cannot read the head of pull request 5\n"
    assert not (temp / "evidence/runs.json").exists()


def test_an_unreadable_run_list_says_which_head(full_source, tmp_path):
    from scripts import ci_reuse

    _, _, head, _, env, responses, _ = full_source
    responses.pop(RUNS.format(head))
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    [step] = [step for step in ci_reuse.reuse_job()["steps"] if step.get("name") == FETCH]
    temp = tmp_path / "runner"
    temp.mkdir()
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(env, GITHUB_REPOSITORY="o/r", RUNNER_TEMP=str(temp), QUEUE_REF=QUEUE_REF),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == f"No reuse: cannot list the passed runs of head {head}\n"
    assert not (temp / "evidence/runs.json").exists()


@pytest.mark.parametrize(
    ("failures", "status", "calls", "kept"),
    [
        (0, 502, 11, True),
        (1, 500, 27, True),
        (2, 503, 53, True),
        (4, 504, 4, False),
        (1, 400, 1, False),
        (1, 403, 1, False),
        (1, 429, 1, False),
    ],
)
def test_attempt_jobs_use_five_job_pages_and_bounded_server_retries(
    full_source, tmp_path, failures, status, calls, kept
):
    from scripts import ci_reuse

    _, _, _, _, env, responses, _ = full_source
    jobs = responses[JOBS]["jobs"]
    jobs.extend({"id": 200 + n, "name": f"extra {n}", "conclusion": "success"} for n in range(45))
    Path(env["REUSE_FIXTURE"]).write_text(json.dumps(responses))
    bin_dir = Path(env["PATH"].split(":")[0])
    (bin_dir / "gh").rename(bin_dir / "original-gh")
    requests = tmp_path / "requests.jsonl"
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "from urllib.parse import parse_qs, urlsplit\n"
        "endpoint = next(arg for arg in sys.argv[2:] if arg.startswith('repos/'))\n"
        "if '/attempts/' not in endpoint:\n"
        "    sys.exit(subprocess.call([str(Path(__file__).with_name('original-gh')), *sys.argv[1:]]))\n"
        "query = parse_qs(urlsplit(endpoint).query)\n"
        "size, page = int(query['per_page'][0]), int(query['page'][0])\n"
        "requests = Path(os.environ['REQUESTS'])\n"
        "calls = len(requests.read_text().splitlines()) if requests.exists() else 0\n"
        "with requests.open('a') as stream: stream.write(json.dumps([size, page]) + '\\n')\n"
        "if size > 5: sys.exit('expected at most five jobs per page')\n"
        "jobs = json.loads(Path(os.environ['REUSE_FIXTURE']).read_text())[endpoint.split('?')[0] + '?per_page=100']['jobs']\n"
        "start = (page - 1) * size\n"
        "rows = jobs[start:start + size]\n"
        "print(json.dumps({'total_count': len(jobs), 'jobs': rows}), flush=True)\n"
        "if calls < int(os.environ['FAILURES']):\n"
        "    print(f'gh: server error (HTTP {os.environ[\"STATUS\"]})', file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    (bin_dir / "gh").chmod(0o755)
    runner = tmp_path / "runner"
    runner.mkdir()
    [step] = [step for step in ci_reuse.paged_reuse_job()["steps"] if step.get("name") == FETCH]
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(
            env,
            GITHUB_REPOSITORY="o/r",
            RUNNER_TEMP=str(runner),
            QUEUE_REF=QUEUE_REF,
            REQUESTS=str(requests),
            FAILURES=str(failures),
            STATUS=str(status),
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    attempts = [json.loads(line) for line in requests.read_text().splitlines()]
    assert len(attempts) == calls
    assert attempts[0] == [5, 1]
    evidence = runner / "evidence" / "7"
    if kept:
        assert [json.loads(line) for line in (evidence / "jobs.jsonl").read_text().splitlines()] == jobs
        assert (evidence / "reuse.log").is_file()
    else:
        assert not (evidence / "jobs.jsonl").exists()
        assert not (evidence / "reuse.log").exists()


def _queue_needs(baseline="success", ratchet="success", ratchet_job="skipped", queue_job="success"):
    needs = {name: {"result": "success"} for name in ("unit", "lint", "sonar", "stage-budget")}
    needs["reuse"] = {"result": "success", "outputs": {"reused": "false"}}
    needs["coverage-ratchet"] = {"result": ratchet_job}
    needs["queue-baseline"] = {"result": queue_job, "outputs": {"baseline": baseline, "ratchet": ratchet}}
    return needs


def _gate(steps, needs, event="merge_group"):
    [step] = steps
    env = dict(os.environ, NEEDS=json.dumps(needs), MUTATION="false", KIND="false", EVENT=event, REUSED="false")
    return subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)


@pytest.mark.parametrize("watcher", ["success", "failure", "cancelled", "skipped", "pending", None])
@pytest.mark.parametrize("failed", ["", "unit (3.11, 1)", "queue group is no longer current"])
def test_queue_watcher_loss_does_not_drop_passed_graded_jobs(watcher, failed):
    from scripts import ci_reuse

    needs = {name: {"result": "success"} for name in ("select", "split", "unit", "shard-check", "test-count")}
    if watcher is not None:
        needs["queue-fail-fast"] = {"result": watcher, "outputs": {"failed": failed}}
    result = _gate(ci_reuse.gate_steps(queue_tier=True, consolidated=True, watcher_loss=True), needs)
    assert (result.returncode == 0) == (watcher in {"success", "failure", "cancelled"} and not failed), (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize("job", ["select", "split", "unit", "shard-check", "test-count"])
@pytest.mark.parametrize("status", ["failure", "cancelled", "skipped", "pending", None])
def test_queue_watcher_loss_never_masks_a_graded_failure(job, status):
    from scripts import ci_reuse

    needs = {name: {"result": "success"} for name in ("select", "split", "unit", "shard-check", "test-count")}
    needs["queue-fail-fast"] = {"result": "failure"}
    if status is None:
        del needs[job]
    else:
        needs[job]["result"] = status
    result = _gate(ci_reuse.gate_steps(queue_tier=True, consolidated=True, watcher_loss=True), needs)
    assert result.returncode != 0, result.stdout + result.stderr


_LOST_RERUN_FORM = {"queue_tier": True, "promotion": True, "consolidated": True, "watcher_loss": True}


@pytest.mark.parametrize(
    ("unit", "lost", "rerun", "passes"),
    [
        ("success", "skipped", "skipped", True),
        ("failure", "success", "success", True),
        ("failure", "success", "failure", False),
        ("failure", "success", "skipped", False),
        ("failure", "failure", "skipped", False),
        ("failure", "skipped", "skipped", False),
        ("success", "success", "success", False),
        ("cancelled", "success", "success", True),
        ("cancelled", "success", "failure", False),
        ("cancelled", "skipped", "skipped", False),
    ],
)
def test_a_lost_unit_shard_passes_the_queue_only_on_its_green_rerun(unit, lost, rerun, passes):
    from scripts import ci_reuse

    needs = {name: {"result": "success"} for name in ("select", "split", "shard-check", "test-count")}
    needs["queue-fail-fast"] = {"result": "success", "outputs": {"failed": ""}}
    needs |= {"unit": {"result": unit}, "unit-lost": {"result": lost}, "unit-rerun": {"result": rerun}}
    result = _gate(ci_reuse.lost_rerun(ci_reuse.inline_mutation(ci_reuse.gate_steps(**_LOST_RERUN_FORM))), needs)
    assert (result.returncode == 0) == passes, result.stdout + result.stderr


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "pull_request"])
@pytest.mark.parametrize("rerun", ["skipped", "success", "failure"])
def test_outside_the_queue_the_rerun_jobs_never_pass_a_failure(event, rerun):
    from scripts import ci_reuse

    needs = {name: {"result": "success"} for name in ("select", "split", "unit", "shard-check", "test-count")}
    needs["select"]["outputs"] = {"scope": "full"}
    for name in ("lint", "semgrep", "sonar"):
        needs[name] = {"result": "success"}
    needs["reuse"] = {"result": "skipped" if event == "pull_request" else "success", "outputs": {"reused": "false"}}
    needs |= {"unit-lost": {"result": rerun}, "unit-rerun": {"result": rerun}}
    result = _gate(
        ci_reuse.lost_rerun(ci_reuse.inline_mutation(ci_reuse.gate_steps(**_LOST_RERUN_FORM))), needs, event=event
    )
    passes = rerun == "skipped" or (rerun == "success" and event != "pull_request")
    assert (result.returncode == 0) == passes, result.stdout + result.stderr


def _mutation_gate(form, event, inline, stats, mutation):
    from scripts import ci_reuse

    needs = {name: {"result": "success"} for name in ("test-count", "lint", "semgrep", "sonar")}
    needs["select"] = {"result": "success", "outputs": {"scope": "none"}}
    if form.get("queue_tier") and not form.get("consolidated"):
        needs["durations"] = {"result": "success"}
    needs["mutation-plan"] = {"result": "success"}
    needs["mutation-stats"] = {"result": stats}
    needs["mutation"] = {"result": mutation}
    [step] = ci_reuse.inline_mutation(ci_reuse.gate_steps(**form))
    env = dict(
        os.environ,
        NEEDS=json.dumps(needs),
        MUTATION="true",
        KIND="false",
        EVENT=event,
        REUSED="false",
        INLINE=inline or "",
    )
    return subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)


@pytest.mark.parametrize(
    "form",
    [{}, {"selected": True}, {"queue_tier": True}, {"promotion": True}, {"queue_tier": True, "consolidated": True}],
)
@pytest.mark.parametrize("event", ["pull_request", "workflow_dispatch"])
@pytest.mark.parametrize(
    ("inline", "stats", "mutation", "passes"),
    [
        ("true", "success", "skipped", True),
        ("false", "success", "success", True),
        ("false", "success", "skipped", False),
        (None, "success", "skipped", False),
        ("true", "failure", "skipped", False),
        ("true", "skipped", "skipped", False),
        ("true", "success", "failure", False),
        ("true", "success", "cancelled", False),
    ],
)
def test_the_inline_mutation_form_skips_the_shard_job_only_for_an_inline_plan(
    form, event, inline, stats, mutation, passes
):
    result = _mutation_gate(form, event, inline, stats, mutation)
    assert (result.returncode == 0) == passes, result.stdout + result.stderr


def test_the_inline_mutation_form_derives_inline_from_a_one_part_one_shard_plan():
    from scripts import ci_reuse

    [step] = ci_reuse.inline_mutation(ci_reuse.gate_steps(queue_tier=True, consolidated=True))
    assert step["env"]["INLINE"] == (
        "${{ needs.mutation-plan.outputs.shards == '[0]' && needs.mutation-plan.outputs.stats_parts == '[0]' }}"
    )
    [plain] = ci_reuse.gate_steps(queue_tier=True, consolidated=True)
    assert "INLINE" not in plain["env"]
    assert "$inline" not in plain["run"]


def test_the_watcher_loss_rule_is_written_once_for_the_watcher_job():
    from scripts import ci_queue_fail_fast, ci_reuse

    assert ci_reuse.WATCHER == ci_queue_fail_fast.SELF
    assert ci_reuse.WATCHER in ci_reuse.WATCHER_LOSS
    [step] = ci_reuse.gate_steps(queue_tier=True, consolidated=True, watcher_loss=True)
    assert step["run"].count(ci_reuse.WATCHER_LOSS) == 1
    [plain] = ci_reuse.gate_steps(queue_tier=True, consolidated=True)
    assert ci_reuse.WATCHER_LOSS not in plain["run"]


@pytest.mark.parametrize("watcher", ["success", "failure", "cancelled", None])
@pytest.mark.parametrize("failed", ["", "unit (3.11, 1)", "queue group is no longer current"])
@pytest.mark.parametrize("graded", ["success", "failure", "cancelled", "skipped"])
def test_the_workflow_gate_distinguishes_watcher_loss_from_reported_failures(watcher, failed, graded):
    from scripts.ci_queue_fail_fast import SELF

    jobs = yaml.safe_load((PROGRAM.parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    assert SELF in jobs
    assert SELF in jobs["gate-required"]["needs"]
    required = {"select", "split", "unit", "shard-check", "test-count"}
    needs = {name: {"result": "success" if name in required else "skipped"} for name in jobs["gate-required"]["needs"]}
    if watcher is None:
        del needs[SELF]
    else:
        needs[SELF] = {"result": watcher, "outputs": {"failed": failed}}
    needs["unit"]["result"] = graded
    step = next(step for step in jobs["gate-required"]["steps"] if step.get("name") == "Require every gate to succeed")
    result = _gate([step], needs)
    assert (result.returncode == 0) == (watcher is not None and not failed and graded == "success"), (
        result.stdout + result.stderr
    )


def test_the_workflow_watcher_exports_reported_failures_to_the_gate():
    jobs = yaml.safe_load((PROGRAM.parents[1] / ".github/workflows/test.yml").read_text())["jobs"]
    assert jobs["queue-fail-fast"]["outputs"]["failed"] == "${{ steps.watch.outputs.failed }}"


def test_queue_gate_keeps_legacy_duration_admission_by_default():
    from scripts import ci_reuse

    implicit = ci_reuse.gate_steps(queue_tier=True)
    assert implicit == ci_reuse.gate_steps(queue_tier=True, consolidated=False)
    assert implicit != ci_reuse.gate_steps(queue_tier=True, consolidated=True)


@pytest.mark.parametrize("event", ["merge_group", "pull_request"])
@pytest.mark.parametrize("promotion", [False, True])
@pytest.mark.parametrize("admission", ["success", "failure", "skipped", "cancelled", None])
def test_consolidated_gate_requires_the_shared_admission(event, promotion, admission):
    from scripts import ci_reuse

    required = (
        {"select", "split", "unit", "shard-check", "test-count", "queue-fail-fast"}
        if event == "merge_group"
        else {"select", "test-count", "lint", "semgrep", "sonar"}
    )
    needs = {name: {"result": "success"} for name in required}
    needs["select"]["outputs"] = {"scope": "none"}
    if admission is None:
        del needs["select"]
    else:
        needs["select"]["result"] = admission
    result = _gate(ci_reuse.gate_steps(queue_tier=True, promotion=promotion, consolidated=True), needs, event)
    assert (result.returncode == 0) == (admission == "success"), result.stdout + result.stderr


@pytest.mark.parametrize("status", ["success", "failure", "skipped", "cancelled", None])
def test_documentation_gate_requires_a_successful_build_on_pull_requests(status):
    from scripts import ci_reuse

    required = {"select", "test-count", "lint", "semgrep", "sonar"}
    needs = {name: {"result": "success"} for name in required}
    needs["select"]["outputs"] = {"scope": "none"}
    if status is not None:
        needs["docs"] = {"result": status}
    result = _gate(
        ci_reuse.gate_steps(queue_tier=True, consolidated=True, docs=True),
        needs,
        event="pull_request",
    )
    assert (result.returncode == 0) == (status == "success"), result.stdout + result.stderr


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("status", ["success", "failure", "skipped", "cancelled", None])
def test_postgres_gate_requires_the_writer_lock_scenario_only_on_pull_requests(event, status):
    from scripts import ci_reuse

    required = (
        {"select", "test-count", "lint", "docs", "semgrep", "sonar"}
        if event == "pull_request"
        else {"select", "split", "unit", "shard-check", "test-count", "queue-fail-fast"}
    )
    needs = {name: {"result": "success"} for name in required}
    needs["select"]["outputs"] = {"scope": "none"}
    if status is not None:
        needs["writer-lock"] = {"result": status}
    canonical = ci_reuse.gate_steps(queue_tier=True, consolidated=True, docs=True, summary=True)
    steps = ci_reuse.postgres_required(canonical)
    assert steps[0]["run"] == canonical[0]["run"].replace('"sonar"]', '"sonar", "writer-lock"]')
    assert steps[0]["run"].count('"writer-lock"') == 1
    assert steps[1:] == canonical[1:] and len(steps) > 1
    assert {**steps[0], "run": canonical[0]["run"]} == canonical[0]
    result = _gate(
        ci_reuse.postgres_required(ci_reuse.gate_steps(queue_tier=True, consolidated=True, docs=True)),
        needs,
        event=event,
    )
    green = status == "success" if event == "pull_request" else status in ("skipped", None)
    assert (result.returncode == 0) == green, result.stdout + result.stderr


def test_the_canonical_gate_still_needs_the_standalone_ratchet_in_the_queue():
    from scripts import ci_reuse

    assert _gate(ci_reuse.gate_steps(), _queue_needs()).returncode == 1
    assert _gate(ci_reuse.gate_steps(), _queue_needs(ratchet_job="success")).returncode == 0


FULL_SUITE_JOBS = ("split", "unit", "shard-check", "coverage-ratchet")


def _selected_needs(scope="selected", selected_job="success", skipped=FULL_SUITE_JOBS):
    needs = {name: {"result": "success"} for name in ("lint", "sonar", "stage-budget", *FULL_SUITE_JOBS)}
    needs.update({name: {"result": "skipped"} for name in skipped})
    needs["reuse"] = {"result": "success", "outputs": {"reused": "false"}}
    needs["queue-baseline"] = {"result": "skipped"}
    needs["select"] = {"result": "success", "outputs": {"scope": scope}}
    needs["unit-selected"] = {"result": selected_job}
    return needs


def test_the_selected_gate_accepts_a_pull_request_that_ran_only_its_selected_tests():
    from scripts import ci_reuse

    steps = ci_reuse.gate_steps(selected=True)
    result = _gate(steps, _selected_needs(), event="pull_request")
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_selected_gate_accepts_a_pull_request_whose_change_selects_nothing():
    from scripts import ci_reuse

    needs = _selected_needs(scope="none", selected_job="skipped")
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event="pull_request")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("event", ["merge_group", "push", "workflow_dispatch"])
@pytest.mark.parametrize("job", FULL_SUITE_JOBS)
def test_the_selected_gate_refuses_a_skipped_full_suite_job_outside_pull_requests(event, job):
    from scripts import ci_reuse

    needs = _selected_needs(selected_job="skipped", skipped=(job,))
    needs["queue-baseline"] = {"result": "success", "outputs": {"baseline": "success", "ratchet": "success"}}
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event=event)
    assert result.returncode == 1
    assert "::error::Every required gate must succeed." in result.stdout


@pytest.mark.parametrize("scope", ["full", "", None])
@pytest.mark.parametrize("job", FULL_SUITE_JOBS)
def test_the_selected_gate_refuses_a_skipped_full_suite_job_on_a_full_scope_pull_request(scope, job):
    from scripts import ci_reuse

    needs = _selected_needs(scope=scope, selected_job="skipped", skipped=(job,))
    if scope is None:
        del needs["select"]["outputs"]
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event="pull_request")
    assert result.returncode == 1
    assert "::error::Every required gate must succeed." in result.stdout


@pytest.mark.parametrize("selected_job", ["skipped", "failure", "cancelled"])
def test_the_selected_gate_refuses_selected_tests_that_did_not_pass_when_they_were_due(selected_job):
    from scripts import ci_reuse

    needs = _selected_needs(selected_job=selected_job)
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event="pull_request")
    assert result.returncode == 1
    assert "::error::Every required gate must succeed." in result.stdout


@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
def test_the_selected_gate_refuses_a_selection_that_did_not_succeed(result):
    from scripts import ci_reuse

    needs = _selected_needs()
    needs["select"]["result"] = result
    outcome = _gate(ci_reuse.gate_steps(selected=True), needs, event="pull_request")
    assert outcome.returncode == 1


def test_the_selected_gate_refuses_a_selected_pull_request_whose_gate_lacks_the_selected_tests():
    from scripts import ci_reuse

    needs = _selected_needs()
    del needs["unit-selected"]
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event="pull_request")
    assert result.returncode == 1
    assert "::error::Every required gate must succeed." in result.stdout


@pytest.mark.parametrize("selection", ["absent", "empty", "unknown"])
def test_the_selected_gate_refuses_skipped_selected_tests_without_a_known_scope(selection):
    from scripts import ci_reuse

    needs = _selected_needs(scope="full", selected_job="skipped", skipped=())
    if selection == "absent":
        del needs["select"]
    else:
        needs["select"]["outputs"]["scope"] = "" if selection == "empty" else "partial"
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event="pull_request")
    assert result.returncode == 1
    assert "::error::Every required gate must succeed." in result.stdout


def test_the_selected_gate_accepts_skipped_selected_tests_on_a_full_scope_pull_request():
    from scripts import ci_reuse

    needs = _selected_needs(scope="full", selected_job="skipped", skipped=())
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event="pull_request")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("event", ["merge_group", "push"])
def test_the_selected_gate_grades_a_full_run_as_the_canonical_gate(event):
    from scripts import ci_reuse

    needs = _selected_needs(scope="full", selected_job="skipped", skipped=())
    needs["queue-baseline"] = {"result": "success", "outputs": {"baseline": "success", "ratchet": "success"}}
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event=event)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_selected_variant_leaves_the_canonical_aggregation_unchanged():
    from scripts import ci_reuse

    [plain] = ci_reuse.gate_steps()
    [selected] = ci_reuse.gate_steps(selected=True)
    assert plain != selected
    assert plain["env"] == selected["env"]


@pytest.mark.parametrize("outcome", ["failure", "cancelled"])
@pytest.mark.parametrize("job", FULL_SUITE_JOBS)
def test_the_selected_gate_refuses_a_full_suite_job_that_ran_and_did_not_pass(job, outcome):
    from scripts import ci_reuse

    needs = _selected_needs()
    needs[job] = {"result": outcome}
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event="pull_request")
    assert result.returncode == 1
    assert "::error::Every required gate must succeed." in result.stdout


@pytest.mark.parametrize("event", ["merge_group", "push"])
def test_the_selected_gate_ignores_a_selected_scope_outside_pull_requests(event):
    from scripts import ci_reuse

    needs = _selected_needs(selected_job="skipped", skipped=())
    needs["queue-baseline"] = {"result": "success", "outputs": {"baseline": "success", "ratchet": "success"}}
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event=event)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("outcome", ["failure", "cancelled"])
@pytest.mark.parametrize("scope", ["full", "none"])
def test_the_selected_gate_refuses_selected_tests_that_ran_and_did_not_pass_when_not_due(scope, outcome):
    from scripts import ci_reuse

    skipped = FULL_SUITE_JOBS if scope == "none" else ()
    needs = _selected_needs(scope=scope, selected_job=outcome, skipped=skipped)
    result = _gate(ci_reuse.gate_steps(selected=True), needs, event="pull_request")
    assert result.returncode == 1
    assert "::error::Every required gate must succeed." in result.stdout
