import json
import sys
from dataclasses import replace
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
    from scripts.hive import dispatch, orders

    spawned = Mock()
    monkeypatch.setattr(orders.subprocess, "Popen", spawned)
    monkeypatch.setattr(dispatch, "_resumes", [])
    order = _order()
    orders.Consumer(redis, HIVE, Mock())._spawn_resume(order)
    assert spawned.call_args.args[0] == [sys.executable, "-m", "scripts.hive.resume_order", HIVE, order.encode()]
    assert spawned.call_args.kwargs == {"stdin": orders.subprocess.DEVNULL, "stdout": orders.subprocess.DEVNULL}


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
    assert _ack(redis, order) == {"status": "failed", "target": {}, "failure": "error: RuntimeError"}


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
    consumer.poll_commands()
    assert redis.hget(receipt, "runs") == "1"
    assert redis.get(f"{receipt}:running") == "1"
    assert redis.ttl(f"{receipt}:running") > 0
    redis.delete(f"{receipt}:running")
    for _ in range(RESUME_RUNS):
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


@pytest.mark.feature("hub-dead-resume")
def test_a_resume_that_raises_answers_failed_with_an_empty_target(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import orders

    consumer = orders.Consumer(redis, HIVE, cast(Any, Mock()))

    def broken(_order: Any) -> Any:
        raise RuntimeError("herdr gone")

    monkeypatch.setattr(consumer, "command", broken)
    order = _order()
    consumer.run_resume(order)
    assert _ack(redis, order) == {"status": "failed", "target": {}, "failure": "error: RuntimeError"}


@pytest.mark.feature("hub-dead-resume")
def test_a_record_placed_by_runtime_target_is_held_on_its_hive(redis: Any) -> None:
    from scripts.hive.orders import CommandOrder, Consumer
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(redis)
    store.create(SwarmConfig("rig", "/repo", max_eng=1, max_ci=0, engine="events"))
    name = store.next_name("rig", "eng")
    assert store.claim("rig", "task", name, 600_000)
    record = AgentRecord(name, "eng", "task", seat="eng-1@rig", state="starting", runtime_target={"host_id": HIVE})
    agent = store.start_execution("rig", record)
    order = CommandOrder("resume:1", "resume", "rig", name, agent.execution_id, agent.generation, "", 0, "")
    held = Consumer(redis, HIVE, cast(Any, Mock()))._holding(order)
    assert held is not None and held.name == name


def _resuming(redis: Any, monkeypatch: pytest.MonkeyPatch, reaped: bool = True, recovered: bool = True) -> Any:
    from scripts.hive import orders
    from scripts.swarm.store import AgentRecord
    from scripts.swarm_v2.runtime.base import Outcome, Status

    record = AgentRecord("x", "eng", "task", pane_id="old")
    runtime = Mock()
    runtime.reap.return_value = Outcome("reap", Status.OK if reaped else Status.UNAVAILABLE, "local", None, "no reap")
    placed = SimpleNamespace(pane_id="new", launched_at=7)
    status = Status.OK if recovered else Status.UNAVAILABLE
    runtime.recover.return_value = Outcome("recover", status, "local", placed, "no recover")
    consumer = orders.Consumer(redis, HIVE, runtime)
    store = Mock()
    store.config.return_value = SimpleNamespace(repo_origin="")
    consumer.store = store
    monkeypatch.setattr(consumer, "_holding", lambda order: record)
    return SimpleNamespace(consumer=consumer, runtime=runtime, store=store, record=record)


@pytest.mark.feature("hub-dead-resume")
@pytest.mark.parametrize(("reaped", "failure"), [(False, "no reap"), (True, "no recover")])
def test_a_resume_the_runtime_refuses_answers_failed_on_this_hive(
    redis: Any, monkeypatch: pytest.MonkeyPatch, reaped: bool, failure: str
) -> None:
    from scripts.hive.orders import FAILED, Ack

    rig = _resuming(redis, monkeypatch, reaped=reaped, recovered=False)
    assert rig.consumer.resume(_order()) == Ack(FAILED, {"host_id": HIVE}, failure)


@pytest.mark.feature("hub-dead-resume")
def test_a_resume_answers_done_on_this_hive_and_records_the_new_pane(
    redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive.orders import DONE, Ack

    rig = _resuming(redis, monkeypatch)
    assert rig.consumer.resume(_order()) == Ack(DONE, {"host_id": HIVE})
    expected = replace(rig.record, pane_id="new", launched_at=7, idle_ticks=0)
    rig.store.put_agent.assert_called_once_with("rig", expected)
    rig.runtime.terminate.assert_not_called()


@pytest.mark.feature("hub-dead-resume")
def test_a_resume_that_cannot_record_its_pane_closes_the_new_pane(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _resuming(redis, monkeypatch)
    rig.store.put_agent.side_effect = RuntimeError("redis gone")
    with pytest.raises(RuntimeError):
        rig.consumer.resume(_order())
    rig.runtime.terminate.assert_called_once_with(replace(rig.record, pane_id="new"))


@pytest.mark.feature("hub-dead-resume")
def test_a_resume_without_a_launch_time_records_the_store_clock(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import orders
    from scripts.hive.orders import DONE, Ack

    rig = _resuming(redis, monkeypatch)
    rig.runtime.recover.return_value = replace(
        rig.runtime.recover.return_value, value=SimpleNamespace(pane_id="new", launched_at=0)
    )
    monkeypatch.setattr(orders.lease, "now_ms", lambda store: 42 if store is rig.store else -1)
    assert rig.consumer.resume(_order()) == Ack(DONE, {"host_id": HIVE})
    assert rig.store.put_agent.call_args.args[1].launched_at == 42


@pytest.mark.feature("hub-dead-resume")
def test_a_hive_runs_its_resume_orders_off_its_heartbeat_by_default(redis: Any) -> None:
    from scripts.hive.orders import Consumer

    consumer = Consumer(redis, HIVE, cast(Any, Mock()))
    assert consumer.offbeat == consumer._spawn_resume


@pytest.mark.feature("hub-dead-resume")
def test_one_poll_starts_every_unanswered_resume_order(redis: Any) -> None:
    from scripts.hive.orders import Consumer, Orders

    first, second = _order(), replace(_order(), id="resume:x:1:2")
    Orders(redis).command(HIVE, first)
    Orders(redis).command(HIVE, second)
    consumer = Consumer(redis, HIVE, cast(Any, Mock()))
    consumer.offbeat = Mock()
    consumer.poll_commands()
    assert [call.args[0] for call in consumer.offbeat.call_args_list] == [first, second]


@pytest.mark.feature("hive-resume-start-failure")
def test_a_resume_order_process_that_cannot_start_is_answered_failed_by_the_command_poll(
    redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import orders

    first, second = _order(), replace(_order(), id="resume:x:1:2")
    orders.Orders(redis).command(HIVE, first)
    orders.Orders(redis).command(HIVE, second)
    monkeypatch.setattr(orders.subprocess, "Popen", Mock(side_effect=OSError(11, "Resource temporarily unavailable")))
    consumer = orders.Consumer(redis, HIVE, cast(Any, Mock()))
    assert consumer.poll_commands() == 2
    failed = {"status": "failed", "target": {"host_id": HIVE}, "failure": "error: OSError"}
    assert [_ack(redis, first), _ack(redis, second)] == [failed, failed]
    assert consumer.poll_commands() == 2
    assert redis.xlen(orders.Orders(redis).commands(HIVE)) == 0


@pytest.mark.feature("hub-dead-resume")
def test_the_resume_order_process_runs_the_order_on_its_own_redis(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import orders, resume_order
    from scripts.swarm import store

    monkeypatch.setattr(store, "redis_client", lambda: redis)
    monkeypatch.setattr(redis, "close", lambda: None)
    seen: list[tuple[Any, Any]] = []

    def local(cls: Any, client: Any, hive: str) -> Any:
        consumer = orders.Consumer(client, hive, cast(Any, Mock()))
        consumer.run_resume = lambda order: seen.append((client, order))
        return consumer

    monkeypatch.setattr(orders.Consumer, "local", classmethod(local))
    order = _order()
    resume_order.run(HIVE, order.encode())
    assert seen == [(redis, order)]
