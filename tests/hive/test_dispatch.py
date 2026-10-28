import json
import subprocess
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

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
        thread = threading.Thread(target=lambda: dispatch.run(*argv[-2:]))
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
    assert setup.orders.ack(setup.order).failure == "canary failed"
