from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from redis import Redis

    from scripts.hive.run_orders import RunOrder

pytestmark = pytest.mark.unit


@pytest.fixture
def setup() -> "tuple[Redis, ModuleType, RunOrder]":
    import fakeredis

    from scripts.hive import run_orders

    redis = fakeredis.FakeRedis(decode_responses=True)
    order = run_orders.RunOrder(
        "run-1", "grade", "case text", "agentihooks", "owner@fixture", ("build", "lint", "test"), "host", 9000
    )
    return redis, run_orders, order


def test_a_run_order_round_trips_through_its_hive_stream(setup: "tuple[Redis, ModuleType, RunOrder]") -> None:
    from scripts.hive import registry
    from scripts.hive.orders import RECEIPT_SECONDS, Ack

    redis, run_orders, order = setup
    store = run_orders.RunOrders(redis)
    assert store.stream("host") == f"{registry.key('host')}:run-orders"
    assert store.receipt(order) == f"{registry.key('host')}:run-order:run-1"
    sent = store.send("host", order)
    [(item, fields)] = redis.xrange(store.stream("host"))
    assert sent == item
    assert run_orders.RunOrder.decode(fields["order"]) == order
    assert redis.hget(store.receipt(order), "sent") == "1"
    assert redis.ttl(store.receipt(order)) == RECEIPT_SECONDS
    assert store.ack(order) is None
    store.finish("host", item, order, Ack("done", {"host_id": "host"}))
    store.finish("host", item, order, Ack("failed", {}, "late"))
    assert store.ack(order) == Ack("done", {"host_id": "host"})
    assert redis.hget(f"{store.stream('host')}:acks", item) == Ack("done", {"host_id": "host"}).encode()
    assert redis.xlen(store.stream("host")) == 0


def test_a_duplicate_run_order_send_adds_no_entry(setup: "tuple[Redis, ModuleType, RunOrder]") -> None:
    redis, run_orders, order = setup
    store = run_orders.RunOrders(redis)
    assert store.send("host", order) is not None
    assert store.send("host", order) is None
    assert redis.xlen(store.stream("host")) == 1


def test_a_send_raced_by_another_sender_adds_no_entry(
    setup: "tuple[Redis, ModuleType, RunOrder]", monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.client import Pipeline

    redis, run_orders, order = setup
    store = run_orders.RunOrders(redis)
    multi = Pipeline.multi

    def raced(pipe: Pipeline) -> None:
        redis.hset(store.receipt(order), "sent", "1")
        multi(pipe)

    monkeypatch.setattr(Pipeline, "multi", raced)
    assert store.send("host", order) is None
    assert redis.xlen(store.stream("host")) == 0


def test_a_run_order_for_another_hive_is_not_sent(setup: "tuple[Redis, ModuleType, RunOrder]") -> None:
    redis, run_orders, order = setup
    store = run_orders.RunOrders(redis)
    assert store.send("other", order) is None
    assert redis.keys() == []
