import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "local/agentihooks-swarm:clients-check"


def runtime_images() -> list[str]:
    script = (ROOT / "scripts/packaging/pull-runtime-image.sh").read_text()
    return re.findall(r'"\$image" == mirror\.gcr\.io/library/([^\s*)]+)', script)


def assert_pulls_every_runtime_image(run: str) -> None:
    images = runtime_images()
    assert images
    lines = [line.strip() for line in run.splitlines()]
    for image in images:
        assert f"bash scripts/packaging/pull-runtime-image.sh public.ecr.aws/docker/library/{image} &&" in lines, image
        assert any(
            line.startswith(f"docker tag public.ecr.aws/docker/library/{image} mirror.gcr.io/library/{image} ")
            for line in lines
        ), image


def build_command(run: str) -> str:
    builds = [line for line in run.replace("\\\n", "").splitlines() if line.strip().startswith("docker build ")]
    assert len(builds) == 1, builds
    return " ".join(re.sub(r'\$\{\w+:\+--cache-(?:from|to) "\$\w+"\}', "", builds[0]).split()).removesuffix(" &")


def test_main_image_is_built_once_before_every_compatible_smoke() -> None:
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
        assert_pulls_every_runtime_image(step["run"])
        assert build_command(step["run"]) in {
            f"docker build -t {IMAGE} .",
            f"docker build -t {IMAGE} --output type=image,compression=uncompressed .",
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
        "compose-master-drill",
        "stability-smoke",
    ):
        assert build < next(
            i for i, step in enumerate(steps) if f"scripts/packaging/{script}.sh" in step.get("run", "").split()
        )


@pytest.mark.parametrize("commented", [False, True])
def test_the_build_check_fails_when_the_step_loses_any_runtime_image(commented: bool) -> None:
    steps = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]["smoke"]["steps"]
    run = next(step["run"] for step in steps if step.get("id") == "build")
    assert_pulls_every_runtime_image(run)
    for image in runtime_images():
        lost = "\n".join(
            (f"# {line}" if commented else "") if f"/library/{image} " in f"{line} " else line
            for line in run.splitlines()
        )
        with pytest.raises(AssertionError, match=f"(?m)^{re.escape(image)}$"):
            assert_pulls_every_runtime_image(lost)


@pytest.mark.parametrize(
    "script",
    [
        "hive-launch-smoke",
        "swarm-smoke",
        "hive-join-smoke",
        "compose-hive-smoke",
        "compose-task-smoke",
        "compose-recovery-drill",
        "compose-master-drill",
        "stability-smoke",
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
