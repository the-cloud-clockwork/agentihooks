"""The operating system safety nudge: master registry, one nudge round, the hourly job install and its check."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import cli_delegates
from scripts.swarm import safety_nudge


def _proc(tmp_path: Path, *pids: int) -> Path:
    proc = tmp_path / "proc"
    for pid in pids:
        (proc / str(pid)).mkdir(parents=True)
    proc.mkdir(exist_ok=True)
    return proc


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"AGENTIHOOKS_HOME": str(tmp_path / "home"), **extra}


def _master(tmp_path: Path, name: str, pane: str, target: str = "") -> dict[str, str]:
    extra = {"AGENTIHOOKS_TARGET": target} if target else {}
    return _env(tmp_path, AGENTIHOOKS_AGENT_NAME=name, AGENTIHOOKS_SWARM="demo", HERDR_PANE_ID=pane, **extra)


class FakeHerdr:
    def __init__(self, statuses: dict[str, str], typed: dict[str, str] | None = None) -> None:
        self.statuses = statuses
        self.typed = typed or {}
        self.prompts: list[tuple[str, str, str]] = []

    def agent_status(self, agent: safety_nudge.Pane) -> str:
        return self.statuses[agent.pane_id]

    def typed_input(self, agent: safety_nudge.Pane) -> str:
        return self.typed.get(agent.pane_id, "")

    def prompt(self, agent: safety_nudge.Pane, text: str) -> None:
        self.prompts.append((agent.pane_id, agent.harness, text))


class FakeCron:
    def __init__(self, lines: list[str] | None) -> None:
        self.lines = lines

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[0] == safety_nudge.CRON
        if args[1] == "-l":
            if self.lines is None:
                return subprocess.CompletedProcess(args, 1, "", "no table for user")
            return subprocess.CompletedProcess(args, 0, "".join(f"{line}\n" for line in self.lines), "")
        self.lines = str(kwargs["input"]).splitlines()
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.mark.feature("master-safety-nudge")
def test_a_master_in_a_herdr_pane_is_recorded_and_dead_masters_are_dropped(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 11, 22)
    safety_nudge.record(_master(tmp_path, "master@abc123-0001", "p1"), pid=11, now=100, proc=proc)
    (proc / "11").rmdir()
    safety_nudge.record(_master(tmp_path, "master@abc123-0002", "p2", "codex"), pid=22, now=200, proc=proc)

    entries = json.loads((tmp_path / "home" / safety_nudge.REGISTRY).read_text())

    assert entries == {
        "master@abc123-0002": {
            "seat": "master@abc123-0002",
            "slug": "demo",
            "harness": "codex",
            "pane": "p2",
            "pid": 22,
            "last_seen": 200,
        }
    }


@pytest.mark.feature("master-safety-nudge")
def test_a_session_that_is_not_a_master_in_a_pane_is_never_recorded(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 11)
    worker = _master(tmp_path, "ci@abc123-0003", "p1")
    outside = _env(tmp_path, AGENTIHOOKS_AGENT_NAME="master@abc123-0001", AGENTIHOOKS_SWARM="demo")

    assert safety_nudge.record(worker, pid=11, now=1, proc=proc) is None
    assert safety_nudge.record(outside, pid=11, now=1, proc=proc) is None
    assert not (tmp_path / "home" / safety_nudge.REGISTRY).exists()


@pytest.mark.feature("master-safety-nudge")
def test_an_unreadable_registry_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "home" / safety_nudge.REGISTRY
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    assert safety_nudge.load(path) == {}


@pytest.mark.feature("master-safety-nudge")
def test_a_round_nudges_idle_claude_and_codex_masters_and_skips_busy_or_typed_panes(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 1, 2, 3, 4, 5)
    for pid, pane, target in ((1, "p1", ""), (2, "p2", "codex"), (3, "p3", ""), (4, "p4", ""), (5, "p5", "")):
        safety_nudge.record(_master(tmp_path, f"master@abc123-000{pid}", pane, target), pid=pid, now=10, proc=proc)
    (proc / "5").rmdir()
    herdr = FakeHerdr({"p1": "idle", "p2": "done", "p3": "working", "p4": "idle"}, typed={"p4": "half a line"})

    results = safety_nudge.run_round(_env(tmp_path), herdr=herdr, now=3600, proc=proc)

    assert herdr.prompts == [("p1", "claude", safety_nudge.TEXT), ("p2", "codex", safety_nudge.TEXT)]
    assert {r["pane"]: r["outcome"] for r in results} == {
        "p1": "sent",
        "p2": "sent",
        "p3": "skipped-working",
        "p4": "skipped-typed",
    }
    logged = [json.loads(line) for line in (tmp_path / "home" / "logs" / safety_nudge.LOG).read_text().splitlines()]
    assert [(e["pane"], e["outcome"], e["at"]) for e in logged] == [(r["pane"], r["outcome"], 3600) for r in results]
    assert "master@abc123-0005" not in json.loads((tmp_path / "home" / safety_nudge.REGISTRY).read_text())


@pytest.mark.feature("master-safety-nudge")
def test_safety_nudge_text_is_the_operator_words() -> None:
    assert safety_nudge.TEXT == "Safety Nudge: In case the main tick or controller is down"


@pytest.mark.feature("master-safety-nudge")
def test_a_herdr_failure_is_logged_as_failed_and_the_round_goes_on(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 1, 2)
    for pid in (1, 2):
        safety_nudge.record(_master(tmp_path, f"master@abc123-000{pid}", f"p{pid}"), pid=pid, now=10, proc=proc)

    class Broken(FakeHerdr):
        def agent_status(self, agent: safety_nudge.Pane) -> str:
            if agent.pane_id == "p1":
                raise RuntimeError("herdr gone")
            return "idle"

    herdr = Broken({})
    results = safety_nudge.run_round(_env(tmp_path), herdr=herdr, now=20, proc=proc)

    assert [r["outcome"] for r in results] == ["failed-RuntimeError", "sent"]


@pytest.mark.feature("master-safety-nudge")
def test_install_registers_one_hourly_round_and_keeps_other_entries(tmp_path: Path) -> None:
    cron = FakeCron(["5 4 * * * backup", f"0 * * * * old {safety_nudge.MARKER}"])

    line = safety_nudge.install({"PATH": "/usr/bin"}, runner=cron)
    safety_nudge.install({"PATH": "/usr/bin"}, runner=cron)

    assert cron.lines == ["5 4 * * * backup", line]
    assert line.startswith("0 * * * * ")
    assert "safety-nudge round" in line and line.endswith(safety_nudge.MARKER)


@pytest.mark.feature("master-safety-nudge")
def test_install_works_for_a_user_with_no_table(tmp_path: Path) -> None:
    cron = FakeCron(None)

    line = safety_nudge.install({"PATH": "/usr/bin"}, runner=cron)

    assert cron.lines == [line]


@pytest.mark.feature("master-safety-nudge")
def test_check_reports_the_job_and_the_pane_of_this_master(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 1, 2)
    me = _master(tmp_path, "master@abc123-0001", "p1")
    safety_nudge.record(me, pid=1, now=10, proc=proc)
    safety_nudge.record(_master(tmp_path, "master@abc123-0002", "p2"), pid=2, now=10, proc=proc)

    missing = safety_nudge.check(me, runner=FakeCron([]), proc=proc)
    found = safety_nudge.check(me, runner=FakeCron([f"0 * * * * x {safety_nudge.MARKER}"]), proc=proc)

    assert missing["installed"] is False
    assert found["installed"] is True and found["job"].endswith(safety_nudge.MARKER)
    assert [m["pane"] for m in found["masters"]] == ["p1"]


@pytest.mark.feature("master-safety-nudge")
def test_agentihooks_safety_nudge_is_a_delegated_command() -> None:
    assert cli_delegates.delegated_cli(["safety-nudge", "round"]) is safety_nudge.main
