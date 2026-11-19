import subprocess
from pathlib import Path

import pytest


@pytest.mark.feature("kind-controller-readiness")
@pytest.mark.parametrize(
    ("failed_probe", "failures", "status", "requests"),
    [
        ("readiness", 0, 0, ["startup", "liveness", "readiness"]),
        ("readiness", 2, 0, ["startup", "liveness", "readiness", "readiness", "readiness"]),
        ("readiness", 99, 1, ["startup", "liveness", "readiness", "readiness", "readiness"]),
        ("startup", 1, 1, ["startup"]),
        ("liveness", 1, 1, ["startup", "liveness"]),
    ],
)
def test_kind_probe_waits_only_for_readiness(
    failed_probe: str, failures: int, status: int, requests: list[str]
) -> None:
    root = Path(__file__).resolve().parents[1]
    smoke = (root / "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh").read_text()
    start = smoke.index("for probe in startup liveness readiness; do")
    end = smoke.index('kubectl wait pod "${controller[@]}" --for=condition=Ready', start)
    stubs = r"""
set -euo pipefail
release=agentihooks-swarm
attempts=0
sleep() { SECONDS=$((SECONDS + 30)); }
kubectl() {
  if [[ "$1" == get ]]; then
    if [[ "$*" == *httpGet.path* ]]; then
      if [[ "$probe" == readiness ]]; then
        echo /v2/health/ready
      else
        echo /v2/health/live
      fi
    elif [[ "$*" == *httpGet.port* ]]; then
      echo api
    else
      echo 127.0.0.1:8780
    fi
    return
  fi
  printf 'request %s\n' "$probe"
  if [[ "$probe" == "$FAILED_PROBE" ]]; then
    attempts=$((attempts + 1))
    if [[ "$attempts" -le "$FAILURES" ]]; then
      return 1
    fi
  fi
}
"""
    result = subprocess.run(
        ["bash", "-c", stubs + smoke[start:end]],
        env={"FAILED_PROBE": failed_probe, "FAILURES": str(failures)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == status, result.stdout + result.stderr
    assert [
        line.removeprefix("request ") for line in result.stdout.splitlines() if line.startswith("request ")
    ] == requests
    if status:
        assert f"controller {failed_probe} HTTP probe passed" not in result.stdout
