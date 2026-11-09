"""Claim holders idle at their prompts under a checked wait the tick could not end are retired and counted idle."""

from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import automated_gc, elapsed, idle, tick, timing
from scripts.swarm.health.findings import Limits, idle_with_claim
from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import AgentRecord
from scripts.tick.steps import agents

pytestmark = pytest.mark.unit

STOPPED = 1_794_216_840_000
WAITS_END_RAN_S = (STOPPED - 56 * 3_600_000) / 1000
CHECKS_WAIT_MS = 12 * 3_600_000
PASSES = 25
WAITS_END = agents.WAITS_END


class Redis(dict[str, Any]):
    """A Redis holding plain keys and hashes in one dict."""

    def get(self, key: str) -> Any:
        """Return the plain value at ``key``, or None."""
        return super().get(key)

    def set(self, key: str, value: Any, **_: Any) -> None:
        """Store ``value`` at ``key``."""
        self[key] = value

    def delete(self, key: str) -> None:
        """Remove ``key``."""
        self.pop(key, None)

    def hgetall(self, key: str) -> dict[str, Any]:
        """Return the hash at ``key``."""
        return dict(super().get(key) or {})

    def hset(self, key: str, field: str, value: Any) -> None:
        """Set ``field`` of the hash at ``key``."""
        self.setdefault(key, {})[field] = value


class Store:
    """A swarm store over :class:`Redis` that keeps each saved agent."""

    def __init__(self) -> None:
        """Start empty."""
        self.redis = Redis()
        self.saved: dict[str, AgentRecord] = {}

    def key(self, slug: str, *parts: str) -> str:
        """Return the key of ``parts`` in swarm ``slug``."""
        return ":".join(("sw", slug, *parts))

    def put_agent(self, slug: str, agent: AgentRecord) -> None:
        """Keep ``agent`` as saved."""
        self.saved[agent.name] = agent


def _checked_wait(name: str) -> dict[str, Any]:
    """Return the checks wait the stop gate declared when ``name`` stopped."""
    target = f"https://github.com/the-cloud-clockwork/agentihooks/pull/{name[-4:]}"
    return {
        "until": STOPPED + CHECKS_WAIT_MS,
        "reason": f"checks on {target}",
        "at": STOPPED,
        "on": {"kind": "checks", "target": target},
    }


def _seams(monkeypatch: pytest.MonkeyPatch, waits: dict[str, dict[str, Any]], retired: list[str]) -> Any:
    """Patch the tick seams the idle watch reaches beyond its own counting and return the runtime."""
    monkeypatch.setattr(tick, "_observe", lambda runtime, agent: PaneObservation("idle"))
    monkeypatch.setattr(tick, "_refusal_due", lambda *a: False)
    monkeypatch.setattr(tick, "_stuck_on_provider", lambda *a: False)
    monkeypatch.setattr(tick, "_stale_nudge", lambda *a: [])
    monkeypatch.setattr(tick, "_operator_at_pane", lambda *a: False)
    monkeypatch.setattr(tick, "_drop", lambda *a, **k: ", task reopened")
    monkeypatch.setattr(tick, "outage", SimpleNamespace(mark_pane=lambda *a: None, held=lambda *a: False))
    monkeypatch.setattr(
        tick,
        "resume",
        SimpleNamespace(
            recovery_exhausted=lambda *a: False,
            recovery_pending=lambda *a: False,
            recovery_holds=lambda *a: False,
            recovery_blocker=lambda *a: "",
        ),
    )
    monkeypatch.setattr(
        tick,
        "idle_state",
        SimpleNamespace(
            WORKING=idle.WORKING,
            WAITING=idle.WAITING,
            IDLE=idle.IDLE,
            BEAT_TTL_S=idle.BEAT_TTL_S,
            verdict=idle.verdict,
            heartbeat=lambda redis, slug, name: {"state": idle.IDLE, "at": STOPPED},
            wait=lambda redis, slug, name: waits.get(name),
        ),
    )
    monkeypatch.setattr(tick, "ledger_probe", SimpleNamespace(holding=lambda *a: False))
    monkeypatch.setattr(automated_gc, "owned", lambda *a: False)
    monkeypatch.setattr(
        tick, "gate_log", SimpleNamespace(append=lambda *a: None, Row=SimpleNamespace(of=lambda *a, **k: a))
    )
    monkeypatch.setattr(tick, "reaper", SimpleNamespace(scratch_homes=lambda slug, task: ()))

    def retire(agent: AgentRecord, homes: Any) -> bool:
        """Record the retired agent's name."""
        retired.append(agent.name)
        return True

    return SimpleNamespace(retire=retire)


def _run(store: Store, runtime: Any, crew: list[AgentRecord], passes: int) -> dict[str, int]:
    """Run ``passes`` one minute idle watch passes over ``crew`` and return the pass that retired each agent."""
    rows = {agent.task: {"claimed_by": agent.name, "state": "claimed"} for agent in crew}
    when: dict[str, int] = {}
    for count in range(1, passes + 1):
        now = STOPPED + count * elapsed.PASS_MS
        for agent in crew:
            if agent.name in when:
                continue
            current = store.saved.get(agent.name, agent)
            said = agents._watch_idle("rig", store, "ledger", runtime, rows, current, now)
            if any(line.startswith(f"stalled {agent.name}") for line in said):
                when[agent.name] = count
    return when


def _starved(store: Store) -> None:
    """Record the waits end step's last run long before the agents stopped."""
    timing.step_ended(store, "rig", WAITS_END, WAITS_END_RAN_S)


@pytest.mark.feature("swarm-tick-idle-claim-retire")
def test_workstation_engineers_idle_under_an_unended_checked_wait_are_retired_within_25_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crew = [AgentRecord(name=f"engineer@323133-{n}", lane="eng", task=f"t{n}") for n in (7730, 7732)]
    retired: list[str] = []
    runtime = _seams(monkeypatch, {a.name: _checked_wait(a.name) for a in crew}, retired)
    store = Store()
    _starved(store)
    when = _run(store, runtime, crew, PASSES)
    assert sorted(retired) == sorted(a.name for a in crew)
    assert all(count <= PASSES for count in when.values())


@pytest.mark.feature("swarm-tick-idle-claim-retire")
def test_eight_codex_hive_owners_with_no_turn_are_retired_within_25_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    crew = [
        AgentRecord(
            name=f"engineer@323133-77{n:02d}",
            lane="eng",
            task=f"h{n}",
            harness="codex",
            hive="6d9e2d0c1ed5106d",
            runtime_backend="hive",
        )
        for n in range(8)
    ]
    retired: list[str] = []
    runtime = _seams(monkeypatch, {a.name: _checked_wait(a.name) for a in crew}, retired)
    store = Store()
    _starved(store)
    assert set(_run(store, runtime, crew, PASSES)) == {a.name for a in crew}


@pytest.mark.feature("swarm-tick-idle-claim-retire")
def test_the_idle_with_claim_finding_fires_on_the_counted_idle_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentRecord(name="engineer@323133-7730", lane="eng", task="t7730")
    runtime = _seams(monkeypatch, {agent.name: _checked_wait(agent.name)}, [])
    monkeypatch.setattr(tick, "IDLE_KILL_TICKS", 10**6)
    store = Store()
    _starved(store)
    _run(store, runtime, [agent], 5)
    tasks = {"t7730": {"id": "t7730", "state": "claimed", "title": "hive two restore"}}
    found = idle_with_claim([asdict(store.saved[agent.name])], tasks, Limits())
    assert [finding.subject for finding in found] == [agent.name]


@pytest.mark.feature("swarm-tick-idle-claim-retire")
def test_a_checked_wait_still_holds_the_idle_count_while_waits_end_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = AgentRecord(name="engineer@323133-7730", lane="eng", task="t7730")
    retired: list[str] = []
    runtime = _seams(monkeypatch, {agent.name: _checked_wait(agent.name)}, retired)
    store = Store()
    timing.step_ended(store, "rig", WAITS_END, (STOPPED + PASSES * elapsed.PASS_MS) / 1000)
    _run(store, runtime, [agent], PASSES)
    assert retired == []
    assert store.saved[agent.name].idle_ticks == 0


@pytest.mark.feature("swarm-tick-idle-claim-retire")
@pytest.mark.parametrize(("starved", "lost"), [(True, True), (False, False)])
def test_a_silent_hive_owner_under_an_unended_checked_wait_is_lost(
    monkeypatch: pytest.MonkeyPatch, starved: bool, lost: bool
) -> None:
    agent = AgentRecord(name="engineer@323133-7700", lane="eng", task="h0", harness="codex", hive="6d9e2d0c1ed5106d")
    _seams(monkeypatch, {agent.name: _checked_wait(agent.name)}, [])
    reports = SimpleNamespace(lost=lambda agent, now_ms, limit: True)
    monkeypatch.setattr(tick, "reports", SimpleNamespace(Reports=lambda redis: reports, LOST_MS=10 * elapsed.PASS_MS))
    store = Store()
    now = STOPPED + PASSES * elapsed.PASS_MS
    timing.step_ended(store, "rig", WAITS_END, WAITS_END_RAN_S if starved else now / 1000)
    assert agents._hive_lost("rig", store, agent, now) is lost
