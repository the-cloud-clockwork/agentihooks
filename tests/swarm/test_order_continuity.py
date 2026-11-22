from typing import Any

import pytest

from scripts.handoff import envelope
from scripts.swarm import cli, master_stall, prompt
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_freeze import task
from tests.swarm.test_order_check import ORDER, ledger_doc

pytestmark = pytest.mark.unit

SLUG = "sw"
LINE = "phases/p1 Build: 1 open, 1 claimable, 0 claimed or in a pull request, 1 done"
HANDOFF = "# Handoff v2\n## Done\nNone\n## Stopped at\nNone\n## Next\nRead the orders.\n<!-- handoff complete -->\n"
OPEN = "Operator orders open at the handoff. Build each before other work; only the operator's words clear one:"
NONE = "Operator orders: none were open at the handoff."
UNKNOWN = (
    "Operator orders: unknown, the ledger could not be read at the handoff. "
    "Run agentihooks api --slug sw get orders first."
)


class Ledger:
    def __init__(self, doc: dict[str, Any] | None) -> None:
        self.doc = doc

    def state(self, slug: str) -> dict[str, Any]:
        if self.doc is None:
            raise SwarmError(f"ledger {slug}: unreachable")
        return self.doc

    def task(self, slug: str, task_id: str) -> None:
        return None


class Unread(Ledger):
    def state(self, slug: str) -> dict[str, Any]:
        raise AssertionError("only a master handoff reads the orders")


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, "/repo", 1, 1))
    return found


def ordered() -> Ledger:
    return Ledger(ledger_doc([task("t1"), task("t5", state="done")], p1=ORDER))


def seated(store: RedisStore) -> AgentRecord:
    agent = AgentRecord("master@a1b2c3-0001", MASTER, MASTER, seat=f"master@{SLUG}")
    store.put_agent(SLUG, agent)
    store.seats.occupy(agent.seat, agent.name, 1)
    return agent


@pytest.mark.feature("master-handoff-carries-orders")
@pytest.mark.parametrize(
    ("ledger", "orders"),
    [
        (ordered(), [LINE]),
        (Ledger(ledger_doc([task("t1")])), []),
        (Ledger(None), "unknown"),
    ],
)
def test_a_master_envelope_carries_one_line_for_each_open_order(
    store: RedisStore, ledger: Ledger, orders: list[str] | str
) -> None:
    built = {"ledger": SLUG, "agent": "master@a1b2c3-0001"}
    assert envelope.with_orders(built, store, ledger, seated(store)) == {**built, "orders": orders}
    assert built == {"ledger": SLUG, "agent": "master@a1b2c3-0001"}


@pytest.mark.feature("master-handoff-carries-orders")
def test_an_engineer_envelope_carries_no_orders_and_reads_none(store: RedisStore) -> None:
    built = {"ledger": SLUG, "agent": "engineer@a1b2c3-0002"}
    engineer = AgentRecord("engineer@a1b2c3-0002", "eng", "t1")
    assert envelope.with_orders(built, store, Unread(None), engineer) is built


@pytest.mark.feature("master-handoff-carries-orders")
def test_a_master_handoff_through_the_swarm_command_stores_the_open_orders(store: RedisStore) -> None:
    found, _ = cli._hand_off(store, SLUG, seated(store), HANDOFF, "recycle", ordered())
    assert found["orders"] == [LINE]
    stored = store.handoff_envelope(SLUG, MASTER)
    assert stored is not None and stored["orders"] == [LINE]


@pytest.mark.feature("master-handoff-carries-orders")
def test_a_ledger_that_cannot_be_read_never_stops_a_master_handoff(store: RedisStore) -> None:
    found, _ = cli._hand_off(store, SLUG, seated(store), HANDOFF, "recycle", Ledger(None))
    assert found["orders"] == "unknown"
    assert store.handoff(SLUG, MASTER) == HANDOFF
    assert [agent.state for agent in store.agents(SLUG)] == ["finished"]


@pytest.mark.feature("master-handoff-carries-orders")
def test_an_engineer_handoff_through_the_swarm_command_reads_no_orders(store: RedisStore) -> None:
    engineer = AgentRecord("engineer@a1b2c3-0002", "eng", "t1", seat=f"eng-1@{SLUG}")
    store.put_agent(SLUG, engineer)
    store.seats.occupy(engineer.seat, engineer.name, 1)
    found, _ = cli._hand_off(store, SLUG, engineer, HANDOFF, "recycle", Unread(None))
    assert "orders" not in found


@pytest.mark.feature("master-handoff-carries-orders")
def test_a_master_the_stall_pass_recycles_leaves_the_open_orders(store: RedisStore) -> None:
    master_stall.recycle(SLUG, store, ordered(), seated(store), HANDOFF, 1_800_000_000_000, master_stall.STALLED)
    stored = store.handoff_envelope(SLUG, MASTER)
    assert stored is not None and stored["orders"] == [LINE]


def primed(found: dict[str, Any]) -> list[str]:
    return prompt.handoff_lines({"handoff": HANDOFF, "handoff_envelope": found})


@pytest.mark.feature("master-handoff-carries-orders")
@pytest.mark.parametrize(
    ("orders", "said"),
    [([LINE, "tasks/t9 Seats: 1 open"], [OPEN, LINE, "tasks/t9 Seats: 1 open"]), ([], [NONE]), ("unknown", [UNKNOWN])],
)
def test_the_opening_prompt_lists_the_orders_of_the_envelope_before_the_document(
    orders: list[str] | str, said: list[str]
) -> None:
    lines = primed({"ledger": SLUG, "orders": orders})
    document = lines.index("Handoff document: continue from it:")
    assert lines[2:document] == said


@pytest.mark.feature("master-handoff-carries-orders")
def test_an_envelope_without_orders_primes_as_before() -> None:
    lines = primed({"ledger": SLUG, "reason": "quota"})
    assert lines[2] == "Handoff document: continue from it:"
    assert lines[:2] == ["1. Handoff runtime envelope:", '{\n  "ledger": "sw",\n  "reason": "quota"\n}']
    assert prompt.handoff_lines({"handoff": HANDOFF})[:3] == [
        "1. Handoff runtime envelope:",
        "Missing runtime envelope.",
        "Handoff document: continue from it:",
    ]
