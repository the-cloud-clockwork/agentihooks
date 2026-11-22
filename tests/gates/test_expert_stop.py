"""The Stop gate for an expert: a session of a running lineage does not end while its mission is open."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.gates import Call, Gate, Who
from scripts.gates.base import Decision
from scripts.gates.expert_stop import ExpertStop
from scripts.gates.verdicts import Verdicts
from scripts.swarm import expert_lineage, idle
from scripts.swarm.store import AgentRecord, RedisStore
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit

SLUG, ME = "sw", "expert@a1b2c3-0001"
WHO = Who(name=ME, swarm=SLUG, lane="expert", task="expert-1")
STOP = Call("")
NOW = 1_000_000
DENIED = (
    "Lineage expert-1 still runs, so this session does not end here: an expert works its mission to the end. "
    "Keep working. Near the context limit hand off with `agentihooks swarm sw handoff <doc>`. When every "
    "deliverable is proven, end the lineage with `agentihooks swarm sw expert complete <proof>`. While you wait "
    'on something, declare it with `agentihooks swarm sw wait <minutes> --reason "<what>"`.'
)
Stop = Callable[..., Decision]


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    lineage = expert_lineage.start(found, SLUG, GOOD, "/missions/batch.md", "xhigh", "operator", 1)
    found.put_agent(SLUG, AgentRecord(ME, "expert", lineage.id, seat=lineage.seat, state="working"))
    return found


@pytest.fixture
def stop(store: RedisStore, tmp_path: Path) -> Stop:
    def decide(who: Who = WHO, now: int = NOW) -> Decision:
        gate = ExpertStop(connect=lambda: store, now=lambda: now)
        return gate.decide(STOP, who, Verdicts(SLUG, gate.name, tmp_path))

    return decide


def test_it_is_a_stop_gate_that_ships_enforcing() -> None:
    gate = ExpertStop()
    assert isinstance(gate, Gate)
    assert (gate.name, gate.default_mode) == ("expert-stop", "enforce")
    assert gate.matches(Call(""))
    assert not gate.matches(Call("Bash", {"command": "ls"}))


@pytest.mark.feature("expert-stop-gate")
def test_an_expert_of_a_running_lineage_is_told_how_to_go_on_instead_of_stopping(stop: Stop) -> None:
    for _ in range(4):
        decision = stop()
        assert (decision.allowed, decision.reason) == (False, DENIED)


@pytest.mark.feature("expert-stop-gate")
def test_an_expert_that_handed_its_seat_off_stops(store: RedisStore, stop: Stop) -> None:
    [agent] = store.agents(SLUG)
    store.put_agent(SLUG, replace(agent, state="finished"))
    assert stop() == Decision()


@pytest.mark.feature("expert-stop-gate")
@pytest.mark.parametrize("ended", ["stopped", "complete"])
def test_an_expert_whose_lineage_ended_stops(store: RedisStore, stop: Stop, ended: str) -> None:
    if ended == "stopped":
        expert_lineage.stop(store, SLUG, "expert-1", "operator", "the order changed", 5)
    else:
        expert_lineage.complete(store, SLUG, "expert-1", ME, "# Mission proof\n", 5)
    assert expert_lineage.get(store, SLUG, "expert-1").state == ended
    assert stop() == Decision()


@pytest.mark.feature("expert-stop-gate")
def test_a_declared_wait_lets_the_expert_stop_until_it_ends(store: RedisStore, stop: Stop) -> None:
    idle.declare_wait(store.redis, SLUG, ME, NOW + 60_000, "the merge queue", NOW)
    assert stop() == Decision()
    assert stop(now=NOW + 59_999) == Decision()
    assert stop(now=NOW + 60_000).reason == DENIED
    idle.end_wait(store.redis, SLUG, ME, NOW + 61_000)
    assert stop().reason == DENIED


@pytest.mark.parametrize(
    "who",
    [
        Who(name="engineer@a1b2c3-0001", swarm=SLUG, lane="eng", task="t1"),
        Who(name="master@a1b2c3-0001", swarm=SLUG, lane="master", task="master"),
        Who(name=ME, swarm="", lane="expert", task="expert-1"),
        Who(name="", swarm=SLUG),
        Who(name="expert@a1b2c3-0009", swarm=SLUG, lane="expert", task="expert-1"),
        Who(name=ME, swarm="other", lane="expert", task="expert-1"),
    ],
)
def test_no_other_session_is_held(stop: Stop, who: Who) -> None:
    assert stop(who) == Decision()


def test_an_expert_whose_lineage_record_is_gone_is_not_held(store: RedisStore, stop: Stop) -> None:
    store.redis.hdel(store.key(SLUG, "expert-lineages"), "expert-1")
    assert stop() == Decision()


def test_the_gate_entry_runs_it_by_name() -> None:
    from scripts.gates.entry import GATES

    assert isinstance(GATES["expert-stop"], ExpertStop)


def test_without_an_injected_store_and_clock_the_gate_reads_the_swarm_store_and_the_time(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: store)
    monkeypatch.setattr("scripts.handoff.transfers.now_ms", lambda: NOW)
    gate = ExpertStop()
    assert gate.decide(STOP, WHO, Verdicts(SLUG, gate.name, tmp_path)).reason == DENIED
    idle.declare_wait(store.redis, SLUG, ME, NOW + 60_000, "a reply", NOW)
    assert gate.decide(STOP, WHO, Verdicts(SLUG, gate.name, tmp_path)) == Decision()
