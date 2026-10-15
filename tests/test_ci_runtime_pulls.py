import os
import subprocess
from pathlib import Path

import pytest

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
        assert len(pulls) == (1 if script == "hive-launch-smoke" else 2)
        assert all("@sha256:runtime" in call for call in pulls)
        assert any("redis:7@sha256:runtime" in call for call in pulls)
        if script != "hive-launch-smoke":
            assert any("agentihooks-swarm:dev@sha256:runtime" in call for call in pulls)
        for pull in pulls:
            image = pull.split()[-1].split("@")[0]
            assert any(call.endswith(f"tag {image}@sha256:runtime {image}") for call in calls)
            assert calls.index(pull) < calls.index(starts[0])
