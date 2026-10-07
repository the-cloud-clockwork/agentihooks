import fakeredis
import pytest

from scripts.swarm import retire_watch
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

NAME = "engineer@a1b2c3-0001"
REFUSED = {"process": 77, "refusal": "survived SIGKILL: 77"}


@pytest.fixture
def store():
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
