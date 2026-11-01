"""Simulated tick load under a virtual clock, held to a pass budget that only shrinks."""

import importlib
import inspect
import json
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import cli, tick_report, timing
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime
from tests.swarm.tick_scenarios.test_characterisation import BASE_MS, github_reply

tick_module = importlib.import_module("scripts.swarm.tick")
ROOT = Path(__file__).resolve().parents[2]
BUDGET = Path(__file__).with_name("tick_scenarios") / "load_budget.json"
INITIAL_BUDGET_S = 384.0
SLOWDOWN_S = 300.0
AGENTS = {"eng": 10, "ci": 5}
TASKS = 900
LATENCY_S = {"ledger": 0.25, "runtime": 0.5, "redis": 0.002}


class VirtualClock:
    """Seconds that pass only when a collaborator call is charged."""

    def __init__(self) -> None:
        self.seconds = 0.0
        self.calls: Counter[str] = Counter()
        self._lock = threading.Lock()

    def charge(self, surface: str, seconds: float) -> None:
        with self._lock:
            self.seconds += seconds
            self.calls[surface] += 1

    def now(self) -> float:
        with self._lock:
            return self.seconds

    def reset(self) -> None:
        with self._lock:
            self.seconds = 0.0
            self.calls.clear()


class VirtualTime:
    """A time module whose clocks read the virtual clock."""

    def __init__(self, clock: VirtualClock) -> None:
        self._clock = clock

    def monotonic(self) -> float:
        return self._clock.now()

    def perf_counter(self) -> float:
        return self._clock.now()

    def time(self) -> float:
        return BASE_MS / 1000 + self._clock.now()

    def time_ns(self) -> int:
        return BASE_MS * 1_000_000 + int(self._clock.now() * 1_000_000_000)

    def sleep(self, seconds: float) -> None:
        self._clock.charge("sleep", seconds)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class SlowClient:
    """A Redis client whose every command costs the Redis latency on the virtual clock."""

    def __init__(self, client: Any, clock: VirtualClock) -> None:
        self._client = client
        self._clock = clock

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._client, name)
        if not callable(value):
            return value

        def charged(*args: Any, **kwargs: Any) -> Any:
            self._clock.charge("redis", LATENCY_S["redis"])
            return value(*args, **kwargs)

        return charged


def slow_methods(target: Any, surface: str, clock: VirtualClock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Charge every public method of target the latency of its surface."""
    for name, method in inspect.getmembers(target, inspect.ismethod):
        if name.startswith("_"):
            continue

        def charged(*args: Any, _method: Callable[..., Any] = method, **kwargs: Any) -> Any:
            clock.charge(surface, LATENCY_S[surface])
            return _method(*args, **kwargs)

        monkeypatch.setattr(target, name, charged)


def populate(store: RedisStore) -> tuple[FakeLedger, FakeRuntime]:
    """Fifteen working agents on their claimed tasks among nine hundred ledger tasks."""
    rows = [{"id": f"t{number}", "lane": "eng" if number % 3 else "ci"} for number in range(1, TASKS + 1)]
    ledger, runtime = FakeLedger(rows), FakeRuntime()
    pending = {lane: [row["id"] for row in rows if row["lane"] == lane] for lane in AGENTS}
    number = 0
    for lane, count in AGENTS.items():
        kind = "engineer" if lane == "eng" else "ci"
        for task in pending[lane][:count]:
            number += 1
            name = f"{kind}@a1b2c3-{number:04d}"
            started = BASE_MS - tick_module.STARTUP_GRACE_MS - 1
            store.put_agent("sw", AgentRecord(name, lane, task, f"w1:p{number}", started_at=started))
            ledger.rows[task].update(state="claimed", claimed_by=name)
            runtime.live.add(name)
    return ledger, runtime


def load(monkeypatch: pytest.MonkeyPatch, slowdown_s: float = 0.0) -> dict[str, Any]:
    """Run one fenced tick pass under the virtual clock and return its tick report."""
    import fakeredis

    from scripts.swarm import commands, incidents, reboot
    from scripts.swarm.health import merge_stall

    clock = VirtualClock()
    virtual = VirtualTime(clock)
    monkeypatch.setattr(merge_stall, "request", github_reply)
    monkeypatch.setattr(commands, "hive_id", lambda: "fixture-hive")
    monkeypatch.setattr(incidents.socket, "gethostname", lambda: "fixture-host")
    monkeypatch.setattr(reboot, "boot_id", lambda: "00000000-0000-0000-0000-000000000001")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES", "0")
    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
    monkeypatch.setattr(time, "time", virtual.time)
    monkeypatch.setattr(timing, "time", virtual)
    monkeypatch.setattr(tick_module, "time", virtual)
    monkeypatch.setattr(cli, "now_ms", lambda: BASE_MS + int(clock.now() * 1000))
    client = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    store = RedisStore(SlowClient(client, clock))
    store.create(SwarmConfig("sw", "/repo", max_eng=AGENTS["eng"], max_ci=AGENTS["ci"]))
    ledger, runtime = populate(store)
    slow_methods(ledger, "ledger", clock, monkeypatch)
    slow_methods(runtime, "runtime", clock, monkeypatch)
    clock.reset()
    core = cli.tick

    def stepped(*args: Any, **kwargs: Any) -> list[str]:
        clock.charge("planted", slowdown_s)
        return core(*args, **kwargs)

    monkeypatch.setattr(cli, "tick", stepped)
    actions = cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    report = tick_report.read(store, "sw", 1)
    report.update(actions=actions, calls=dict(clock.calls), agents=len(store.agents("sw")), tasks=len(ledger.rows))
    print("TICK_LOAD=" + json.dumps(report, sort_keys=True, default=str))
    return report


def budget() -> float:
    """The pass budget this revision holds."""
    return float(json.loads(BUDGET.read_text())["pass_budget_s"])


def base_budget() -> float:
    """The pass budget on the integration branch, the initial budget before it holds one."""
    relative = BUDGET.relative_to(ROOT).as_posix()
    shown = subprocess.run(["git", "show", f"origin/dev:{relative}"], cwd=ROOT, capture_output=True, text=True)
    if shown.returncode:
        return INITIAL_BUDGET_S
    return float(json.loads(shown.stdout)["pass_budget_s"])


def check_budget(current: float, base: float) -> None:
    """Refuse a budget above its base or above the initial budget."""
    ceiling = min(base, INITIAL_BUDGET_S)
    assert current <= ceiling, f"pass budget {current}s rises above {ceiling}s; a pull request may only lower it"


@pytest.mark.feature("swarm-tick-pass-budget")
def test_simulated_load_pass_fits_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    report = load(monkeypatch)
    assert (report["agents"], report["tasks"], report["pass_count"]) == (15, TASKS, 1)
    assert report["pass_p95_s"] <= budget(), f"simulated pass {report['pass_p95_s']}s exceeds {budget()}s"


@pytest.mark.feature("swarm-tick-pass-budget")
def test_a_step_made_300_seconds_slower_breaks_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    report = load(monkeypatch, SLOWDOWN_S)
    assert report["calls"]["planted"] == 1
    assert report["pass_p95_s"] > budget()


@pytest.mark.feature("swarm-tick-pass-budget")
def test_the_budget_only_shrinks_against_its_base() -> None:
    check_budget(budget(), base_budget())


@pytest.mark.feature("swarm-tick-pass-budget")
@pytest.mark.parametrize(("current", "base"), [(385.0, 384.0), (300.0, 299.5), (400.0, 500.0)])
def test_a_raised_budget_is_refused(current: float, base: float) -> None:
    with pytest.raises(AssertionError, match="may only lower it"):
        check_budget(current, base)


@pytest.mark.feature("swarm-tick-pass-budget")
@pytest.mark.parametrize(("current", "base"), [(384.0, 384.0), (200.0, 384.0), (299.5, 300.0)])
def test_an_equal_or_lower_budget_is_accepted(current: float, base: float) -> None:
    check_budget(current, base)
