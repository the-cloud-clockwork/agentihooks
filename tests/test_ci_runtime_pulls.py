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
        assert len(pulls) == (2 if script == "hive-launch-smoke" else 3)
        assert all("@sha256:runtime" in call for call in pulls)
        assert any("redis:7@sha256:runtime" in call for call in pulls)
        if script != "hive-launch-smoke":
            assert any("nats:2.15.1-alpine@sha256:runtime" in call for call in pulls)
        if script != "hive-launch-smoke":
            assert any("agentihooks-swarm:dev@sha256:runtime" in call for call in pulls)
        for pull in pulls:
            image = pull.split()[-1].split("@")[0]
            assert any(call.endswith(f"tag {image}@sha256:runtime {image}") for call in calls)
            assert calls.index(pull) < calls.index(starts[0])


LAYER_DOCKER = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$PULL_LOG"
stall() { exec python3 -c 'import time; time.sleep(30)'; }
if [[ "$1" == manifest ]]; then
  if (( "$(grep -c '^manifest ' "$PULL_LOG")" <= ${MANIFEST_STALLS:-0} )); then stall; fi
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
  if (( attempt <= ${STALLS:-0} )); then stall; fi
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


def pull_runtime(tmp_path, failures, manifest_failures=0, stalls=0, manifest_stalls=0, extra_env=None):
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
            "STALLS": str(stalls),
            "MANIFEST_STALLS": str(manifest_stalls),
            "PULL_ATTEMPT_SECONDS": "1",
            **(extra_env or {}),
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


@pytest.mark.parametrize("extra_env,delay", [(None, "10"), ({"PULL_RETRY_SECONDS": "1"}, "1")])
def test_runtime_pull_waits_the_configured_retry_delay_between_all_three_attempts(tmp_path, extra_env, delay):
    result, pulls, tags, waits = pull_runtime(tmp_path, 99, extra_env=extra_env)
    assert result.returncode == 17
    assert len(pulls) == 3
    assert waits == [delay, delay]
    assert result.stderr.count(f"retrying in {delay} seconds") == 2


def test_attestation_proof_runs_its_negative_pull_beside_the_positive_one():
    proof = (ROOT / "scripts/packaging/hive-launch-attestation-proof.sh").read_text()
    negative = proof.index("PULL_RETRY_SECONDS=1 bash scripts/packaging/pull-runtime-image.sh")
    positive = proof.index("bash scripts/packaging/pull-runtime-image.sh 127.0.0.1:5000/proof:present")
    assert negative < positive
    assert '--insecure > "$proof_dir/pull.log" 2>&1 &\nnegative=$!' in proof
    assert proof.index('if wait "$negative"; then') > positive
    assert "grep -c 'failed on attempt' \"$proof_dir/pull.log\" | grep -x 2" in proof
    assert proof.index('wait "$daemon_start"') < proof.index("for attempt in {1..30}")


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


@pytest.mark.parametrize("stalls,manifest_stalls,expected_pulls", [(1, 0, 2), (0, 1, 1)])
def test_runtime_pull_retries_a_stalled_mirror_within_the_same_bound(tmp_path, stalls, manifest_stalls, expected_pulls):
    result, pulls, tags, waits = pull_runtime(tmp_path, 0, stalls=stalls, manifest_stalls=manifest_stalls)
    assert result.returncode == 0, result.stderr
    assert len(pulls) == expected_pulls
    assert len(waits) == 1
    assert len(tags) == 1
    assert "failed on attempt 1 of 3" in result.stderr


@pytest.mark.parametrize("stalls,manifest_stalls,expected_pulls", [(99, 0, 3), (0, 99, 0)])
def test_runtime_pull_fails_with_the_timeout_status_after_three_stalled_attempts(
    tmp_path, stalls, manifest_stalls, expected_pulls
):
    result, pulls, tags, waits = pull_runtime(tmp_path, 0, stalls=stalls, manifest_stalls=manifest_stalls)
    assert result.returncode == 124
    assert len(pulls) == expected_pulls
    assert len(waits) == 2
    assert not tags


def test_runtime_pull_falls_back_to_the_mirror_when_public_ecr_throttles(tmp_path):
    image = "public.ecr.aws/docker/library/redis:7"
    log = tmp_path / "docker.log"
    tools = tools_with(
        tmp_path,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$PULL_LOG"
if [[ "$1" == manifest ]]; then
  if [[ "$3" == public.ecr.aws/* ]]; then
    echo 'toomanyrequests: Rate exceeded' >&2
    exit 1
  fi
  printf '%s\\n' '{"manifests":[{"digest":"sha256:runtime","platform":{"os":"linux","architecture":"amd64"}}]}'
fi
""",
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/packaging/pull-runtime-image.sh"), image],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "PULL_LOG": str(log),
            "WAITS": str(tmp_path / "waits.log"),
            "PULL_ATTEMPT_SECONDS": "1",
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert [call.split()[-1] for call in calls if call.startswith("manifest inspect ")] == [
        image,
        "mirror.gcr.io/library/redis:7",
    ]
    assert [call for call in calls if " pull " in f" {call} "][-1].endswith(
        " pull mirror.gcr.io/library/redis:7@sha256:runtime"
    )
    assert calls[-1].endswith(f" tag mirror.gcr.io/library/redis:7@sha256:runtime {image}")


@pytest.mark.parametrize(
    "image,architecture,expected",
    [
        ("redis:7", "amd64", 0),
        ("redis:7-alpine", "amd64", 0),
        ("nats:2.15.1-alpine", "amd64", 0),
        ("nats:2.15.1-alpine", "arm64", 17),
        ("redis:7", "arm64", 17),
        ("python:3.12-slim-bookworm", "amd64", 17),
    ],
)
def test_smoke_reuses_only_a_verified_local_redis_image(tmp_path, image, architecture, expected):
    image = f"mirror.gcr.io/library/{image}"
    log = tmp_path / "docker.log"
    tools = tools_with(
        tmp_path,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$PULL_LOG"
if [[ "$1" == --host ]]; then shift 2; fi
if [[ "$1" == image && "$2" == inspect ]]; then
  printf '%s\\n' "$LOCAL_ARCH"
  exit 0
fi
exit 17
""",
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/packaging/pull-runtime-image.sh"), image],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "PULL_LOG": str(log),
            "WAITS": str(tmp_path / "waits.log"),
            "SWARM_SMOKE_RUNTIME_IMAGES_READY": "true",
            "LOCAL_ARCH": architecture,
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == expected, result.stderr
    calls = log.read_text().splitlines()
    if expected == 0:
        assert len(calls) == 1
        assert " image inspect " in f" {calls[0]} "
        assert calls[0].endswith(image)
    else:
        assert any(call.startswith("manifest inspect ") for call in calls)


@pytest.mark.parametrize("failure", ["", "redis:7", "redis:7-alpine", "nats:2.15.1-alpine", "build"])
def test_the_smoke_build_overlaps_runtime_pulls_and_preserves_failures(tmp_path, failure):
    log = tmp_path / "docker.log"
    tools = tools_with(
        tmp_path,
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$PULL_LOG"
if [[ "$1" == manifest ]]; then
  printf '%s\\n' '{"manifests":[{"digest":"sha256:runtime","platform":{"os":"linux","architecture":"amd64"}}]}'
  exit 0
fi
if [[ "$1" == --host ]]; then shift 2; fi
if [[ "$1" == pull ]]; then
  image="${2%%@*}"
  touch "$SYNC/${image##*/}"
  for _ in {1..200}; do
    if [[ -e "$SYNC/build" ]]; then break; fi
    /bin/sleep 0.01
  done
  [[ -e "$SYNC/build" ]] || exit 18
  if [[ -n "$FAILURE" && "$image" == *"$FAILURE" ]]; then exit 17; fi
elif [[ "$1" == build ]]; then
  touch "$SYNC/build"
  for _ in {1..200}; do
    if [[ -e "$SYNC/redis:7" && -e "$SYNC/redis:7-alpine" ]]; then break; fi
    /bin/sleep 0.01
  done
  [[ -e "$SYNC/redis:7" && -e "$SYNC/redis:7-alpine" ]] || exit 19
  if [[ "$FAILURE" == build ]]; then exit 23; fi
fi
""",
    )
    sync = tmp_path / "sync"
    sync.mkdir()
    workflow = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())
    step = next(step for step in workflow["jobs"]["smoke"]["steps"] if step.get("name") == "Build the swarm image")
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "PULL_LOG": str(log),
            "WAITS": str(tmp_path / "waits.log"),
            "SYNC": str(sync),
            "FAILURE": failure,
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == (23 if failure == "build" else 17 if failure else 0), result.stderr
    calls = log.read_text().splitlines()
    assert sum(call.startswith("build ") for call in calls) == 1
    assert (sync / "redis:7").exists()
    assert (sync / "redis:7-alpine").exists()
    assert (sync / "nats:2.15.1-alpine").exists()
    assert {call.split()[1] for call in calls if call.startswith("tag ")} == {
        f"public.ecr.aws/docker/library/{image}"
        for image in ("redis:7", "redis:7-alpine", "nats:2.15.1-alpine")
        if image != failure
    }


def dockerfile_bases():
    return {line.split()[1] for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("FROM ")}


def test_kind_proof_pulls_every_dockerfile_base_through_the_bounded_pull_before_building():
    script = (ROOT / "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh").read_text()
    build = script.index('docker build -q -t "$image"')
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
    build = next(i for i, step in enumerate(steps) if f"scripts/packaging/{script}.sh" in step.get("run", "").split())
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
