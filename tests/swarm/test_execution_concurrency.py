import threading
from dataclasses import replace

import pytest

from scripts.swarm import execution
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    return store


def sibling(store, seat):
    return AgentRecord(store.next_name("fixture", "eng"), "eng", "task", seat=seat, started_at=123)


@pytest.mark.parametrize("operation", ["start", "update", "pane"])
def test_sibling_writes_wait_for_an_admission_in_flight(store, monkeypatch, operation):
    current = store.start_execution("fixture", sibling(store, "eng-1@fixture"))
    entered, release = threading.Event(), threading.Event()
    check_uid = execution.ExecutionRegistry.check_uid

    def held(self, slug, agent, reader):
        if not entered.is_set() and agent.seat == "eng-2@fixture":
            entered.set()
            assert release.wait(5)
        return check_uid(self, slug, agent, reader)

    monkeypatch.setattr(execution.ExecutionRegistry, "check_uid", held)
    admitted = []
    first = threading.Thread(
        target=lambda: admitted.append(store.start_execution("fixture", sibling(store, "eng-2@fixture")))
    )
    writes = {
        "start": lambda: store.start_execution("fixture", sibling(store, "eng-3@fixture")),
        "update": lambda: store.put_agent("fixture", replace(current, idle_ticks=2)),
        "pane": lambda: store.execution_registry.set_pane("fixture", current.execution_id, "w2:p7"),
    }
    second = threading.Thread(target=writes[operation])
    first.start()
    assert entered.wait(5)
    second.start()
    second.join(0.5)
    blocked = second.is_alive()
    release.set()
    first.join(5)
    second.join(5)
    assert blocked
    assert not first.is_alive() and not second.is_alive()
    assert [agent.generation for agent in admitted] == [1]


def test_another_swarm_writes_while_an_admission_is_in_flight(store, monkeypatch):
    store.create(SwarmConfig("other", "agentihooks", 1, 0))
    entered, release = threading.Event(), threading.Event()
    check_uid = execution.ExecutionRegistry.check_uid

    def held(self, slug, agent, reader):
        if slug == "fixture":
            entered.set()
            assert release.wait(5)
        return check_uid(self, slug, agent, reader)

    monkeypatch.setattr(execution.ExecutionRegistry, "check_uid", held)
    first = threading.Thread(target=lambda: store.start_execution("fixture", sibling(store, "eng-1@fixture")))
    other = AgentRecord(store.next_name("other", "eng"), "eng", "task", seat="eng-1@other", started_at=123)
    admitted = []
    second = threading.Thread(target=lambda: admitted.append(store.start_execution("other", other)))
    first.start()
    assert entered.wait(5)
    second.start()
    second.join(5)
    finished = not second.is_alive()
    release.set()
    first.join(5)
    assert finished
    assert [agent.seat for agent in admitted] == ["eng-1@other"]
