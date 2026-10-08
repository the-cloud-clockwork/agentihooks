import pytest

from hooks.context import swarm_heartbeat
from scripts.swarm import idle
from scripts.swarm.keyspace import ROOT as KEY_ROOT
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


def test_a_session_model_is_reported_only_by_a_swarm_agent_that_names_one(redis):
    from scripts.swarm import session_model

    swarm = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "sw-eng-1"}
    assert swarm_heartbeat.report("", "high", environ=swarm, redis=redis) is False
    assert swarm_heartbeat.report("opus", "high", environ={"AGENTIHOOKS_AGENT_NAME": "sw-eng-1"}, redis=redis) is False
    assert swarm_heartbeat.report("opus", "high", environ={"AGENTIHOOKS_SWARM": "sw"}, redis=redis) is False
    assert session_model.get(redis, "sw", "sw-eng-1") is None
    assert swarm_heartbeat.report("opus", "high", environ=swarm, redis=redis, now_ms=NOW) is True
    assert session_model.get(redis, "sw", "sw-eng-1") == {"model": "opus", "effort": "high", "at": NOW}
    assert 0 < redis.ttl(f"{KEY_ROOT}:swarm:sw:session-model:sw-eng-1") <= session_model.TTL_S


def test_a_codex_report_without_effort_reaches_the_swarm_redis_stamped_now(redis, monkeypatch):
    import time

    from scripts.swarm import session_model, store

    swarm = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "sw-eng-1"}
    monkeypatch.setattr(store, "redis_client", lambda environ: redis if environ is swarm else None)
    before = time.time_ns() // 1_000_000
    assert swarm_heartbeat.report("gpt-6.1-sol", environ=swarm) is True
    reported = session_model.get(redis, "sw", "sw-eng-1")
    assert (reported["model"], reported["effort"]) == ("gpt-6.1-sol", "")
    assert isinstance(reported["at"], int) and before <= reported["at"] <= time.time_ns() // 1_000_000


def test_the_hook_records_operator_prompts_but_not_the_ones_the_swarm_types(redis):
    from scripts.inbox.wake import WAKE_TEXT
    from scripts.swarm.tick import NUDGE

    swarm = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "sw-master-1"}
    assert swarm_heartbeat.heard(WAKE_TEXT, environ=swarm, redis=redis, now_ms=NOW) is False
    assert swarm_heartbeat.heard(NUDGE.format(slug="sw"), environ=swarm, redis=redis, now_ms=NOW) is False
    notice = "<task-notification>\n<task-id>b1</task-id>\n<summary>Monitor event</summary>\n</task-notification>"
    assert swarm_heartbeat.heard(notice, environ=swarm, redis=redis, now_ms=NOW) is False
    assert idle.last_prompt(redis, "sw", "sw-master-1") is None
    assert swarm_heartbeat.heard("hold on", environ=swarm, redis=redis, now_ms=NOW) is True
    assert idle.last_prompt(redis, "sw", "sw-master-1") == NOW
    assert redis.ttl(idle.key("sw", "prompt", "sw-master-1")) == idle.BEAT_TTL_S
    assert swarm_heartbeat.heard("hold on", environ={"AGENTIHOOKS_AGENT_NAME": "x"}, redis=redis) is False
    assert swarm_heartbeat.heard("hold on", environ={"AGENTIHOOKS_SWARM": "sw"}, redis=redis) is False


def test_the_hook_connects_with_the_session_environment_and_stamps_the_clock(redis, monkeypatch):
    from scripts.swarm import store

    seen = []
    monkeypatch.setattr(store, "redis_client", lambda env: seen.append(env) or redis)
    monkeypatch.setattr(swarm_heartbeat.time, "time_ns", lambda: 1_791_275_228_240_123_456)
    swarm = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "sw-master-1"}
    assert swarm_heartbeat.heard("hold on", environ=swarm) is True
    assert seen == [swarm] and idle.last_prompt(redis, "sw", "sw-master-1") == 1_791_275_228_240
