import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = "public.ecr.aws/docker/library/python:3.12.12-slim-bookworm@sha256:2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301"


def pull(tmp_path, failures, error):
    commands = tmp_path / "bin"
    commands.mkdir()
    calls = tmp_path / "calls"
    waits = tmp_path / "waits"
    docker = commands / "docker"
    docker.write_text("""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$CALLS"
if [[ "$(wc -l < "$CALLS")" -le "$FAILURES" ]]; then
    printf '%s\\n' "$PULL_ERROR" >&2
    exit 17
fi
""")
    sleep = commands / "sleep"
    sleep.write_text("""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$WAITS"
""")
    docker.chmod(0o755)
    sleep.chmod(0o755)
    env = os.environ | {
        "PATH": f"{commands}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "WAITS": str(waits),
        "FAILURES": str(failures),
        "PULL_ERROR": error,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "docker/swarm-node/pull-base.sh")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return (
        result,
        calls.read_text().splitlines() if calls.exists() else [],
        waits.read_text().splitlines() if waits.exists() else [],
    )


@pytest.mark.parametrize("error", ["unexpected status: 429 Too Many Requests", "toomanyrequests: Rate exceeded"])
def test_locked_base_recovers_from_registry_throttling(tmp_path, error):
    result, calls, waits = pull(tmp_path, 2, error)
    assert result.returncode == 0, result.stderr
    assert calls == [f"pull --platform linux/amd64 {BASE}"] * 3
    assert waits == ["15", "30"]
    assert json.loads((ROOT / "docker/swarm-node/versions.lock").read_text())["base_image"] == BASE
    assert f"ARG BASE_IMAGE={BASE}\n" in (ROOT / "docker/swarm-node/Dockerfile").read_text()


def test_persistent_throttling_exhausts_the_pull_bound(tmp_path):
    result, calls, waits = pull(tmp_path, 99, "429 Too Many Requests")
    assert result.returncode == 17
    assert calls == [f"pull --platform linux/amd64 {BASE}"] * 5
    assert waits == ["15", "30", "60", "120"]
    assert "429 Too Many Requests" in result.stderr


def test_unrelated_pull_failure_is_not_retried(tmp_path):
    result, calls, waits = pull(tmp_path, 99, "manifest unknown")
    assert result.returncode == 17
    assert calls == [f"pull --platform linux/amd64 {BASE}"]
    assert waits == []
    assert "manifest unknown" in result.stderr


@pytest.mark.parametrize(
    ("workflow", "job"),
    [
        ("swarm-node-image.yml", "image"),
        ("swarm-node-smoke.yml", "worker-image"),
        ("swarm-node-smoke.yml", "worker-health"),
    ],
)
def test_worker_build_jobs_obtain_the_locked_base_first(workflow, job):
    steps = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"][job]["steps"]
    runs = [step["run"] for step in steps if "run" in step]
    assert runs[0] == "bash docker/swarm-node/pull-base.sh"
    assert "docker build" in runs[1] or "proof.sh" in runs[1] or "smoke.sh" in runs[1]
