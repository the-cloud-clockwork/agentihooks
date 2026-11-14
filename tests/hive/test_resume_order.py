import json
import sys
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

HIVE = "hive"


@pytest.fixture
def redis() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def _order() -> Any:
    from scripts.hive.orders import CommandOrder

    return CommandOrder("resume:x:1:1", "resume", "rig", "engineer", "x", 1, "continue", 5, "pane")


def _ack(redis: Any, order: Any) -> dict[str, Any]:
    from scripts.hive.orders import Orders

    return json.loads(redis.hget(Orders(redis).command_receipt(HIVE, order.id), "ack"))


@pytest.mark.feature("hub-dead-resume")
def test_the_hive_spawns_the_resume_order_process_with_the_encoded_order(
    redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import orders

    spawned = Mock()
    monkeypatch.setattr(orders.subprocess, "Popen", spawned)
    order = _order()
    orders.Consumer(redis, HIVE, Mock())._spawn_resume(order)
    assert spawned.call_args.args[0] == [sys.executable, "-m", "scripts.hive.resume_order", HIVE, order.encode()]


@pytest.mark.feature("hub-dead-resume")
def test_the_resume_order_process_stores_the_answer(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import orders, resume_order
    from scripts.swarm import store

    monkeypatch.setattr(store, "redis_client", lambda: redis)
    monkeypatch.setattr(redis, "close", lambda: None)

    def local(cls: Any, client: Any, hive: str) -> Any:
        return orders.Consumer(client, hive, cast(Any, Mock()))

    monkeypatch.setattr(orders.Consumer, "local", classmethod(local))
    order = _order()
    resume_order.run(HIVE, order.encode())
    assert _ack(redis, order)["status"] == "failed"


@pytest.mark.feature("hub-dead-resume")
def test_a_resume_order_process_that_fails_to_start_stores_a_failed_answer(
    redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import orders, resume_order
    from scripts.swarm import store

    def broken(cls: Any, client: Any, hive: str) -> Any:
        raise RuntimeError("no herdr")

    monkeypatch.setattr(store, "redis_client", lambda: redis)
    monkeypatch.setattr(redis, "close", lambda: None)
    monkeypatch.setattr(orders.Consumer, "local", classmethod(broken))
    order = _order()
    resume_order.run(HIVE, order.encode())
    assert _ack(redis, order)["failure"] == "error: RuntimeError"


@pytest.mark.feature("hub-dead-resume")
def test_a_late_answer_never_overwrites_the_first(redis: Any) -> None:
    from scripts.hive.orders import DONE, FAILED, Ack, Orders, answer

    receipt = Orders(redis).command_receipt(HIVE, "resume:x:1:1")
    answer(redis, receipt, Ack(DONE, {"host_id": HIVE}))
    answer(redis, receipt, Ack(FAILED, {}, "late"))
    assert json.loads(redis.hget(receipt, "ack"))["status"] == DONE


@pytest.mark.feature("hub-dead-resume")
def test_a_resume_order_that_never_answers_is_answered_failed_after_its_run_bound(redis: Any) -> None:
    from scripts.hive.orders import RESUME_RUNS, Consumer, Orders

    order = _order()
    Orders(redis).command(HIVE, order)
    consumer = Consumer(redis, HIVE, Mock())
    consumer.offbeat = Mock()
    receipt = Orders(redis).command_receipt(HIVE, order.id)
    for _ in range(RESUME_RUNS + 1):
        consumer.poll_commands()
        redis.delete(f"{receipt}:running")
    assert consumer.offbeat.call_count == RESUME_RUNS
    assert _ack(redis, order) == {"status": "failed", "target": {"host_id": HIVE}, "failure": "resume never answered"}
    consumer.poll_commands()
    assert redis.xlen(Orders(redis).commands(HIVE)) == 0


@pytest.mark.feature("hub-dead-resume")
def test_a_running_resume_keeps_its_marker_alive(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import orders

    order = _order()
    receipt = orders.Orders(redis).command_receipt(HIVE, order.id)
    redis.set(f"{receipt}:running", "1", ex=orders.RESUME_RUN_SECONDS)
    monkeypatch.setattr(orders, "RESUME_RENEW_SECONDS", 0.01)
    consumer = orders.Consumer(redis, HIVE, Mock())

    def slow(_order: Any) -> Any:
        import time

        redis.expire(f"{receipt}:running", 1)
        time.sleep(0.2)
        return SimpleNamespace(encode=lambda: json.dumps({"status": "done", "target": {}}))

    monkeypatch.setattr(consumer, "command", slow)
    consumer.run_resume(order)
    assert redis.ttl(f"{receipt}:running") > 1
