import pytest

from hooks.context import swarm_heartbeat
from scripts.swarm import idle
from scripts.swarm.store import SwarmError

pytestmark = pytest.mark.xdist_group("fakeredis")

NOW = 10_000_000


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def beat(state, age_ms=0):
    return {"state": state, "at": NOW - age_ms}


@pytest.mark.parametrize(
    "pane, heartbeat, wait, expected",
    [
        ("working", None, None, idle.WORKING),
        ("unknown", None, None, idle.WORKING),
        ("idle", None, None, idle.IDLE),
        ("done", beat("idle"), None, idle.IDLE),
        ("idle", beat("working"), None, idle.WORKING),
        ("idle", beat("working", idle.STALE_MS + 1), None, idle.IDLE),
        ("idle", beat("idle"), {"until": NOW + 1}, idle.WAITING),
        ("idle", beat("idle"), {"until": NOW}, idle.IDLE),
        ("working", beat("idle"), {"until": NOW + 1}, idle.WORKING),
    ],
)
def test_an_agent_is_idle_only_when_pane_heartbeat_and_wait_all_say_so(pane, heartbeat, wait, expected):
    assert idle.verdict(pane, heartbeat, wait, NOW) == expected


def test_a_recorded_heartbeat_and_wait_are_read_back(redis):
    idle.beat(redis, "sw", "sw-eng-1", idle.WORKING, NOW)
    idle.declare_wait(redis, "sw", "sw-eng-1", NOW + 60_000, "deploy run", NOW)
    assert idle.heartbeat(redis, "sw", "sw-eng-1") == {"state": "working", "at": NOW}
    assert idle.wait(redis, "sw", "sw-eng-1") == {"until": NOW + 60_000, "reason": "deploy run", "at": NOW}
    assert idle.heartbeat(redis, "sw", "sw-eng-2") is None and idle.wait(redis, "sw", "sw-eng-2") is None


def test_a_wait_must_end_in_the_future(redis):
    with pytest.raises(SwarmError):
        idle.declare_wait(redis, "sw", "sw-eng-1", NOW, "", NOW)


def test_the_hook_heartbeat_is_written_only_for_a_swarm_agent(redis):
    swarm = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "sw-eng-1"}
    assert swarm_heartbeat.beat(idle.IDLE, environ=swarm, redis=redis, now_ms=NOW) is True
    assert idle.heartbeat(redis, "sw", "sw-eng-1") == {"state": "idle", "at": NOW}
    assert swarm_heartbeat.beat(idle.WORKING, environ={"AGENTIHOOKS_AGENT_NAME": "x"}, redis=redis) is False
