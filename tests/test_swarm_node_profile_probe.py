import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("profile_probe", ROOT / "tests" / "swarm_node" / "profile_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
TOKEN = "ghp_" + "a1" * 18


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def ran(tmp: Path, target: str, status: int, output: str) -> None:
    (tmp / f"{target}-run.txt").write_text(output, encoding="utf-8")
    (tmp / f"{target}-run.exit").write_text(f"{status}\n", encoding="utf-8")


def register(attempt: Path, target: str) -> None:
    home = attempt / "homes" / target / ".agentihooks"
    home.mkdir(parents=True)
    (home / "active-sessions.json").write_text(json.dumps({"s1": {"cwd": "/home/worker/work"}}), encoding="utf-8")


def test_diagnostics_keep_exit_status_redacted_output_and_registration_time(tmp_path):
    attempt = tmp_path / "a1"
    ran(tmp_path, "claude", 0, f"loaded {attempt}/homes/claude\n")
    ran(tmp_path, "codex", 1, f"Authorization: Bearer {TOKEN}\n")
    register(attempt, "claude")
    register(attempt, "codex")
    clock = Clock()

    found = probe.diagnose(attempt, tmp_path, clock=clock, sleep=clock.sleep)

    assert found["registration_budget_seconds"] == probe.REGISTRATION_BUDGET_SECONDS
    assert found["harnesses"]["claude"] == {
        "sessions": {"s1": "/home/worker/work"},
        "seconds": 0.0,
        "harness": {"exit": 0, "output": "loaded <attempt>/homes/claude\n"},
    }
    codex = found["harnesses"]["codex"]["harness"]
    assert codex["exit"] == 1
    assert TOKEN not in codex["output"]
    assert codex["output"] == "Authorization: Bearer [REDACTED:github_token]\n"


def test_late_registration_is_observed_within_the_budget(tmp_path):
    attempt = tmp_path / "a1"
    ran(tmp_path, "claude", 0, "")
    ran(tmp_path, "codex", 0, "")
    register(attempt, "claude")
    clock = Clock()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.now == 2:
            register(attempt, "codex")

    found = probe.diagnose(attempt, tmp_path, clock=clock, sleep=sleep)

    assert found["harnesses"]["codex"]["seconds"] == 2.0
    assert found["harnesses"]["codex"]["sessions"] == {"s1": "/home/worker/work"}


def test_missing_hook_registration_is_fatal_and_keeps_diagnostics(tmp_path):
    attempt = tmp_path / "a1"
    ran(tmp_path, "claude", 0, "hello\n")
    ran(tmp_path, "codex", 124, "codex started\n")
    register(attempt, "claude")
    clock = Clock()

    with pytest.raises(SystemExit) as raised:
        probe.diagnose(attempt, tmp_path, clock=clock, sleep=clock.sleep)

    prefix, detail = str(raised.value.code).split(": ", 1)
    assert prefix == "missing hook registration"
    assert json.loads(detail)["harnesses"]["codex"] == {
        "sessions": {},
        "seconds": probe.REGISTRATION_BUDGET_SECONDS,
        "harness": {"exit": 124, "output": "codex started\n"},
    }
    assert clock.now == probe.REGISTRATION_BUDGET_SECONDS
