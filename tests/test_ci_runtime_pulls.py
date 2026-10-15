import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    (script, missing)
    for script in ("compose-hive-smoke", "swarm-smoke", "hive-launch-smoke")
    for missing in ("", "redis", "swarm")
    if script != "hive-launch-smoke" or missing != "swarm"
]


@pytest.mark.parametrize("script,missing", CASES)
def test_smoke_pulls_runtime_images_before_startup(tmp_path, script, missing):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "docker.log"
    docker = tools / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$PULL_LOG"
if [[ "$1" == manifest ]]; then
  printf '%s\\n' '{"manifests":[{"digest":"sha256:arm","platform":{"os":"linux","architecture":"arm64"}},{"digest":"sha256:runtime","platform":{"os":"linux","architecture":"amd64"}},{"digest":"sha256:absent","platform":{"os":"unknown","architecture":"unknown"}}]}'
  exit 0
fi
if [[ "$1" == --host ]]; then shift 2; fi
if [[ "$1" == pull ]]; then
  if [[ "$2" != *@sha256:runtime ]]; then
    echo 'registry descriptor: not found' >&2
    exit 1
  fi
  if [[ -n "$MISSING_IMAGE" && "$2" == *"$MISSING_IMAGE"* ]]; then
    echo 'runtime image: not found' >&2
    exit 1
  fi
fi
if [[ "$1" == compose ]]; then
  shift
  if [[ "$1" == -f ]]; then shift 2; fi
  if [[ "$1" == up ]]; then
    if [[ " $* " != *' --pull never '* ]]; then
      echo 'registry descriptor: not found' >&2
      exit 1
    fi
    echo 'runtime images ready for startup'
    exit 42
  fi
fi
"""
    )
    docker.chmod(0o755)
    (tools / "sleep").write_text(SLEEP)
    (tools / "sleep").chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / f"scripts/packaging/{script}.sh")],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "PULL_LOG": str(log),
            "MISSING_IMAGE": missing,
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    calls = log.read_text().splitlines()
    starts = [call for call in calls if " up " in f" {call} "]
    if missing:
        assert result.returncode != 0
        assert "runtime image: not found" in result.stderr
        assert not starts
    else:
        assert "runtime images ready for startup" in result.stdout, result.stderr
        assert len(starts) == 1
        pulls = [call for call in calls if " pull " in f" {call} "]
        assert len(pulls) == 2
        assert all("@sha256:runtime" in call for call in pulls)
        assert any("redis:7@sha256:runtime" in call for call in pulls)
        if script != "hive-launch-smoke":
            assert any("agentihooks-swarm:dev@sha256:runtime" in call for call in pulls)
        for pull in pulls:
            image = pull.split()[-1].split("@")[0]
            assert any(call.endswith(f"tag {image}@sha256:runtime {image}") for call in calls)
            assert calls.index(pull) < calls.index(starts[0])


LAYER_DOCKER = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$PULL_LOG"
if [[ "$1" == manifest ]]; then
  if (( "$(grep -c '^manifest ' "$PULL_LOG")" <= ${MANIFEST_FAILURES:-0} )); then
    echo 'manifest lookup: registry unavailable' >&2
    exit 18
  fi
  printf '%s\\n' '{"manifests":[{"digest":"sha256:runtime","platform":{"os":"linux","architecture":"amd64"}}]}'
  exit 0
fi
if [[ "$1" == --host ]]; then shift 2; fi
if [[ "$1" == pull ]]; then
  attempt="$(grep -c ' pull ' "$PULL_LOG")"
  if (( attempt <= FAILURES )); then
    echo "could not fetch content descriptor sha256:layer from remote: not found on attempt $attempt" >&2
    exit 17
  fi
fi
"""
SLEEP = '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "${WAITS:-/dev/null}"\n'


def tools_with(tmp_path, docker):
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, body in (("docker", docker), ("sleep", SLEEP)):
        (tools / name).write_text(body)
        (tools / name).chmod(0o755)
    return tools


def pull_runtime(tmp_path, failures, manifest_failures=0):
    tools = tools_with(tmp_path, LAYER_DOCKER)
    log, waits = tmp_path / "docker.log", tmp_path / "waits.log"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/packaging/pull-runtime-image.sh"), "mirror.gcr.io/library/redis:7"],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "PULL_LOG": str(log),
            "WAITS": str(waits),
            "FAILURES": str(failures),
            "MANIFEST_FAILURES": str(manifest_failures),
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    calls = log.read_text().splitlines()
    pulls = [call for call in calls if " pull " in f" {call} "]
    tags = [call for call in calls if " tag " in f" {call} "]
    return result, pulls, tags, waits.read_text().splitlines() if waits.exists() else []


@pytest.mark.parametrize("failures", [1, 2])
def test_runtime_pull_retries_a_missing_registry_layer(tmp_path, failures):
    result, pulls, tags, waits = pull_runtime(tmp_path, failures)
    assert result.returncode == 0, result.stderr
    assert len(pulls) == failures + 1
    assert len(waits) == failures
    assert len(tags) == 1


def test_runtime_pull_stops_at_three_attempts_and_keeps_the_final_failure(tmp_path):
    result, pulls, tags, waits = pull_runtime(tmp_path, 99)
    assert result.returncode == 17
    assert len(pulls) == 3
    assert len(waits) == 2
    assert not tags
    assert result.stderr.rstrip().endswith("not found on attempt 3")


def test_runtime_pull_retries_a_failed_digest_lookup_within_the_same_bound(tmp_path):
    result, pulls, tags, waits = pull_runtime(tmp_path, 0, manifest_failures=1)
    assert result.returncode == 0, result.stderr
    assert len(pulls) == 1
    assert len(waits) == 1
    assert len(tags) == 1
    (tmp_path / "exhausted").mkdir()
    result, pulls, tags, waits = pull_runtime(tmp_path / "exhausted", 0, manifest_failures=99)
    assert result.returncode != 0
    assert not pulls
    assert len(waits) == 2
    assert result.stderr.count("manifest lookup: registry unavailable") == 3


def dockerfile_bases():
    return {line.split()[1] for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("FROM ")}


def test_kind_proof_pulls_every_dockerfile_base_through_the_bounded_pull_before_building():
    script = (ROOT / "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh").read_text()
    build = script.index('docker build -q -t "$image" .')
    for base in dockerfile_bases():
        pull = f"bash scripts/packaging/pull-runtime-image.sh {base}"
        assert pull in script
        assert script.index(pull) < build


def test_hive_join_pulls_its_images_through_the_bounded_pull_before_starting_them():
    script = (ROOT / "scripts/packaging/hive-join-smoke.sh").read_text()
    build = script.index('docker build --progress=plain -t "$image" .')
    for image in {"mirror.gcr.io/library/redis:7-alpine", *dockerfile_bases()}:
        pull = f"bash scripts/packaging/pull-runtime-image.sh {image}\n"
        assert pull in script
        assert script.index(pull) < build


@pytest.mark.parametrize("job,script", [("smoke", "swarm-smoke"), ("compose-hive", "compose-hive-smoke")])
def test_compose_smoke_jobs_pull_every_dockerfile_base_before_building(job, script):
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())
    smoke = workflow["jobs"].get(job, workflow["jobs"]["smoke"])
    assert not smoke.get("needs")
    steps = smoke["steps"]
    build = next(i for i, step in enumerate(steps) if step.get("run") == f"bash scripts/packaging/{script}.sh")
    for base in dockerfile_bases():
        pull = next(
            i
            for i, step in enumerate(steps)
            if step.get("run") == f"bash scripts/packaging/pull-runtime-image.sh {base}"
        )
        assert pull < build
        assert "if" not in steps[pull]
        assert "continue-on-error" not in steps[pull]


def test_hive_launch_pulls_the_worker_base_and_never_retries_the_launch(tmp_path):
    script = (ROOT / "scripts/packaging/hive-launch-smoke.sh").read_text()
    for base in dockerfile_bases():
        assert f"bash scripts/packaging/pull-runtime-image.sh {base}" in script
    failing_up = """if [[ "$1" == compose && " $* " == *' up '* ]]; then
  echo 'worker never became healthy' >&2
  exit 1
fi
"""
    tools = tools_with(tmp_path, LAYER_DOCKER + failing_up)
    log = tmp_path / "docker.log"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/packaging/hive-launch-smoke.sh")],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "PULL_LOG": str(log),
            "WAITS": str(tmp_path / "waits.log"),
            "FAILURES": "0",
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    calls = log.read_text().splitlines()
    assert result.returncode == 1
    assert "worker never became healthy" in result.stderr
    assert len([call for call in calls if " up " in f" {call} "]) == 1
