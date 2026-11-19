import io
import shlex
import subprocess
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.mark.feature("kind-controller-readiness")
@pytest.mark.parametrize(
    ("failed_probe", "failures", "sleep_seconds", "request_seconds", "status", "requests"),
    [
        ("readiness", 0, 1, 0, 0, ["startup", "liveness", "readiness"]),
        ("readiness", 2, 1, 0, 0, ["startup", "liveness", "readiness", "readiness", "readiness"]),
        ("readiness", 99, 30, 0, 1, ["startup", "liveness", "readiness", "readiness"]),
        ("startup", 1, 1, 0, 1, ["startup"]),
        ("liveness", 1, 1, 0, 1, ["startup", "liveness"]),
        ("readiness", 1, 60, 0, 1, ["startup", "liveness", "readiness"]),
        ("readiness", 0, 1, 60, 1, ["startup", "liveness", "readiness"]),
    ],
)
def test_kind_probe_waits_only_for_readiness(
    failed_probe: str, failures: int, sleep_seconds: int, request_seconds: int, status: int, requests: list[str]
) -> None:
    root = Path(__file__).resolve().parents[1]
    smoke = (root / "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh").read_text()
    start = smoke.index("for probe in startup liveness readiness; do")
    end = smoke.index('kubectl wait pod "${controller[@]}" --for=condition=Ready', start)
    stubs = r"""
set -euo pipefail
release=agentihooks-swarm
attempts=0
sleep() { SECONDS=$((SECONDS + SLEEP_SECONDS)); }
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
    SECONDS=$((SECONDS + REQUEST_SECONDS))
    attempts=$((attempts + 1))
    if [[ "$attempts" -le "$FAILURES" ]]; then
      return 1
    fi
  fi
}
"""
    result = subprocess.run(
        ["bash", "-c", stubs + smoke[start:end]],
        env={
            "FAILED_PROBE": failed_probe,
            "FAILURES": str(failures),
            "SLEEP_SECONDS": str(sleep_seconds),
            "REQUEST_SECONDS": str(request_seconds),
        },
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


@pytest.mark.feature("kind-controller-readiness")
def test_kind_probe_keeps_the_failed_http_response_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).resolve().parents[1]
    smoke = (root / "deploy/helm/agentihooks-swarm/ci/kind-smoke.sh").read_text()
    start = smoke.index("for probe in startup liveness readiness; do")
    end = smoke.index('kubectl wait pod "${controller[@]}" --for=condition=Ready', start)
    command = shlex.split(smoke[start:end])
    program = command[command.index("python") + 2]
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8780/v2/health/ready",
        503,
        "Service Unavailable",
        Message(),
        io.BytesIO(b"readiness check busy"),
    )
    monkeypatch.setenv("PROBE_URL", error.url)
    monkeypatch.setenv("PROBE_HOST", "127.0.0.1:8780")
    monkeypatch.setattr(urllib.request, "urlopen", Mock(side_effect=error))

    with pytest.raises(urllib.error.HTTPError):
        exec(program, {})

    assert capsys.readouterr().out == "readiness check busy\n"
