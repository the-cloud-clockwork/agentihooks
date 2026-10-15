import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "swarm_node"))
SPEC = importlib.util.spec_from_file_location("profile_probe", ROOT / "tests" / "swarm_node" / "profile_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
TOKEN = "ghp_" + "a1" * 18
LAUNCH_NS = 1_700_000_000_000_000_000


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
    (tmp / f"{target}-run.started").write_text(f"{LAUNCH_NS}\n", encoding="utf-8")


def register(attempt: Path, target: str, after_launch_ns: int = 1_500_000_000) -> None:
    home = attempt / "homes" / target / ".agentihooks"
    home.mkdir(parents=True)
    registry = home / "active-sessions.json"
    registry.write_text(json.dumps({"s1": {"cwd": "/home/worker/work"}}), encoding="utf-8")
    os.utime(registry, ns=(LAUNCH_NS + after_launch_ns, LAUNCH_NS + after_launch_ns))


def test_diagnostics_keep_exit_status_redacted_output_and_registration_time(tmp_path):
    attempt = tmp_path / "a1"
    ran(tmp_path, "claude", 0, f"loaded {attempt}/homes/claude\n")
    ran(tmp_path, "codex", 1, f"Authorization: Bearer {TOKEN}\n")
    register(attempt, "claude")
    register(attempt, "codex", 2_250_000_000)
    clock = Clock()

    found = probe.diagnose(attempt, tmp_path, clock=clock, sleep=clock.sleep)

    assert found["registration_budget_seconds"] == probe.REGISTRATION_BUDGET_SECONDS
    assert found["waited_seconds"] == 0.0
    assert found["harnesses"]["claude"] == {
        "sessions": {"s1": "/home/worker/work"},
        "registry_written_after_launch_seconds": 1.5,
        "harness": {"exit": 0, "output": "loaded <attempt>/homes/claude\n"},
    }
    codex = found["harnesses"]["codex"]
    assert codex["registry_written_after_launch_seconds"] == 2.25
    assert codex["harness"]["exit"] == 1
    assert TOKEN not in codex["harness"]["output"]
    assert codex["harness"]["output"] == "Authorization: Bearer [REDACTED:github_token]\n"


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

    assert found["waited_seconds"] == 2.0
    assert found["harnesses"]["codex"]["sessions"] == {"s1": "/home/worker/work"}


def test_missing_hook_registration_is_fatal_and_keeps_diagnostics(tmp_path, capsys):
    attempt = tmp_path / "a1"
    ran(tmp_path, "claude", 0, "hello\n")
    ran(tmp_path, "codex", 124, "codex started\n")
    register(attempt, "claude")
    clock = Clock()

    with pytest.raises(SystemExit) as raised:
        probe.diagnose(attempt, tmp_path, clock=clock, sleep=clock.sleep)

    codex = {
        "sessions": {},
        "registry_written_after_launch_seconds": None,
        "harness": {"exit": 124, "output": "codex started\n"},
    }
    prefix, detail = str(raised.value.code).split(": ", 1)
    assert prefix == "missing hook registration"
    printed = json.loads(capsys.readouterr().out)["missing_hook_registration"]
    assert printed == json.loads(detail)
    missing = printed["harnesses"]["codex"]
    assert missing.pop("error").startswith("FileNotFoundError: ")
    assert missing == codex
    assert "error" not in printed["harnesses"]["claude"]
    assert printed["waited_seconds"] == probe.REGISTRATION_BUDGET_SECONDS
    assert clock.now == probe.REGISTRATION_BUDGET_SECONDS


def test_one_budget_covers_both_harnesses(tmp_path):
    attempt = tmp_path / "a1"
    ran(tmp_path, "claude", 124, "")
    ran(tmp_path, "codex", 124, "")
    clock = Clock()

    with pytest.raises(SystemExit):
        probe.diagnose(attempt, tmp_path, clock=clock, sleep=clock.sleep)

    assert clock.now == probe.REGISTRATION_BUDGET_SECONDS
