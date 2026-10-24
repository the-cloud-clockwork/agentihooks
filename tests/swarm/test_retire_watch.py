import pytest

from scripts.swarm import retire_watch
from scripts.swarm.store import RedisStore, SwarmConfig

NAME = "engineer@a1b2c3-0001"
REFUSED = {"process": 77, "refusal": "survived SIGKILL: 77"}


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    return s


def test_each_failed_tick_counts_and_keeps_the_first_time(store):
    assert retire_watch.failed(store, "sw", NAME, REFUSED, 10) == 1
    assert retire_watch.failed(store, "sw", NAME, {"process": 78, "refusal": "pane w1:p1 did not close"}, 20) == 2
    assert retire_watch.rows(store, "sw") == [
        {"agent": NAME, "ticks": 2, "since": 10, "at": 20, "process": 78, "refusal": "pane w1:p1 did not close"}
    ]


def test_the_finding_appears_on_the_third_failed_tick(store):
    for at in (1, 2):
        retire_watch.failed(store, "sw", NAME, REFUSED, at)
    assert retire_watch.findings(store, "sw") == []
    retire_watch.failed(store, "sw", NAME, REFUSED, 3)
    (found,) = retire_watch.findings(store, "sw")
    assert found.as_dict() == {
        "kind": "retire failed",
        "subject": NAME,
        "summary": f"{NAME} is still running after 3 retire attempts",
        "evidence": ["process 77", "refusal: survived SIGKILL: 77"],
        "threshold": "a retire failing 3 ticks",
    }
    assert found.measure == 3


def test_a_dropped_agent_clears_its_failures(store):
    for at in (1, 2, 3):
        retire_watch.failed(store, "sw", NAME, REFUSED, at)
    store.drop_agent("sw", NAME, at=4)
    assert retire_watch.rows(store, "sw") == [] and retire_watch.findings(store, "sw") == []


@pytest.mark.parametrize("projection", [True, False])
def test_retry_of_an_ended_agent_without_process_or_pane_releases_its_seat(store, tmp_path, monkeypatch, projection):
    from dataclasses import replace

    from scripts.swarm.tick import _retire_ended
    from scripts.swarm_v2.runtime.base import RuntimeRouter
    from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
    from scripts.swarm_v2.runtime.routed import RoutedRuntime
    from tests.swarm.test_tick import FakeLedger
    from tests.test_swarm_v2_process import ANTON, record
    from tests.test_swarm_v2_runtime import herdr_runtime

    agent = replace(record(pid=None, start=None), state="finished", seat="eng-1@sw")
    assert store.claim("sw", "t1", agent.name, 60_000)
    store.put_agent("sw", agent)
    if not projection:
        store.drop_agent("sw", agent.name)
    retire_watch.failed(store, "sw", agent.name, {"process": 0, "refusal": "runtime target names no process"}, 1)
    herdr, calls = herdr_runtime(tmp_path, monkeypatch, panes={})
    monkeypatch.setattr(herdr, "live_names", lambda: set())
    local = LocalHerdrRuntime(herdr, namespace=lambda: ANTON, table=lambda: {})
    runtime = RoutedRuntime(RuntimeRouter([local]), herdr)
    ledger = FakeLedger([{"id": "t1", "state": "done", "done": True}])

    assert _retire_ended("sw", store, ledger, runtime, ledger.rows, agent, 2) == [f"retired {agent.name}"]
    assert store.claimant("sw", "t1") is None
    assert store.agents("sw") == []
    assert not store.execution_registry.occupants("sw")
    assert retire_watch.rows(store, "sw") == []
    assert ["pane", "close", "w1:p1"] not in calls


def test_failures_are_kept_per_swarm(store):
    for at in (1, 2, 3):
        retire_watch.failed(store, "other", NAME, REFUSED, at)
    assert retire_watch.findings(store, "sw") == []


def test_swarm_status_lists_the_retire_finding(store, monkeypatch):
    from scripts.swarm import status

    monkeypatch.setattr(status.activity, "counts", lambda slug: {})
    for at in (1, 2, 3):
        retire_watch.failed(store, "sw", NAME, REFUSED, at)
    found = status.findings(store, "sw", store.config("sw"), [], [])
    assert [f["id"] for f in found] == [f"retire-failed/{NAME}"]
