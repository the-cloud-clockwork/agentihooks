import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hooks.context import broadcast
from scripts.swarm import master_fallback, master_launch
from scripts.swarm import runtime as runtime_module
from scripts.swarm import store as store_module
from scripts.swarm.status import status_report
from scripts.swarm.store import MASTER, RedisStore
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks  # noqa: F401

pytestmark = pytest.mark.unit
MINUTE = 60_000
START = 1_000
CRITICAL_AT = START + 30 * MINUTE
REFUSAL = "the workstation hive spawns master work: no hive holds the claude master login for its slot"
REFUSED = "error: the new master could not start: no Claude account has room for a master"
ENV = {master_fallback.SWARMS: "sw"}
DOC: dict[str, Any] = {"tasks": [], "_meta": {"events": []}}


class MasterRefused(FakeRuntime):
    def placement_refusal(self, lane: str, task: dict[str, Any]) -> str:
        return REFUSAL if lane == MASTER else ""


class Alerts:
    def __init__(self) -> None:
        self.raised: list[tuple[str, str]] = []
        self.cleared: list[str] = []

    def create(self, message: str, severity: str = "info", **options: Any) -> str:
        self.raised.append((severity, message))
        return f"alert-{len(self.raised)}"

    def clear(self, message_id: str | None = None, channel: str | None = None) -> int:
        self.cleared.append(message_id or "")
        return 1


class Watched:
    def __init__(self, held: RedisStore, state: Path) -> None:
        self.store, self.state = held, state
        self.herdr: Any = MasterRefused()
        self.ledger, self.alerts = tasks(), Alerts()
        self.forced: list[tuple[str, int]] = []
        self.started: list[str] = []
        self.at = START

    def force(self, slug: str, at: int) -> int:
        self.forced.append((slug, at))
        return 7

    def start(self, slug: str) -> int:
        self.started.append(slug)
        return 5

    def passes(self, minutes: range | list[int]) -> None:
        for minute in minutes:
            self.at = START + minute * MINUTE
            tick("sw", self.store, self.ledger, self.herdr, self.at)
            master_fallback.pass_once(ENV, self.state)

    def report(self) -> dict[str, Any]:
        return status_report(self.store, "sw", DOC, self.herdr)


@pytest.fixture
def watched(store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Watched:  # noqa: F811
    watched = Watched(store, tmp_path / "fallback.json")
    monkeypatch.setattr(runtime_module, "HerdrRuntime", lambda: watched.herdr)
    monkeypatch.setattr(store_module, "connect", lambda: store)
    monkeypatch.setattr(master_fallback, "start", watched.start)
    monkeypatch.setattr(master_fallback, "force", watched.force, raising=False)
    monkeypatch.setattr(master_fallback.time, "time", lambda: watched.at / 1000)
    monkeypatch.setattr(broadcast, "create_broadcast", watched.alerts.create)
    monkeypatch.setattr(broadcast, "clear_broadcasts", watched.alerts.clear)
    return watched


def critical_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [found for found in report["findings"] if found["kind"] == "master critical"]


@pytest.mark.feature("swarm-master-critical")
def test_a_master_placement_refusal_for_thirty_minutes_puts_the_swarm_critical_and_forces_master_up(
    watched: Watched,
) -> None:
    watched.passes(range(30))

    assert masters(watched.store) == []
    assert watched.forced == []
    assert watched.report().get("master_critical", {}).get("critical_at") is None
    assert watched.alerts.raised == []

    watched.passes([30])

    assert masters(watched.store) == []
    assert watched.forced == [("sw", CRITICAL_AT)]
    report = watched.report()
    assert report.get("master_critical", {}).get("since") == START
    assert report.get("master_critical", {}).get("critical_at") == CRITICAL_AT
    assert [found["subject"] for found in critical_findings(report)] == ["sw"]
    assert [severity for severity, _ in watched.alerts.raised] == ["critical"]
    assert "no bound master for 30 minutes" in watched.alerts.raised[0][1]


@pytest.mark.feature("swarm-master-critical")
def test_the_forced_master_up_starts_a_master_the_placement_refusal_cannot_block_and_its_bind_clears_critical(
    watched: Watched,
) -> None:
    watched.passes(range(31))
    assert watched.forced == [("sw", CRITICAL_AT)]

    launched = master_launch.up(
        watched.store, "sw", watched.herdr, START + 31 * MINUTE, master_launch.REPLACE, input, print
    )

    assert watched.herdr.placement_refusal(MASTER, {}) == REFUSAL
    assert [agent.name for agent in masters(watched.store)] == [launched.master]
    assert launched.seat == f"{MASTER}@sw"

    watched.passes([32])

    report = watched.report()
    assert report.get("master_critical") == {}
    assert critical_findings(report) == []
    assert watched.alerts.cleared == ["alert-1"]
    assert [severity for severity, _ in watched.alerts.raised] == ["critical", "resolved"]
    assert launched.master in watched.alerts.raised[-1][1]


@pytest.mark.feature("swarm-master-critical")
def test_a_refused_forced_start_is_recorded_with_its_reason_and_time_and_raises_the_alert_again(
    watched: Watched, monkeypatch: pytest.MonkeyPatch
) -> None:
    watched.passes(range(31))
    ran: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        ran.append((command, options))
        return subprocess.CompletedProcess(command, 1, "", f"{REFUSED}\n")

    monkeypatch.setattr(
        master_fallback,
        "subprocess",
        SimpleNamespace(
            run=run,
            TimeoutExpired=subprocess.TimeoutExpired,
            SubprocessError=subprocess.SubprocessError,
            DEVNULL=subprocess.DEVNULL,
        ),
    )
    monkeypatch.setattr(master_fallback, "run", lambda environ: pytest.fail("force fell through to the fallback loop"))
    watched.at = START + 31 * MINUTE

    master_fallback.main(["force", "sw", str(CRITICAL_AT)])

    assert [command[-5:] for command, _ in ran] == [["swarm", "sw", "master", "up", "--replace"]]
    attempts = watched.report().get("master_critical", {}).get("attempts")
    assert attempts == [{"at": CRITICAL_AT, "outcome": "failed", "reason": REFUSED, "ended_at": START + 31 * MINUTE}]
    assert [severity for severity, _ in watched.alerts.raised] == ["critical", "critical"]
    assert REFUSED in watched.alerts.raised[-1][1]
    assert any(REFUSED in line for line in critical_findings(watched.report())[0]["evidence"])
