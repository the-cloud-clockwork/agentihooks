import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

import scripts.swarm.cli  # noqa: F401
from hooks.context import broadcast
from scripts.hub import lifecycle  # noqa: F401
from scripts.swarm import idle, ledger_client, master_critical, master_fallback, master_launch
from scripts.swarm import runtime as runtime_module
from scripts.swarm import store as store_module
from scripts.swarm.status import status_report
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import tick
from tests.swarm.client_class import client_class
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks  # noqa: F401

pytestmark = pytest.mark.unit
MINUTE = 60_000
START = 1_000
CRITICAL_AT = START + 30 * MINUTE
REFUSAL = "the workstation hive spawns master work: no hive holds the claude master login for its slot"
REFUSED = "error: the new master could not start: no Claude account has room for a master"
ENV = {master_fallback.SWARMS: "sw"}
DOC: dict[str, Any] = {"tasks": [], "_meta": {"events": []}}
RAISED = "raised the seat alert of swarm sw"


class MasterRefused(FakeRuntime):
    def placement_refusal(self, lane: str, task: dict[str, Any]) -> str:
        return REFUSAL if lane == MASTER else ""


class Alerts:
    def __init__(self) -> None:
        self.raised: list[tuple[str, str]] = []
        self.options: list[dict[str, Any]] = []
        self.cleared: list[str] = []
        self.ids = True

    def create(self, message: str, severity: str = "info", **options: Any) -> str | None:
        self.raised.append((severity, message))
        self.options.append(options)
        return f"alert-{len(self.raised)}" if self.ids else None

    def clear(self, message_id: str | None = None, channel: str | None = None) -> int:
        self.cleared.append(message_id or "")
        return 1


class Ledger:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.held: list[dict[str, Any]] = []
        self.raised: list[str] = []
        self.closed: list[tuple[str, str]] = []
        self.broken = False
        self.refuses_raise = False

    def tasks(self, slug: str) -> list[dict[str, Any]]:
        assert slug == "sw"
        if self.broken:
            raise SwarmError("the ledger is unreachable")
        return self.rows

    def raise_alert(self, slug: str, source: str, text: str) -> None:
        assert (slug, source) == ("sw", "seat")
        if self.broken or self.refuses_raise:
            raise OSError("the ledger is unreachable")
        self.raised.append(text)
        if not any(alert["source"] == source and alert.get("state") != "done" for alert in self.held):
            self.held.append({"id": f"seat-{len(self.held) + 1}", "source": source, "state": "open", "text": text})

    def alerts(self, slug: str) -> list[dict[str, Any]]:
        assert slug == "sw"
        if self.broken:
            raise OSError("the ledger is unreachable")
        return [dict(alert) for alert in self.held]

    def close_alert(self, slug: str, alert_id: str, outcome: str) -> None:
        assert slug == "sw"
        self.closed.append((alert_id, outcome))
        next(alert for alert in self.held if alert["id"] == alert_id)["state"] = "done"


class LockWatching(Ledger):
    def __init__(self, swarm_store: RedisStore, rows: list[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.store = swarm_store
        self.calls: list[tuple[str, int]] = []

    def seen(self, call: str) -> None:
        self.calls.append((call, self.store.redis.exists(self.store.key("sw", master_critical.LOCK))))

    def tasks(self, slug: str) -> list[dict[str, Any]]:
        self.seen("tasks")
        return super().tasks(slug)

    def alerts(self, slug: str) -> list[dict[str, Any]]:
        self.seen("alerts")
        return super().alerts(slug)

    def raise_alert(self, slug: str, source: str, text: str) -> None:
        self.seen("raise_alert")
        super().raise_alert(slug, source, text)

    def close_alert(self, slug: str, alert_id: str, outcome: str) -> None:
        self.seen("close_alert")
        super().close_alert(slug, alert_id, outcome)


class Watched:
    def __init__(self, held: RedisStore, state: Path) -> None:
        self.store, self.state = held, state
        self.herdr: Any = MasterRefused()
        self.ledger, self.alerts, self.seats = tasks(("t1", "eng")), Alerts(), Ledger()
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
    store.update("sw", max_eng=0, max_ci=0)
    watched = Watched(store, tmp_path / "fallback.json")
    monkeypatch.setattr(runtime_module, "HerdrRuntime", lambda: watched.herdr)
    monkeypatch.setattr(store_module, "connect", lambda: store)
    monkeypatch.setattr(ledger_client, "LedgerClient", client_class(watched.seats))
    monkeypatch.setattr(master_fallback, "start", watched.start)
    monkeypatch.setattr(master_fallback, "force", watched.force)
    monkeypatch.setattr(master_fallback.time, "time", lambda: watched.at / 1000)
    monkeypatch.setattr(broadcast, "create_broadcast", watched.alerts.create)
    monkeypatch.setattr(broadcast, "clear_broadcasts", watched.alerts.clear)
    return watched


def test_a_fresh_worker_whose_first_pass_runs_under_the_patched_ledger_client_loads_the_lazy_imports() -> None:
    probe = (
        "from tests.swarm import test_master_critical\n"
        "from scripts.swarm import ledger_client\n"
        "ledger_client.LedgerClient = lambda: None\n"
        "from scripts.hub.reconciler import admitted_agents, legacy_agents\n"
        "from scripts.swarm.cli import _pass_tools\n"
    )

    loaded = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert loaded.returncode == 0, loaded.stderr


def never_asked(prompt: str) -> str:
    raise AssertionError(f"master up asked {prompt!r}")


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
    assert watched.seats.raised == [master_critical.CRITICAL.format(slug="sw", minutes=30)]


@pytest.mark.feature("swarm-master-critical")
def test_the_forced_master_up_starts_a_master_the_placement_refusal_cannot_block_and_its_bind_clears_critical(
    watched: Watched,
) -> None:
    watched.passes(range(31))
    assert watched.forced == [("sw", CRITICAL_AT)]

    launched = master_launch.up(
        watched.store, "sw", watched.herdr, START + 31 * MINUTE, master_launch.REPLACE, never_asked, print
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
    assert watched.seats.closed == [("seat-1", watched.alerts.raised[-1][1])]


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
    assert attempts == [
        {"at": CRITICAL_AT, "outcome": "failed", "reason": REFUSED, "ended_at": START + 31 * MINUTE, "code": 1}
    ]
    assert [severity for severity, _ in watched.alerts.raised] == ["critical", "critical"]
    assert REFUSED in watched.alerts.raised[-1][1]
    assert any(REFUSED in line for line in critical_findings(watched.report())[0]["evidence"])


class Forced:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, slug: str, at: int) -> int:
        self.calls.append((slug, at))
        return 3


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> Alerts:
    alerts = Alerts()
    monkeypatch.setattr(broadcast, "create_broadcast", alerts.create)
    monkeypatch.setattr(broadcast, "clear_broadcasts", alerts.clear)
    return alerts


def watch(
    held: RedisStore,
    at: int,
    forced: Callable[[str, int], int],
    live: set[str] | None = None,
    ledger: Ledger | None = None,
) -> list[str]:
    return master_critical.watch(held, ledger or Ledger(), "sw", live or set(), at, {}, forced)


def critical(held: RedisStore, forced: Forced, ledger: Ledger | None = None) -> int:
    watch(held, START, forced, ledger=ledger)
    watch(held, CRITICAL_AT, forced, ledger=ledger)
    return CRITICAL_AT


def front_door(held: RedisStore, state: str = "working", seat: str = f"{MASTER}@sw", started_at: int = 0) -> str:
    name = "master@a1b2c3-0009"
    held.put_agent("sw", AgentRecord(name, MASTER, MASTER, state=state, seat=seat, started_at=started_at))
    return name


def test_the_critical_minutes_default_to_thirty_and_follow_the_env() -> None:
    assert master_critical.minutes({}) == 30
    assert master_critical.minutes({master_critical.MINUTES: ""}) == 30
    assert master_critical.minutes({master_critical.MINUTES: "2"}) == 2
    assert master_critical.MINUTES == "AGENTIHOOKS_MASTER_CRITICAL_MINUTES"


@pytest.mark.feature("swarm-master-critical")
def test_the_swarm_turns_critical_at_its_threshold_and_forces_master_up(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    forced, env, ledger = Forced(), {master_critical.MINUTES: "2"}, Ledger()
    text = master_critical.CRITICAL.format(slug="sw", minutes=2)

    assert master_critical.watch(store, ledger, "sw", set(), START, env, forced) == []
    assert master_critical.watch(store, ledger, "sw", set(), START + 2 * MINUTE - 1, env, forced) == []
    lines = master_critical.watch(store, ledger, "sw", set(), START + 2 * MINUTE, env, forced)

    assert lines == [
        "swarm sw is critical: no bound front door master since 1970-01-01 00:00Z",
        "forced master up of swarm sw: runner 3",
        RAISED,
    ]
    assert forced.calls == [("sw", START + 2 * MINUTE)]
    assert alerts.raised == [("critical", text)]
    assert alerts.options == [{"source": "swarm"}]
    assert ledger.raised == [text]
    assert master_critical.read(store, "sw") == {
        "since": START,
        "critical_at": START + 2 * MINUTE,
        "alert": "alert-1",
        "attempts": [{"at": START + 2 * MINUTE, "outcome": "", "reason": ""}],
    }


@pytest.mark.feature("swarm-master-critical")
def test_the_critical_alert_counts_the_minutes_since_the_outage_began(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    began = 1_794_880_748_486
    watch(store, began, Forced())
    watch(store, began + 30 * MINUTE, Forced())

    assert alerts.raised == [("critical", master_critical.CRITICAL.format(slug="sw", minutes=30))]


@pytest.mark.feature("swarm-master-critical")
def test_a_failed_forced_start_is_retried_once_per_retry_window(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    forced = Forced()
    at = critical(store, forced)
    master_critical.ended(store, "sw", at, 1, "boom\n", at + MINUTE)

    assert watch(store, at + master_critical.RETRY_MS - 1, forced) == [RAISED]
    assert watch(store, at + master_critical.RETRY_MS, forced) == ["forced master up of swarm sw: runner 3", RAISED]
    assert forced.calls == [("sw", at), ("sw", at + master_critical.RETRY_MS)]
    assert [row["outcome"] for row in master_critical.read(store, "sw")["attempts"]] == ["failed", ""]


@pytest.mark.parametrize(
    "code, reason",
    [(0, master_critical.NO_BIND), (None, master_critical.NO_OUTCOME)],
)
@pytest.mark.feature("swarm-master-critical")
def test_a_forced_start_with_no_bound_master_by_the_retry_counts_as_failed(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    code: int | None,
    reason: str,
) -> None:
    forced = Forced()
    at = critical(store, forced)
    if code is not None:
        master_critical.ended(store, "sw", at, code, '{"master": "master@a1b2c3-0002"}\n', at + MINUTE)
    retry = at + master_critical.RETRY_MS

    assert watch(store, retry, forced) == [
        f"forced master up of swarm sw at 1970-01-01 00:30Z failed: {reason}",
        "forced master up of swarm sw: runner 3",
        RAISED,
    ]
    assert master_critical.read(store, "sw")["attempts"] == [
        {"at": at, "outcome": "failed", "reason": reason, "ended_at": retry, **({} if code is None else {"code": 0})},
        {"at": retry, "outcome": "", "reason": ""},
    ]
    assert alerts.raised[-1] == (
        "critical",
        master_critical.STILL.format(slug="sw", at="1970-01-01 00:30Z", reason=reason),
    )
    assert alerts.cleared == ["alert-1"]


@pytest.mark.feature("swarm-master-critical")
def test_each_failed_forced_start_raises_the_ledger_seat_alert_again(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    ledger = Ledger()
    at = critical(store, Forced(), ledger)
    master_critical.ended(store, "sw", at, 1, "boom", at + MINUTE)
    ledger.held[0]["state"] = "done"

    watch(store, at + master_critical.RETRY_MS, Forced(), ledger=ledger)

    assert ledger.raised == [
        master_critical.CRITICAL.format(slug="sw", minutes=30),
        master_critical.SEAT_STILL.format(slug="sw", reason="boom"),
    ]
    assert [alert["id"] for alert in ledger.held] == ["seat-1", "seat-2"]
    assert ledger.closed == []


@pytest.mark.feature("swarm-master-critical")
def test_a_failed_start_replaces_the_open_seat_alert_with_one_naming_its_reason(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    ledger = Ledger()
    at = critical(store, Forced(), ledger)
    master_critical.ended(store, "sw", at, 1, "boom", at + MINUTE)
    still = master_critical.SEAT_STILL.format(slug="sw", reason="boom")

    assert watch(store, at + 2 * MINUTE, Forced(), ledger=ledger) == [
        "closed the seat alert seat-1 of swarm sw",
        RAISED,
    ]
    assert watch(store, at + master_critical.RETRY_MS, Forced(), ledger=ledger) == [
        "forced master up of swarm sw: runner 3"
    ]
    assert ledger.closed == [("seat-1", master_critical.REPLACED.format(text=still))]
    assert [(alert["id"], alert["state"], alert["text"]) for alert in ledger.held] == [
        ("seat-1", "done", master_critical.CRITICAL.format(slug="sw", minutes=30)),
        ("seat-2", "open", still),
    ]


@pytest.mark.feature("swarm-master-critical")
def test_a_repeated_failure_reason_keeps_its_seat_alert_and_a_new_reason_replaces_it(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    ledger, retry = Ledger(), master_critical.RETRY_MS
    at = critical(store, Forced(), ledger)
    for step, reason in enumerate(("boom", "boom")):
        master_critical.ended(store, "sw", at + step * retry, 1, reason, at + step * retry + MINUTE)
        watch(store, at + (step + 1) * retry, Forced(), ledger=ledger)

    assert [(alert["id"], alert["state"]) for alert in ledger.held] == [("seat-1", "done"), ("seat-2", "open")]
    assert len(ledger.raised) == 2

    master_critical.ended(store, "sw", at + 2 * retry, 1, "no room", at + 2 * retry + MINUTE)
    lines = watch(store, at + 3 * retry, Forced(), ledger=ledger)

    still = master_critical.SEAT_STILL.format(slug="sw", reason="no room")
    assert lines[-2:] == ["closed the seat alert seat-2 of swarm sw", RAISED]
    assert [(alert["id"], alert["state"], alert["text"]) for alert in ledger.held][-1] == ("seat-3", "open", still)


@pytest.mark.feature("swarm-master-critical")
def test_a_refused_raise_after_a_replaced_seat_alert_names_the_close_and_the_next_pass_raises_it(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    ledger = Ledger()
    at = critical(store, Forced(), ledger)
    master_critical.ended(store, "sw", at, 1, "boom", at + MINUTE)
    ledger.refuses_raise = True

    assert watch(store, at + 2 * MINUTE, Forced(), ledger=ledger) == [
        "closed the seat alert seat-1 of swarm sw",
        "the ledger did not take the seat alert of swarm sw: OSError: the ledger is unreachable",
    ]

    ledger.refuses_raise = False
    assert watch(store, at + 3 * MINUTE, Forced(), ledger=ledger) == [RAISED]
    assert ledger.held[-1] == {
        "id": "seat-2",
        "source": "seat",
        "state": "open",
        "text": master_critical.SEAT_STILL.format(slug="sw", reason="boom"),
    }


@pytest.mark.feature("swarm-master-critical")
def test_no_ledger_call_runs_while_the_record_lock_is_held(store: RedisStore, alerts: Alerts) -> None:  # noqa: F811
    store.redis.set(store.key("sw", "master-retired-tasks"), '["t1"]')
    ledger = LockWatching(store, [{"id": "t1"}, {"id": "t2"}])
    at = critical(store, Forced(), ledger)
    master_critical.ended(store, "sw", at, 1, "boom", at + MINUTE)
    watch(store, at + master_critical.RETRY_MS, Forced(), ledger=ledger)
    name = front_door(store)

    watch(store, at + master_critical.RETRY_MS + MINUTE, Forced(), {name}, ledger)

    assert {call for call, _ in ledger.calls} == {"tasks", "alerts", "raise_alert", "close_alert"}
    assert [call for call, held in ledger.calls if held] == []
    assert master_critical.read(store, "sw") == {}


@pytest.mark.feature("swarm-master-critical")
def test_a_ledger_that_refuses_the_seat_alert_is_named_and_the_watch_goes_on(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    ledger, forced = Ledger(), Forced()
    ledger.broken = True
    watch(store, START, forced, ledger=ledger)

    lines = watch(store, CRITICAL_AT, forced, ledger=ledger)

    assert lines[-1] == "the ledger did not take the seat alert of swarm sw: OSError: the ledger is unreachable"
    assert forced.calls == [("sw", CRITICAL_AT)]


@pytest.mark.feature("swarm-master-critical")
def test_a_runner_that_cannot_start_fails_its_forced_start_in_the_same_pass(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    def broken(slug: str, at: int) -> int:
        raise FileNotFoundError(2, "No such file or directory")

    reason, ledger = "FileNotFoundError: [Errno 2] No such file or directory", Ledger()
    watch(store, START, Forced(), ledger=ledger)

    lines = watch(store, CRITICAL_AT, broken, ledger=ledger)

    assert lines[1:] == [f"forced master up of swarm sw at 1970-01-01 00:30Z failed: {reason}", RAISED]
    assert ledger.raised == [master_critical.SEAT_STILL.format(slug="sw", reason=reason)]
    assert master_critical.read(store, "sw")["attempts"] == [
        {"at": CRITICAL_AT, "outcome": "failed", "reason": reason, "ended_at": CRITICAL_AT}
    ]
    assert alerts.raised[-1] == (
        "critical",
        master_critical.STILL.format(slug="sw", at="1970-01-01 00:30Z", reason=reason),
    )


@pytest.mark.feature("swarm-master-critical")
def test_a_forced_start_is_recorded_before_its_runner_starts(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    seen: list[list[dict[str, Any]]] = []

    def runner(slug: str, at: int) -> int:
        seen.append(master_critical.read(store, slug)["attempts"])
        return 9

    watch(store, START, Forced())
    watch(store, CRITICAL_AT, runner)

    assert seen == [[{"at": CRITICAL_AT, "outcome": "", "reason": ""}]]


@pytest.mark.feature("swarm-master-critical")
def test_a_front_door_master_still_starting_holds_the_forced_start_until_its_deadline(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    forced = Forced()
    watch(store, START, forced)
    front_door(store, state="starting", started_at=CRITICAL_AT - MINUTE)

    assert watch(store, CRITICAL_AT, forced) == [
        "swarm sw is critical: no bound front door master since 1970-01-01 00:00Z",
        RAISED,
    ]
    assert forced.calls == []
    assert master_critical.findings(store, "sw", CRITICAL_AT)[0].evidence[1] == "No forced master up has run yet"
    watch(store, CRITICAL_AT + MINUTE, forced)
    assert forced.calls == [("sw", CRITICAL_AT + MINUTE)]


@pytest.mark.parametrize(
    "state, seat",
    [("finished", f"{MASTER}@sw"), ("starting", f"{MASTER}-2@sw"), ("starting", "engineer@sw")],
)
@pytest.mark.feature("swarm-master-critical")
def test_only_a_front_door_master_that_is_not_finished_holds_the_forced_start(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    state: str,
    seat: str,
) -> None:
    forced = Forced()
    watch(store, START, forced)
    front_door(store, state=state, seat=seat, started_at=CRITICAL_AT)

    watch(store, CRITICAL_AT, forced)

    assert forced.calls == [("sw", CRITICAL_AT)]


@pytest.mark.parametrize(
    "state, seat, live",
    [("working", f"{MASTER}-2@sw", True), ("starting", f"{MASTER}@sw", True), ("working", f"{MASTER}@sw", False)],
)
@pytest.mark.feature("swarm-master-critical")
def test_only_a_live_working_master_on_the_front_door_seat_ends_critical_mode(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    state: str,
    seat: str,
    live: bool,
) -> None:
    at = critical(store, Forced())
    name = front_door(store, state=state, seat=seat)

    assert master_critical.bound(store, "sw", {name} if live else set()) == ""
    watch(store, at + MINUTE, Forced(), {name} if live else set())

    assert master_critical.read(store, "sw")["critical_at"] == at
    assert alerts.cleared == []


@pytest.mark.parametrize(
    "live, beat, holds",
    [(True, CRITICAL_AT - MINUTE, True), (True, CRITICAL_AT - 2 * MINUTE, False), (False, CRITICAL_AT, False)],
)
@pytest.mark.feature("swarm-master-critical")
def test_a_starting_front_door_master_whose_hooks_beat_since_its_launch_holds_the_seat(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    live: bool,
    beat: int,
    holds: bool,
) -> None:
    at = critical(store, Forced())
    name = front_door(store, state="starting", started_at=CRITICAL_AT - MINUTE)
    idle.beat(store.redis, "sw", name, "working", beat)
    names = {name} if live else set()

    assert master_critical.bound(store, "sw", names) == (name if holds else "")
    watch(store, at + 60 * MINUTE, Forced(), names)
    assert master_critical.active(store, "sw") is not holds


@pytest.mark.parametrize("seat", [f"{MASTER}@sw", ""])
@pytest.mark.feature("swarm-master-critical")
def test_a_live_working_front_door_master_ends_critical_mode(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    seat: str,
) -> None:
    ledger = Ledger()
    at = critical(store, Forced(), ledger)
    ledger.held += [{"id": "seat-0", "source": "seat", "state": "done"}, {"id": "spawn-1", "source": "spawn"}]
    name = front_door(store, seat=seat)
    cleared = f"Swarm sw is out of critical mode: master {name} bound the front door seat."

    assert watch(store, at + MINUTE, Forced(), {name}, ledger) == [cleared, "closed the seat alert seat-1 of swarm sw"]
    assert master_critical.read(store, "sw") == {}
    assert alerts.cleared == ["alert-1"]
    assert alerts.raised[-1] == ("resolved", cleared)
    assert ledger.closed == [("seat-1", cleared)]


@pytest.mark.feature("swarm-master-critical")
def test_a_ledger_that_cannot_close_the_seat_alert_is_named_and_critical_mode_still_ends(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    at = critical(store, Forced())
    name, ledger = front_door(store), Ledger()
    ledger.broken = True

    lines = watch(store, at + MINUTE, Forced(), {name}, ledger)

    assert lines[1] == "the ledger did not close the seat alert of swarm sw: OSError: the ledger is unreachable"
    assert master_critical.read(store, "sw") == {}


@pytest.mark.feature("swarm-master-critical")
def test_a_master_binding_before_the_threshold_ends_the_outage_without_an_alert(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    watch(store, START, Forced())
    assert master_critical.read(store, "sw") == {"since": START}
    name = front_door(store)

    assert watch(store, START + MINUTE, Forced(), {name}) == []
    assert master_critical.read(store, "sw") == {}
    assert alerts.raised == []


@pytest.mark.parametrize("state", ["stopped", "stopping", "paused", "drained"])
@pytest.mark.feature("swarm-master-critical")
def test_a_swarm_that_is_not_running_leaves_critical_mode_and_is_never_forced(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    state: str,
) -> None:
    forced, ledger = Forced(), Ledger()
    at = critical(store, forced, ledger)
    store.update("sw", state=state)
    cleared = "Swarm sw is out of critical mode: the swarm is not running or sleeps with no work."

    assert watch(store, at + master_critical.RETRY_MS, forced, ledger=ledger) == [
        cleared,
        "closed the seat alert seat-1 of swarm sw",
    ]
    assert forced.calls == [("sw", at)]
    assert master_critical.read(store, "sw") == {}
    assert alerts.raised[-1] == ("resolved", cleared)


@pytest.mark.feature("swarm-master-critical")
def test_a_sleeping_swarm_never_turns_critical(store: RedisStore, alerts: Alerts) -> None:  # noqa: F811
    forced = Forced()
    store.redis.set(store.key("sw", "master-retired-tasks"), '["t1"]')

    for minute in (0, 30, 60):
        assert watch(store, START + minute * MINUTE, forced, ledger=Ledger([{"id": "t1"}])) == []

    assert forced.calls == []
    assert master_critical.read(store, "sw") == {}


@pytest.mark.feature("swarm-master-critical")
def test_a_retired_swarm_with_new_work_counts_its_outage_and_keeps_its_retired_record(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    store.redis.set(store.key("sw", "master-retired-tasks"), '["t1"]')
    ledger, forced = Ledger([{"id": "t1"}, {"id": "t2"}]), Forced()

    watch(store, START, forced, ledger=ledger)
    watch(store, CRITICAL_AT, forced, ledger=ledger)

    assert forced.calls == [("sw", CRITICAL_AT)]
    assert json.loads(store.redis.get(store.key("sw", "master-retired-tasks"))) == ["t1"]


@pytest.mark.feature("swarm-master-critical")
def test_a_retired_swarm_whose_ledger_cannot_be_read_stays_awake(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    store.redis.set(store.key("sw", "master-retired-tasks"), "[]")
    ledger, forced = Ledger(), Forced()
    ledger.broken = True

    watch(store, START, forced, ledger=ledger)
    watch(store, CRITICAL_AT, forced, ledger=ledger)

    assert forced.calls == [("sw", CRITICAL_AT)]


@pytest.mark.feature("swarm-master-critical")
def test_the_record_keeps_the_last_ten_forced_starts(store: RedisStore, alerts: Alerts) -> None:  # noqa: F811
    forced = Forced()
    at = critical(store, forced)
    for retry in range(1, 11):
        master_critical.ended(store, "sw", at + (retry - 1) * master_critical.RETRY_MS, 1, "boom", at)
        watch(store, at + retry * master_critical.RETRY_MS, forced)

    attempts = master_critical.read(store, "sw")["attempts"]
    assert [row["at"] for row in attempts] == [at + retry * master_critical.RETRY_MS for retry in range(1, 11)]


@pytest.mark.feature("swarm-master-critical")
def test_the_pass_and_the_runner_each_hold_the_record_lock(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taken: list[tuple[str, int, int]] = []
    lock = store.redis.lock

    def recorded(name: str, timeout: int, blocking_timeout: int) -> Any:
        taken.append((name, timeout, blocking_timeout))
        return lock(name, timeout=timeout, blocking_timeout=blocking_timeout)

    monkeypatch.setattr(store.redis, "lock", recorded)
    at = critical(store, Forced())
    master_critical.ended(store, "sw", at, 0, "", at + MINUTE)

    assert taken == [(store.key("sw", "master-critical-lock"), 20, 30)] * 3


@pytest.mark.feature("swarm-master-critical")
def test_a_held_record_lock_keeps_the_pass_from_writing(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redis.exceptions import LockError

    at = critical(store, Forced())
    monkeypatch.setattr(master_critical, "WAIT_S", 0.05)
    held = store.redis.lock(store.key("sw", master_critical.LOCK), timeout=5)
    assert held.acquire(blocking=False)

    with pytest.raises(LockError):
        watch(store, at + master_critical.RETRY_MS, Forced())

    held.release()
    assert [row["at"] for row in master_critical.read(store, "sw")["attempts"]] == [at]


@pytest.mark.parametrize(
    "code, output, outcome, reason",
    [
        (0, '{"master": "master@a1b2c3-0002"}\n', "started", ""),
        (2, "", "failed", "master up exited with status 2"),
        (1, "starting\n\n  error: no room  \n\n", "failed", "error: no room"),
    ],
)
@pytest.mark.feature("swarm-master-critical")
def test_a_forced_start_ending_records_its_outcome(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    code: int,
    output: str,
    outcome: str,
    reason: str,
) -> None:
    at = critical(store, Forced())

    line = master_critical.ended(store, "sw", at, code, output, at + MINUTE)

    [attempt] = master_critical.read(store, "sw")["attempts"]
    assert attempt == {"at": at, "outcome": outcome, "reason": reason, "ended_at": at + MINUTE, "code": code}
    if code:
        assert line == f"forced master up of swarm sw at 1970-01-01 00:30Z failed: {reason}"
        assert alerts.raised[-1][1] == master_critical.STILL.format(slug="sw", at="1970-01-01 00:30Z", reason=reason)
    else:
        assert line == "forced master up of swarm sw at 1970-01-01 00:30Z started a master"
        assert len(alerts.raised) == 1
    assert master_critical.findings(store, "sw", at + MINUTE)[0].evidence[1] == (
        f"Last forced master up at 1970-01-01 00:30Z: {outcome}" + (f": {reason}" if reason else "")
    )


@pytest.mark.feature("swarm-master-critical")
def test_a_late_runner_never_overwrites_a_recorded_outcome(store: RedisStore, alerts: Alerts) -> None:  # noqa: F811
    at = critical(store, Forced())
    retry = at + master_critical.RETRY_MS
    watch(store, retry, Forced())

    line = master_critical.ended(store, "sw", at, 0, "", retry + MINUTE)

    assert line == "forced master up of swarm sw at 1970-01-01 00:30Z ended after it was recorded failed"
    assert master_critical.read(store, "sw")["attempts"][0] == {
        "at": at,
        "outcome": "failed",
        "reason": master_critical.NO_OUTCOME,
        "ended_at": retry,
    }


def test_a_forced_start_ending_after_its_critical_state_cleared_writes_nothing(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    assert master_critical.ended(store, "sw", CRITICAL_AT, 1, "boom", CRITICAL_AT + MINUTE) == (
        "forced master up of swarm sw at 1970-01-01 00:30Z ended after its critical state cleared"
    )
    assert master_critical.read(store, "sw") == {}
    assert alerts.raised == []


def test_a_broadcast_the_host_did_not_take_leaves_nothing_to_clear(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    alerts.ids = False
    at = critical(store, Forced())
    master_critical.ended(store, "sw", at, 1, "boom", at + MINUTE)

    assert master_critical.read(store, "sw")["alert"] is None
    assert alerts.cleared == []


@pytest.mark.feature("swarm-master-critical")
def test_the_critical_finding_names_the_outage_and_the_last_forced_start(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    assert master_critical.findings(store, "sw", START) == []
    watch(store, START, Forced())
    assert master_critical.findings(store, "sw", START + MINUTE) == []
    at = critical(store, Forced())

    [found] = master_critical.findings(store, "sw", at + 5 * MINUTE)

    assert (found.kind, found.subject, found.measure) == ("master critical", "sw", 35)
    assert (
        found.summary == "No master has bound the front door seat for 35 minutes; the master fallback forces master up"
    )
    assert found.evidence == (
        "No bound master since 1970-01-01 00:00Z, critical since 1970-01-01 00:30Z",
        "Last forced master up at 1970-01-01 00:30Z: running",
    )
    assert found.threshold == master_critical.THRESHOLD


def test_finding_times_read_in_utc_on_a_host_in_another_zone(store: RedisStore, alerts: Alerts) -> None:  # noqa: F811
    try:
        with mock.patch.dict(os.environ, {"TZ": "JST-9"}):
            time.tzset()
            critical(store, Forced())
            [found] = master_critical.findings(store, "sw", CRITICAL_AT)
    finally:
        time.tzset()

    assert found.evidence[0] == "No bound master since 1970-01-01 00:00Z, critical since 1970-01-01 00:30Z"


@pytest.mark.parametrize(
    "at, error, summary, clock",
    [
        (CRITICAL_AT, "RedisError: down", "The master fallback watch of this swarm failed: RedisError: down", "00:30"),
        (
            CRITICAL_AT - 5 * MINUTE,
            "",
            "The master fallback watch of this swarm last ran 5 minutes ago",
            "00:25",
        ),
    ],
)
@pytest.mark.feature("swarm-master-critical")
def test_a_watch_that_failed_or_stopped_is_a_status_finding(
    store: RedisStore,  # noqa: F811
    at: int,
    error: str,
    summary: str,
    clock: str,
) -> None:
    master_critical.beat(store, "sw", at, error)

    [found] = master_critical.findings(store, "sw", CRITICAL_AT)

    assert (found.kind, found.subject, found.summary) == ("master watch", "sw", summary)
    assert found.evidence == (f"Last watch pass at 1970-01-01 {clock}Z",)
    assert (found.threshold, found.measure) == (master_critical.WATCH_THRESHOLD, (CRITICAL_AT - at) // MINUTE)


def test_a_clean_watch_within_five_minutes_is_no_finding(store: RedisStore) -> None:  # noqa: F811
    assert master_critical.findings(store, "sw", CRITICAL_AT) == []
    master_critical.beat(store, "sw", CRITICAL_AT - 5 * MINUTE + 1, "")

    assert master_critical.findings(store, "sw", CRITICAL_AT) == []


@pytest.mark.feature("swarm-master-critical")
def test_the_forced_runner_runs_master_up_replace_bounded_with_the_launch_env(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    at = critical(store, Forced())
    ran: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        ran.append((command, options))
        return subprocess.CompletedProcess(command, 0, '{"master": "master@a1b2c3-0002"}\n', "")

    monkeypatch.setattr(store_module, "connect", lambda: store)
    monkeypatch.setattr(
        master_fallback, "subprocess", SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError)
    )
    monkeypatch.setattr(master_fallback.time, "time", lambda: (at + MINUTE) / 1000)
    monkeypatch.setattr(master_fallback.Path, "home", lambda: tmp_path)
    environ = {"PATH": "/usr/bin", "A": "1"}

    master_fallback.forced("sw", at, environ)

    [(command, options)] = ran
    assert command == [sys.executable, "-P", "-m", "scripts.install", "swarm", "sw", "master", "up", "--replace"]
    assert options == {
        "capture_output": True,
        "text": True,
        "timeout": 240,
        "env": master_fallback.launch_env(environ),
        "check": False,
    }
    assert master_critical.read(store, "sw")["attempts"][0]["outcome"] == "started"


@pytest.mark.parametrize(
    "error, reason",
    [
        (FileNotFoundError(2, "No such file or directory"), "FileNotFoundError: [Errno 2] No such file or directory"),
        (
            subprocess.TimeoutExpired(["master"], 240),
            "TimeoutExpired: Command '['master']' timed out after 240 seconds",
        ),
    ],
)
@pytest.mark.feature("swarm-master-critical")
def test_the_forced_runner_records_a_launch_that_could_not_run(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    reason: str,
) -> None:
    at = critical(store, Forced())

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        raise error

    monkeypatch.setattr(store_module, "connect", lambda: store)
    monkeypatch.setattr(
        master_fallback, "subprocess", SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError)
    )
    monkeypatch.setattr(master_fallback.time, "time", lambda: (at + MINUTE) / 1000)

    master_fallback.forced("sw", at, {})

    [attempt] = master_critical.read(store, "sw")["attempts"]
    assert (attempt["reason"], attempt["code"]) == (reason, -1)
    assert capsys.readouterr().out == f"forced master up of swarm sw at 1970-01-01 00:30Z failed: {reason}\n"


@pytest.mark.feature("swarm-master-critical")
def test_a_forced_runner_refused_the_record_lock_prints_the_refusal_and_leaves_the_start_to_the_retry(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    at = critical(store, Forced())
    monkeypatch.setattr(master_critical, "WAIT_S", 0.05)
    held = store.redis.lock(store.key("sw", master_critical.LOCK), timeout=5)
    assert held.acquire(blocking=False)

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, '{"master": "master@a1b2c3-0002"}\n', "")

    monkeypatch.setattr(store_module, "connect", lambda: store)
    monkeypatch.setattr(
        master_fallback, "subprocess", SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError)
    )

    master_fallback.forced("sw", at, {})

    held.release()
    printed = json.loads(capsys.readouterr().out)
    assert (sorted(printed), printed["swarm"], printed["forced_at"]) == (["error", "forced_at", "swarm"], "sw", at)
    assert printed["error"].startswith("LockError: ")
    assert master_critical.read(store, "sw")["attempts"] == [{"at": at, "outcome": "", "reason": ""}]


@pytest.mark.feature("swarm-master-critical")
def test_a_pass_drops_the_watch_beat_of_a_swarm_the_fallback_no_longer_watches(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    store.create(SwarmConfig("old", "/repo", max_eng=0, max_ci=0))
    master_critical.beat(store, "old", START, "")

    master_fallback.critical(store, Ledger(), ENV, set(), CRITICAL_AT)

    assert master_critical.findings(store, "old", CRITICAL_AT) == []
    assert store.redis.get(store.key("old", master_critical.BEAT)) is None
    assert json.loads(store.redis.get(store.key("sw", master_critical.BEAT))) == {"at": CRITICAL_AT, "error": ""}


def test_with_every_swarm_watched_no_watch_beat_is_dropped(store: RedisStore) -> None:  # noqa: F811
    master_critical.beat(store, "sw", START, "")

    master_critical.unwatch(store, ["sw"])

    assert json.loads(store.redis.get(store.key("sw", master_critical.BEAT))) == {"at": START, "error": ""}
