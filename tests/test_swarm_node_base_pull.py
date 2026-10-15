import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = "public.ecr.aws/docker/library/python:3.12.12-slim-bookworm@sha256:2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301"
DIGEST = BASE.split("@", 1)[1]
COPY = f"ghcr.io/the-cloud-clockwork/agentihooks-worker-base@{DIGEST}"
COPY_TAG = "ghcr.io/the-cloud-clockwork/agentihooks-worker-base:" + DIGEST.replace(":", "-")
CONTEXT = '${WORKER_BASE_CONTEXT:+--build-context "$WORKER_BASE_CONTEXT"}'
MISSING = "Error response from daemon: manifest unknown"
PULL_DOCKER = """printf '%s\\n' "$*" >> "$CALLS"
if [[ "$4" == ghcr.io/* ]]; then
    if [[ -n "$COPY_ERROR" && "$(grep -c "^pull --platform linux/amd64 ghcr.io" "$CALLS")" -le "$COPY_FAILURES" ]]; then
        printf '%s\\n' "$COPY_ERROR" >&2
        exit 18
    fi
elif [[ "$(grep -c "^pull --platform linux/amd64 public" "$CALLS")" -le "$FAILURES" ]]; then
    printf '%s\\n' "$PULL_ERROR" >&2
    exit 17
fi
"""
COPY_DOCKER = """printf '%s\\n' "$*" >> "$CALLS"
case "$1 $2 ${3:-}" in
    "login ghcr.io -u") cat > "$LOGIN" ;;
    "buildx imagetools create")
        if [[ "$CREATES" == fail ]]; then exit 1; fi
        if [[ -n "$CREATES" ]]; then touch "$COPIED"; fi ;;
    "buildx imagetools inspect") [[ -e "$COPIED" ]] ;;
esac
"""


def run(tmp_path, script, docker, **variables):
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, body in (("docker", docker), ("sleep", 'printf \'%s\\n\' "$*" >> "$WAITS"\n')):
        path = commands / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
        path.chmod(0o755)
    files = {name: tmp_path / name for name in ("calls", "waits", "github_env", "login", "copied")}
    hidden = {"GITHUB_TOKEN", "GITHUB_ACTOR", "GITHUB_ENV", "WORKER_BASE_CONTEXT"}
    env = {key: value for key, value in os.environ.items() if key not in hidden} | {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "CALLS": str(files["calls"]),
        "WAITS": str(files["waits"]),
        "LOGIN": str(files["login"]),
        "COPIED": str(files["copied"]),
        "GITHUB_ENV": str(files["github_env"]),
        **variables,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "docker/swarm-node" / script)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    read = {name: path.read_text() if path.exists() else "" for name, path in files.items()}
    return result, read["calls"].splitlines(), read["waits"].splitlines(), read


def pull(tmp_path, failures, error, copy_error=MISSING, copy_failures=99):
    return run(
        tmp_path,
        "pull-base.sh",
        PULL_DOCKER,
        FAILURES=str(failures),
        PULL_ERROR=error,
        COPY_ERROR=copy_error,
        COPY_FAILURES=str(copy_failures),
    )


def test_a_throttled_copy_is_retried_before_any_fallback(tmp_path):
    result, calls, waits, files = pull(tmp_path, 99, "", copy_error="toomanyrequests: Rate exceeded", copy_failures=2)
    assert result.returncode == 0, result.stderr
    assert calls == [f"pull --platform linux/amd64 {COPY}"] * 3
    assert waits == ["15", "30"]
    assert files["github_env"] == f"WORKER_BASE_CONTEXT={BASE}=docker-image://{COPY}\n"


def test_locked_base_comes_from_the_registry_copy_and_feeds_every_build(tmp_path):
    result, calls, waits, files = pull(tmp_path, 99, "toomanyrequests: Data limit exceeded", copy_error="")
    assert result.returncode == 0, result.stderr
    assert calls == [f"pull --platform linux/amd64 {COPY}"]
    assert waits == []
    assert files["github_env"] == f"WORKER_BASE_CONTEXT={BASE}=docker-image://{COPY}\n"
    assert f"from the registry copy {COPY}" in result.stdout


@pytest.mark.parametrize(
    "missing",
    [MISSING, "Error response from daemon: denied", "Error response from daemon: unauthorized", "not found"],
)
def test_a_missing_copy_falls_back_to_the_public_base(tmp_path, missing):
    result, calls, waits, files = pull(tmp_path, 0, "", copy_error=missing)
    assert result.returncode == 0, result.stderr
    assert calls == [f"pull --platform linux/amd64 {COPY}", f"pull --platform linux/amd64 {BASE}"]
    assert files["github_env"] == ""
    assert "registry copy is missing" in result.stderr


def test_an_unreadable_copy_is_not_bypassed(tmp_path):
    result, calls, waits, files = pull(tmp_path, 0, "", copy_error="dial tcp: i/o timeout")
    assert result.returncode == 18
    assert calls == [f"pull --platform linux/amd64 {COPY}"]
    assert files["github_env"] == ""
    assert "i/o timeout" in result.stderr


@pytest.mark.parametrize("error", ["unexpected status: 429 Too Many Requests", "toomanyrequests: Rate exceeded"])
def test_locked_base_recovers_from_registry_throttling(tmp_path, error):
    result, calls, waits, _ = pull(tmp_path, 2, error)
    assert result.returncode == 0, result.stderr
    assert calls == [f"pull --platform linux/amd64 {COPY}"] + [f"pull --platform linux/amd64 {BASE}"] * 3
    assert waits == ["15", "30"]
    assert json.loads((ROOT / "docker/swarm-node/versions.lock").read_text())["base_image"] == BASE
    assert f"ARG BASE_IMAGE={BASE}\n" in (ROOT / "docker/swarm-node/Dockerfile").read_text()


def test_persistent_throttling_exhausts_the_pull_bound(tmp_path):
    result, calls, waits, _ = pull(tmp_path, 99, "429 Too Many Requests")
    assert result.returncode == 17
    assert calls == [f"pull --platform linux/amd64 {COPY}"] + [f"pull --platform linux/amd64 {BASE}"] * 5
    assert waits == ["15", "30", "60", "120"]
    assert "429 Too Many Requests" in result.stderr


def test_unrelated_pull_failure_is_not_retried(tmp_path):
    result, calls, waits, _ = pull(tmp_path, 99, "manifest unknown")
    assert result.returncode == 17
    assert calls == [f"pull --platform linux/amd64 {COPY}", f"pull --platform linux/amd64 {BASE}"]
    assert waits == []
    assert "manifest unknown" in result.stderr


def copy(tmp_path, present=False, creates="yes"):
    if present:
        (tmp_path / "copied").touch()
    return run(
        tmp_path,
        "copy-base.sh",
        COPY_DOCKER,
        GITHUB_TOKEN="job-token",
        GITHUB_ACTOR="octocat",
        CREATES=creates,
    )


def test_a_missing_copy_is_written_from_the_locked_digest_with_the_job_token(tmp_path):
    result, calls, _, files = copy(tmp_path)
    assert result.returncode == 0, result.stderr
    assert calls == [
        f"buildx imagetools inspect {COPY}",
        "login ghcr.io -u octocat --password-stdin",
        f"buildx imagetools create --tag {COPY_TAG} {BASE}",
        "logout ghcr.io",
        f"buildx imagetools inspect {COPY}",
    ]
    assert files["login"].strip() == "job-token"


def test_a_present_copy_is_left_alone(tmp_path):
    result, calls, _, files = copy(tmp_path, present=True)
    assert result.returncode == 0, result.stderr
    assert calls == [f"buildx imagetools inspect {COPY}"] * 2
    assert files["login"] == ""


def test_a_copy_anonymous_pulls_cannot_read_fails_the_job(tmp_path):
    result, calls, _, _ = copy(tmp_path, creates="")
    assert result.returncode != 0
    assert calls[-1] == f"buildx imagetools inspect {COPY}"
    assert "anonymous" in result.stderr


def test_a_failed_copy_still_drops_the_job_token_login(tmp_path):
    result, calls, _, _ = copy(tmp_path, creates="fail")
    assert result.returncode != 0
    assert calls[-2:] == [f"buildx imagetools create --tag {COPY_TAG} {BASE}", "logout ghcr.io"]


@pytest.mark.parametrize(
    ("workflow", "job"),
    [
        ("swarm-node-image.yml", "image"),
        ("swarm-node-smoke.yml", "worker-image"),
        ("swarm-node-smoke.yml", "worker-health"),
    ],
)
def test_worker_build_jobs_obtain_the_locked_base_first(workflow, job):
    jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
    if job == "worker-health" and job not in jobs:
        pytest.skip("worker health runs inside the startup worker image leg")
    steps = jobs[job]["steps"]
    runs = [step["run"] for step in steps if "run" in step]
    assert runs[0] == "bash docker/swarm-node/pull-base.sh"
    assert "docker build" in runs[1] or "proof.sh" in runs[1] or "smoke.sh" in runs[1]


def _base_builds(text):
    joined = re.sub(r"\\\n\s*", " ", text)
    return [line for line in joined.splitlines() if "docker build" in line and "docker/swarm-node/Dockerfile" in line]


@pytest.mark.parametrize(
    ("source", "count"),
    [
        ("docker/swarm-node/smoke.sh", 3),
        ("tests/integration/swarm_node/run_health_proof.sh", 1),
        (".github/workflows/swarm-node-image.yml", 1),
    ],
)
def test_every_locked_base_build_resolves_through_the_copy_context(source, count):
    text = (ROOT / source).read_text()
    if source.endswith(".yml"):
        jobs = yaml.safe_load(text)["jobs"].values()
        text = "\n".join(step.get("run", "") for job in jobs for step in job["steps"])
    builds = _base_builds(text)
    assert len(builds) == count
    assert all(CONTEXT in build for build in builds)


def test_dev_copies_the_locked_base_whenever_the_lock_changes():
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-node-base.yml").read_text())
    assert workflow[True] == {
        "push": {
            "branches": ["dev"],
            "paths": [
                "docker/swarm-node/versions.lock",
                "docker/swarm-node/base-copy.sh",
                "docker/swarm-node/copy-base.sh",
                ".github/workflows/swarm-node-base.yml",
            ],
        }
    }
    assert workflow["permissions"] == {"contents": "read"}
    (job,) = workflow["jobs"].values()
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    step = next(step for step in job["steps"] if "run" in step)
    assert step["run"] == "bash docker/swarm-node/copy-base.sh"
    assert step["env"] == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
