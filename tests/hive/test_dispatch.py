import subprocess
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.hive.test_orders import setup as setup

pytestmark = pytest.mark.unit


def test_blocked_validation_cannot_delay_retirement_heartbeat_or_a_later_launch(setup, monkeypatch):
    from scripts.hive import daemon, join_report
    from scripts.hive.orders import CommandOrder, Consumer
    from scripts.swarm_v2.runtime.base import Outcome, Status

    entered, release, second, reported = (threading.Event() for _ in range(4))
    failures, children = [], []
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "host")
    monkeypatch.setattr(Consumer, "local", lambda *args: setup.consumer)
    monkeypatch.setattr(
        daemon,
        "observe",
        lambda **kwargs: {
            "slots": {},
            "interactive": {},
            "repos": [],
            "sessions": {},
            "quota": {},
        },
    )
    monkeypatch.setattr(
        daemon,
        "execution_report",
        lambda *args: {
            "names": [],
            "panes": {},
            "executions": {},
        },
    )
    monkeypatch.setattr(join_report, "heartbeat", lambda *args: reported.set())

    def spawn(request):
        if request.name == setup.agent.name:
            entered.set()
            assert release.wait(10)
        else:
            second.set()
        return Outcome("spawn", Status.REFUSED, "local", detail="canary failed")

    setup.runtime.spawn.side_effect = spawn
    setup.runtime.terminate.return_value = Outcome("terminate", Status.OK, "local")

    def start(argv, **kwargs):
        from scripts.hive import dispatch
        from scripts.swarm import store

        monkeypatch.setattr(store, "redis_client", lambda: setup.redis)
        thread = threading.Thread(target=lambda: dispatch.run(*argv[-3:]))
        thread.start()
        children.append(thread)
        return SimpleNamespace(poll=lambda: None if thread.is_alive() else 0)

    monkeypatch.setattr(subprocess, "Popen", start)
    setup.orders.send("host", setup.order)

    def beat():
        try:
            daemon.beat(setup.redis, "host")
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=beat)
    worker.start()
    try:
        assert entered.wait(3)
        worker.join(1)
        assert not worker.is_alive(), "launch validation blocked the hive beat"
        assert reported.is_set()
        command = CommandOrder(
            "retire", "retire", "fixture", execution_id=setup.agent.execution_id, generation=setup.agent.generation
        )
        setup.orders.command("host", command)
        next_agent = setup.store.start_execution(
            "fixture",
            replace(
                setup.agent,
                name=setup.store.next_name("fixture", "eng"),
                seat="eng-2@fixture",
                execution_id="",
                generation=0,
            ),
        )
        later = replace(setup.order, execution_id=next_agent.execution_id, generation=next_agent.generation)
        setup.orders.send("host", later)
        reported.clear()
        beat()
        assert not failures
        assert reported.is_set()
        assert setup.orders.command("host", command).status == "done"
        assert second.wait(3), "later accepted launch waited for a slow canary"
        assert not release.is_set()
        assert setup.orders.ack(setup.order) is None
    finally:
        release.set()
        worker.join(10)
        for child in children:
            child.join(10)
    assert not failures
    assert setup.orders.ack(setup.order).failure == "refused: canary failed"


@pytest.fixture(autouse=True)
def workers(monkeypatch):
    from scripts.hive import dispatch

    monkeypatch.setattr(dispatch, "_workers", {})


def test_dispatch_keeps_live_children_and_retries_finished_children_without_relaunch(setup, monkeypatch):
    from unittest.mock import Mock

    from scripts.hive import dispatch

    process = Mock()
    process.poll.return_value = None
    start = Mock(return_value=process)
    monkeypatch.setattr(dispatch.subprocess, "Popen", start)
    setup.orders.send("host", setup.order)
    item, fields = setup.redis.xrange(setup.orders.stream("host"))[0]
    dispatch.poll(setup.consumer)
    start.assert_called_once_with(
        [dispatch.sys.executable, "-m", "scripts.hive.dispatch", "host", item, setup.consumer.pod],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    assert setup.redis.xlen(setup.orders.stream("host")) == 1
    dispatch.poll(setup.consumer)
    assert start.call_count == 1
    setup.consumer.consume(item, fields)
    assert setup.orders.ack(setup.order).status == "live"
    process.poll.return_value = 0
    dispatch.poll(setup.consumer)
    assert dispatch._workers == {}
    assert start.call_count == 1
    setup.runtime.spawn.assert_called_once()


def test_dispatch_capacity_never_queues_children_or_drops_pending_orders(setup, monkeypatch):
    from unittest.mock import Mock

    from scripts.hive import dispatch

    start = Mock(return_value=SimpleNamespace(poll=lambda: None))
    monkeypatch.setattr(dispatch.subprocess, "Popen", start)
    for _ in range(dispatch.LIMIT + 1):
        setup.orders.send("host", setup.order)
    dispatch.poll(setup.consumer)
    assert start.call_count == dispatch.LIMIT
    assert setup.redis.xlen(setup.orders.stream("host")) == dispatch.LIMIT + 1
    dispatch.poll(setup.consumer)
    assert start.call_count == dispatch.LIMIT


def test_dead_child_replay_keeps_the_single_application_fence(setup, monkeypatch):
    from unittest.mock import Mock

    from scripts.hive import dispatch
    from scripts.swarm import lease

    process = Mock()
    process.poll.return_value = None
    start = Mock(return_value=process)
    monkeypatch.setattr(dispatch.subprocess, "Popen", start)
    setup.orders.send("host", setup.order)
    dispatch.poll(setup.consumer)
    setup.redis.hset(setup.orders.receipt(setup.order), "started", lease.now_ms(setup.store))
    process.poll.return_value = 1
    dispatch.poll(setup.consumer)
    assert start.call_count == 2
    setup.consumer.poll()
    setup.runtime.spawn.assert_not_called()
    assert setup.orders.ack(setup.order) is None


def test_child_consumes_only_its_delivery_with_parent_pod_and_closes_redis(setup, monkeypatch):
    from unittest.mock import Mock

    from scripts.hive import dispatch
    from scripts.hive.orders import Consumer
    from scripts.swarm import store

    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "host")
    monkeypatch.setattr(store, "redis_client", lambda: setup.redis)
    monkeypatch.setattr(Consumer, "local", lambda *args: setup.consumer)
    closed = Mock()
    monkeypatch.setattr(setup.redis, "close", closed)
    setup.orders.send("host", setup.order)
    setup.orders.send("host", setup.order)
    item = setup.redis.xrange(setup.orders.stream("host"))[0][0]
    dispatch.run("host", item, "parent-pod")
    assert setup.consumer.pod == "parent-pod"
    assert setup.redis.hget(setup.orders.receipt(setup.order), "pod") == "parent-pod"
    assert setup.redis.xlen(setup.orders.stream("host")) == 1
    assert setup.orders.ack(setup.order).status == "live"
    setup.runtime.spawn.assert_called_once()
    closed.assert_called_once_with()


def test_child_closes_redis_even_when_consumption_fails(setup, monkeypatch):
    from unittest.mock import Mock

    from scripts.hive import dispatch
    from scripts.hive.orders import Consumer
    from scripts.swarm import store

    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "host")
    monkeypatch.setattr(store, "redis_client", lambda: setup.redis)
    monkeypatch.setattr(Consumer, "local", lambda *args: setup.consumer)
    closed = Mock()
    monkeypatch.setattr(setup.redis, "close", closed)
    setup.orders.send("host", setup.order)
    item = setup.redis.xrange(setup.orders.stream("host"))[0][0]
    setup.runtime.spawn.side_effect = RuntimeError("broken launcher")
    with pytest.raises(RuntimeError, match="broken launcher"):
        dispatch.run("host", item, "parent-pod")
    closed.assert_called_once_with()
