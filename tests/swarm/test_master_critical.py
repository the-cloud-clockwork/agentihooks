import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hooks.context import broadcast
from scripts.swarm import master_critical, master_fallback, master_launch
from scripts.swarm import runtime as runtime_module
from scripts.swarm import store as store_module
from scripts.swarm.status import status_report
from scripts.swarm.store import MASTER, AgentRecord, RedisStore
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
        self.options: list[dict[str, Any]] = []
        self.cleared: list[str] = []

    def create(self, message: str, severity: str = "info", **options: Any) -> str:
        self.raised.append((severity, message))
        self.options.append(options)
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


def watch(held: RedisStore, at: int, forced: Forced, live: set[str] | None = None) -> list[str]:
    return master_critical.watch(held, "sw", live or set(), at, {}, forced)


def critical(held: RedisStore, forced: Forced) -> int:
    watch(held, START, forced)
    watch(held, CRITICAL_AT, forced)
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
    forced, env = Forced(), {master_critical.MINUTES: "2"}

    assert master_critical.watch(store, "sw", set(), START, env, forced) == []
    assert master_critical.watch(store, "sw", set(), START + 2 * MINUTE - 1, env, forced) == []
    lines = master_critical.watch(store, "sw", set(), START + 2 * MINUTE, env, forced)

    assert lines == [
        "swarm sw is critical: no bound front door master since 1970-01-01 00:00Z",
        "forced master up of swarm sw: runner 3",
    ]
    assert forced.calls == [("sw", START + 2 * MINUTE)]
    assert alerts.raised == [("critical", master_critical.CRITICAL.format(slug="sw", minutes=2))]
    assert alerts.options == [{"source": "swarm"}]
    assert master_critical.read(store, "sw") == {
        "since": START,
        "critical_at": START + 2 * MINUTE,
        "alert": "alert-1",
        "attempts": [{"at": START + 2 * MINUTE, "outcome": "", "reason": ""}],
    }


@pytest.mark.feature("swarm-master-critical")
def test_a_failed_forced_start_is_retried_once_per_retry_window(store: RedisStore, alerts: Alerts) -> None:  # noqa: F811
    forced = Forced()
    at = critical(store, forced)
    master_critical.ended(store, "sw", at, 1, "boom\n", at + MINUTE)

    assert watch(store, at + master_critical.RETRY_MS - 1, forced) == []
    assert watch(store, at + master_critical.RETRY_MS, forced) == ["forced master up of swarm sw: runner 3"]
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
    ]
    assert master_critical.read(store, "sw")["attempts"] == [
        {"at": at, "outcome": "failed", "reason": reason, "ended_at": retry},
        {"at": retry, "outcome": "", "reason": ""},
    ]
    assert alerts.raised[-1] == (
        "critical",
        master_critical.STILL.format(slug="sw", at="1970-01-01 00:30Z", reason=reason),
    )
    assert alerts.cleared == ["alert-1"]


@pytest.mark.feature("swarm-master-critical")
def test_a_front_door_master_still_starting_holds_the_forced_start_until_its_deadline(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    forced = Forced()
    watch(store, START, forced)
    front_door(store, state="starting", started_at=CRITICAL_AT - MINUTE)

    assert watch(store, CRITICAL_AT, forced) == [
        "swarm sw is critical: no bound front door master since 1970-01-01 00:00Z"
    ]
    assert forced.calls == []
    assert master_critical.findings(store, "sw", CRITICAL_AT)[0].evidence[1] == "No forced master up has run yet"
    watch(store, CRITICAL_AT + MINUTE, forced)
    assert forced.calls == [("sw", CRITICAL_AT + MINUTE)]


@pytest.mark.parametrize(
    "state, seat",
    [("finished", f"{MASTER}@sw"), ("starting", f"{MASTER}-2@sw"), ("starting", "engineer@sw")],
)
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
def test_only_a_live_working_master_on_the_front_door_seat_ends_critical_mode(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    state: str,
    seat: str,
    live: bool,
) -> None:
    at = critical(store, Forced())
    name = front_door(store, state=state, seat=seat)

    watch(store, at + MINUTE, Forced(), {name} if live else set())

    assert master_critical.read(store, "sw")["critical_at"] == at
    assert alerts.cleared == []


@pytest.mark.parametrize("seat", [f"{MASTER}@sw", ""])
@pytest.mark.feature("swarm-master-critical")
def test_a_live_working_front_door_master_ends_critical_mode(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    seat: str,
) -> None:
    at = critical(store, Forced())
    name = front_door(store, seat=seat)
    cleared = f"Swarm sw is out of critical mode: master {name} bound the front door seat."

    assert watch(store, at + MINUTE, Forced(), {name}) == [cleared]
    assert master_critical.read(store, "sw") == {}
    assert alerts.cleared == ["alert-1"]
    assert alerts.raised[-1] == ("resolved", cleared)


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


@pytest.mark.parametrize("state", ["stopped", "stopping"])
@pytest.mark.feature("swarm-master-critical")
def test_a_stopped_swarm_leaves_critical_mode_and_is_never_forced(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
    state: str,
) -> None:
    forced = Forced()
    at = critical(store, forced)
    store.update("sw", state=state)
    cleared = "Swarm sw is out of critical mode: the swarm is stopped or sleeps with no work."

    assert watch(store, at + master_critical.RETRY_MS, forced) == [cleared]
    assert forced.calls == [("sw", at)]
    assert master_critical.read(store, "sw") == {}
    assert alerts.raised[-1] == ("resolved", cleared)


def test_a_sleeping_swarm_never_turns_critical(store: RedisStore, alerts: Alerts) -> None:  # noqa: F811
    forced = Forced()
    store.redis.set(store.key("sw", "master-retired-tasks"), "[]")

    for minute in (0, 30, 60):
        assert watch(store, START + minute * MINUTE, forced) == []

    assert forced.calls == []
    assert master_critical.read(store, "sw") == {}


def test_the_record_keeps_the_last_ten_forced_starts(store: RedisStore, alerts: Alerts) -> None:  # noqa: F811
    forced = Forced()
    at = critical(store, forced)
    for retry in range(1, 11):
        master_critical.ended(store, "sw", at + (retry - 1) * master_critical.RETRY_MS, 1, "boom", at)
        watch(store, at + retry * master_critical.RETRY_MS, forced)

    attempts = master_critical.read(store, "sw")["attempts"]
    assert [row["at"] for row in attempts] == [at + retry * master_critical.RETRY_MS for retry in range(1, 11)]


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
    assert attempt == {"at": at, "outcome": outcome, "reason": reason, "ended_at": at + MINUTE}
    if code:
        assert line == f"forced master up of swarm sw at 1970-01-01 00:30Z failed: {reason}"
        assert alerts.raised[-1][1] == master_critical.STILL.format(slug="sw", at="1970-01-01 00:30Z", reason=reason)
    else:
        assert line == "forced master up of swarm sw at 1970-01-01 00:30Z started a master"
        assert len(alerts.raised) == 1
    assert master_critical.findings(store, "sw", at + MINUTE)[0].evidence[1] == (
        f"Last forced master up at 1970-01-01 00:30Z: {outcome}" + (f": {reason}" if reason else "")
    )


def test_a_forced_start_ending_after_its_critical_state_cleared_writes_nothing(
    store: RedisStore,  # noqa: F811
    alerts: Alerts,
) -> None:
    assert master_critical.ended(store, "sw", CRITICAL_AT, 1, "boom", CRITICAL_AT + MINUTE) == (
        "forced master up of swarm sw at 1970-01-01 00:30Z ended after its critical state cleared"
    )
    assert master_critical.read(store, "sw") == {}
    assert alerts.raised == []


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
    assert command == [sys.executable, "-m", "scripts.install", "swarm", "sw", "master", "up", "--replace"]
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

    assert master_critical.read(store, "sw")["attempts"][0]["reason"] == reason
    assert capsys.readouterr().out == f"forced master up of swarm sw at 1970-01-01 00:30Z failed: {reason}\n"
