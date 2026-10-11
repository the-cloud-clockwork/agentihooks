import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.swarm import commands, execution_card
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_v2.herdr import visibility
from scripts.swarm_v2.herdr.visibility import View

pytestmark = pytest.mark.xdist_group("fakeredis")

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "swarm_v2" / "ledger-execution-cards.json").read_text())
NOW = FIXTURE["now_ms"]


def new_store(slug):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(slug, ".", 0, 0, state="running"))
    return store


def seeded(slug):
    store = new_store(slug)
    commands.publish(store, slug, FIXTURE["status"], {})
    visibility.publish(store, slug, [View(**row) for row in FIXTURE["terminal_views_published_after_the_heartbeat"]])
    return store


def read(store, slug):
    with patch.object(commands, "time", SimpleNamespace(time_ns=lambda: NOW * 1_000_000)):
        return commands.view(store, slug)


def without_agent(cards):
    return {name: {k: v for k, v in card.items() if k != "agent"} for name, card in cards.items()}


def snapshot(store):
    return {key: store.redis.dump(key) for key in sorted(store.redis.keys("*"))}


def test_local_and_remote_agents_read_from_the_same_view():
    for slug in ("sv2-ux-01-a", "sv2-ux-01-a2"):
        view = read(seeded(slug), slug)
        assert without_agent(view["executions"]) == FIXTURE["expected"]
        assert view["ledger_state_projection_errors"] == 1
        assert [a["name"] for a in view["agents"]] == [a["name"] for a in FIXTURE["status"]["agents"]]


def test_a_stale_terminal_view_never_overwrites_the_fresher_heartbeat_state():
    slug = "sv2-ux-01-b"
    store = seeded(slug)
    before = snapshot(store)
    card = read(store, slug)["executions"]["eng-2"]
    assert (card["observed"], card["terminal_behind"], card["seen_at"]) == ("working", True, 1791299880000)
    assert snapshot(store) == before
    newer = execution_card.card(FIXTURE["status"]["agents"][3], {"checked_at": 1791299990000})
    assert newer["terminal_behind"] is False


def test_a_terminal_source_never_counts_as_seen():
    agent = {"name": "eng-9", "runtime_backend": "kubernetes", "generation": 4, "profile": "standard"}
    only_terminal = {"state": "suspect", "sources": {"terminal": {"reading": "ok", "observed_at": 1791299999.0}}}
    card = execution_card.card({**agent, "observation": only_terminal}, None)
    assert (card["seen_at"], card["observed"], card["wait"]) == (0, "suspect", "")


def test_an_agent_without_any_observation_reads_unobserved_and_local():
    card = execution_card.card({"name": "eng-8"}, None)
    assert card == {
        "agent": "eng-8",
        "backend": "local",
        "generation": 0,
        "pool": "",
        "observed": "unobserved",
        "wait": "",
        "seen_at": 0,
        "terminal_behind": False,
    }


def test_a_nameless_row_counts_as_a_projection_error_and_names_no_card():
    built, errors = execution_card.cards([{"lane": "eng"}, "not a row"], {})
    assert (built, errors) == ({}, 2)


def test_reloading_the_view_restores_the_latest_state_without_replaying_anything():
    slug = "sv2-ux-01-c"
    store = seeded(slug)
    first = read(store, slug)
    before = snapshot(store)
    restarted = RedisStore(store.redis)
    again = read(restarted, slug)
    assert again["executions"] == first["executions"]
    assert again["commands"] == first["commands"] == []
    assert snapshot(store) == before


def test_an_unreadable_row_without_a_backend_never_claims_local():
    built, errors = execution_card.cards([{"name": "eng-7", "observation": {"sources": {"heartbeat": 3}}}], {})
    assert errors == 1
    assert (built["eng-7"]["backend"], built["eng-7"]["observed"]) == ("unknown", "unreadable")


def observed(**observation):
    return {"name": "eng-9", "runtime_backend": "kubernetes", "generation": 1, "observation": observation}


@pytest.mark.parametrize(
    ("observation", "wait"),
    [
        ({"failure": "pod_scheduling", "state": "suspect"}, "scheduling"),
        ({"failure": "none", "state": "pending"}, "scheduling"),
        ({"failure": "provider_wait", "state": "suspect"}, "quota"),
        ({"failure": "none", "state": "waiting_quota"}, "quota"),
        ({"failure": "none", "state": "working"}, ""),
    ],
)
def test_either_the_failure_or_the_state_alone_names_the_wait(observation, wait):
    assert execution_card.card(observed(**observation), None)["wait"] == wait


def test_a_source_seen_at_time_zero_reads_as_never_seen():
    beat = {"heartbeat": {"reading": "ok", "observed_at": 0, "value": "working"}}
    assert execution_card.card(observed(state="working", sources=beat), None)["seen_at"] == 0


def test_a_terminal_view_is_behind_only_when_checked_strictly_before_the_heartbeat():
    beat = {"heartbeat": {"reading": "ok", "observed_at": 0.001, "value": "working"}}
    agent = observed(state="working", sources=beat)
    assert execution_card.card(agent, {"checked_at": 1})["terminal_behind"] is False
    assert execution_card.card(agent, {"state": "unverified"})["terminal_behind"] is True


def test_without_a_heartbeat_no_terminal_view_is_behind():
    pod = {"kubernetes": {"reading": "ok", "observed_at": 5.0, "value": "Running"}}
    assert execution_card.card(observed(state="working", sources=pod), {"checked_at": 0})["terminal_behind"] is False


def test_a_remote_agent_without_a_profile_names_no_pool():
    assert execution_card.card(observed(state="working"), None)["pool"] == ""


def test_every_unreadable_row_is_counted():
    rows = [{"name": name, "observation": {"sources": {"heartbeat": 3}}} for name in ("eng-5", "eng-6", "eng-7")]
    assert execution_card.cards(rows, {})[1] == 3
