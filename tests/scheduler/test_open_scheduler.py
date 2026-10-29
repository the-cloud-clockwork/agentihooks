import socket

import pytest

from scripts.scheduler.core import Scheduler, open_scheduler

pytestmark = pytest.mark.unit


def test_the_opened_scheduler_runs_on_the_swarm_store_connection_for_this_machine(monkeypatch):
    from scripts.swarm import store

    client = object()
    monkeypatch.setattr(store, "redis_client", lambda: client)
    opened = open_scheduler()
    assert type(opened) is Scheduler
    assert opened.redis is client
    assert opened.machine == socket.gethostname()
