import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "local/agentihooks-swarm:clients-check"


def test_main_image_is_built_once_before_every_compatible_smoke():
    job = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]["smoke"]
    assert job["env"]["SWARM_SMOKE_IMAGE"] == IMAGE
    steps = job["steps"]
    builds = [
        i
        for i, step in enumerate(steps)
        if step.get("run", "").startswith("docker build ") or step.get("uses") == "docker/build-push-action@v6"
    ]
    assert len(builds) == 1
    build = builds[0]
    step = steps[build]
    if "run" in step:
        assert step["run"] in {
            f"docker build -t {IMAGE} .",
            f"docker build -t {IMAGE} --output type=image,compression=uncompressed .",
            """docker build -t local/agentihooks-swarm:clients-check --output type=image,compression=uncompressed . &
build=$!
status=0
bash scripts/packaging/pull-runtime-image.sh public.ecr.aws/docker/library/redis:7 &&
  docker tag public.ecr.aws/docker/library/redis:7 mirror.gcr.io/library/redis:7 &
redis=$!
bash scripts/packaging/pull-runtime-image.sh public.ecr.aws/docker/library/nats:2.15.1-alpine &&
  docker tag public.ecr.aws/docker/library/nats:2.15.1-alpine mirror.gcr.io/library/nats:2.15.1-alpine &
nats=$!
bash scripts/packaging/pull-runtime-image.sh public.ecr.aws/docker/library/redis:7-alpine &&
  docker tag public.ecr.aws/docker/library/redis:7-alpine mirror.gcr.io/library/redis:7-alpine || status=$?
wait "$redis" || status=$?
wait "$nats" || status=$?
wait "$build" || status=$?
exit "$status"
""",
        }
    else:
        assert step["with"]["context"] == "."
        assert step["with"]["load"] is True
        assert step["with"]["tags"] == IMAGE
    for script in (
        "hive-launch-smoke",
        "swarm-smoke",
        "hive-join-smoke",
        "compose-hive-smoke",
        "compose-task-smoke",
        "compose-recovery-drill",
    ):
        assert build < next(
            i for i, step in enumerate(steps) if f"scripts/packaging/{script}.sh" in step.get("run", "").split()
        )


@pytest.mark.parametrize(
    "script",
    [
        "hive-launch-smoke",
        "swarm-smoke",
        "hive-join-smoke",
        "compose-hive-smoke",
        "compose-task-smoke",
        "compose-recovery-drill",
    ],
)
@pytest.mark.parametrize("shared", [False, True])
def test_smoke_uses_supplied_image_without_building_or_pulling_it(tmp_path, script, shared):
    tools = tmp_path / "bin"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [[ "$1" == manifest ]]; then
  printf '%s\\n' '{"manifests":[{"digest":"sha256:runtime","platform":{"os":"linux","architecture":"amd64"}}]}'
fi
if [[ "$1" == build || "$1" == network && "$2" == create || "$1" == compose && " $* " == *' up '* ]]; then
  printf 'startup boundary reached\\n' >&2
  exit 23
fi
"""
    )
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    result = subprocess.run(
        ["bash", str(ROOT / f"scripts/packaging/{script}.sh")],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "DOCKER_LOG": str(log),
            "SWARM_SMOKE_IMAGE": IMAGE if shared else "",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "startup boundary reached" in result.stderr, result
    calls = log.read_text().splitlines()
    builds = [call for call in calls if call.startswith("build ") or " --build " in f" {call} "]
    assert bool(builds) is not shared
    if shared:
        assert not any(
            "agentihooks-swarm" in call and (call.startswith("manifest ") or " pull " in f" {call} ") for call in calls
        )
        if script == "hive-join-smoke":
            assert not any(call.startswith("image rm ") and IMAGE in call for call in calls)
        else:
            alias = (
                "local/agentihooks-hive-proof"
                if script == "hive-launch-smoke"
                else "ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev"
            )
            assert f"tag {IMAGE} {alias}" in calls
            assert any(" --no-build " in f" {call} " for call in calls)
