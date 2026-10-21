import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("missing", ["", "swarm", "redis"])
def test_smoke_selects_runtime_manifests_and_keeps_missing_images_red(tmp_path, missing):
    tools = tmp_path / "bin"
    tools.mkdir()
    log = tmp_path / "docker.log"
    docker = tools / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$PULL_LOG"
if [[ "$1" == manifest ]]; then
  printf '%s\\n' '{"manifests":[{"digest":"sha256:runtime","platform":{"os":"linux","architecture":"amd64"}},{"digest":"sha256:attestation","platform":{"os":"unknown","architecture":"unknown"}}]}'
  exit 0
fi
if [[ "$1" == --host ]]; then shift 2; fi
if [[ "$1" == pull ]]; then
  if [[ "$2" != *@sha256:runtime ]]; then
    echo 'application/vnd.in-toto+json: not found' >&2
    exit 1
  fi
  if [[ -n "$MISSING_IMAGE" && "$2" == *"$MISSING_IMAGE"* ]]; then
    echo 'runtime image: not found' >&2
    exit 1
  fi
fi
if [[ "$1" == compose && "$2" == up && " $* " != *' --pull never '* ]]; then
  echo 'application/vnd.in-toto+json: not found' >&2
  exit 1
fi
"""
    )
    docker.chmod(0o755)
    python = tools / "python3"
    python.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == -c && "$2" == *json.load* ]]; then
  exec "$REAL_PYTHON" "$@"
elif [[ "${1:-}" == -c ]]; then
  echo smoke-fixture
else
  cat >/dev/null
fi
"""
    )
    python.chmod(0o755)
    sleep = tools / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nset -euo pipefail\n")
    sleep.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/packaging/swarm-smoke.sh")],
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "PULL_LOG": str(log),
            "MISSING_IMAGE": missing,
            "REAL_PYTHON": os.sys.executable,
        },
        text=True,
        capture_output=True,
        timeout=10,
    )
    calls = log.read_text().splitlines()
    if missing:
        assert result.returncode != 0
        assert "runtime image: not found" in result.stderr
        assert not any(call.startswith("compose up") for call in calls)
    else:
        assert result.returncode == 0, result.stderr
        assert "Swarm container smoke passed" in result.stdout
        pulls = [call for call in calls if " pull " in f" {call} "]
        assert len(pulls) == 2
        assert all("@sha256:runtime" in call for call in pulls)
        assert any("redis:7@sha256:runtime" in call for call in pulls)
        assert any("agentihooks-swarm:dev@sha256:runtime" in call for call in pulls)
