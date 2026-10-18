import threading

import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.inbox import intent, pool
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "sw"
FRONT = "master@sw"
SECOND = "master-2@sw"
TOKENS_PER_MASTER = 700


@pytest.fixture
def swarm():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1))
    for index, seat in enumerate((FRONT, SECOND), start=1):
        store.put_agent(SLUG, AgentRecord(f"sw-master-{index}", "master", "master", seat=seat, state="working"))
        store.seats.occupy(seat, f"sw-master-{index}", at=1)
    return store


@pytest.fixture
def inbox(swarm):
    return InboxStore(swarm.redis, session_id="s1")


@pytest.fixture
def grouping(monkeypatch):
    monkeypatch.setenv(intent.FLAG, "true")
    return monkeypatch


def _decider(monkeypatch, verdict):
    calls = []

    def decide(state, questions, *, purpose, fallbacks):
        calls.append((state, purpose, fallbacks))
        noul = verdict(state["new"], state["recent"])
        return DecisionResult(answers={"same": Answer(type="noul", noul=noul)}, source="liquid-d1")

    monkeypatch.setattr(intent, "decide", decide)
    return calls


def _topic(line):
    return line.split()[0]


def _same_topic(new, recent):
    return 0.9 if any(_topic(line) == _topic(new) for line in recent) else 0.1


def _addresses(inbox, texts):
    return [inbox.get(inbox.send("swarm", FRONT, text).id).address for text in texts]


def test_a_matching_item_goes_to_the_master_that_handled_its_intent(swarm, inbox, grouping):
    calls = _decider(grouping, _same_topic)
    texts = ["queue one is stuck\nmore detail", "deploy two failed", "deploy three is red"]
    assert _addresses(inbox, texts) == [FRONT, SECOND, SECOND]
    assert swarm.redis.lrange(f"{pool.PREFIX}:sw:recent:{SECOND}", 0, -1) == [
        "deploy three is red",
        "deploy two failed",
    ]
    assert swarm.redis.lrange(f"{pool.PREFIX}:sw:recent:{FRONT}", 0, -1) == ["queue one is stuck"]
    assert {(purpose, fallbacks) for _, purpose, fallbacks in calls} == {(intent.PURPOSE, ())}
    assert {state["new"] for state, _, _ in calls} == {"deploy two failed", "deploy three is red"}


def test_no_match_keeps_the_round_robin_turn(swarm, inbox, grouping):
    calls = _decider(grouping, lambda new, recent: 0.2)
    assert _addresses(inbox, ["alpha one", "alpha two", "alpha three"]) == [FRONT, SECOND, FRONT]
    assert len(calls) == 3


def test_a_verdict_below_the_match_floor_keeps_the_round_robin_turn(swarm, inbox, grouping):
    _decider(grouping, lambda new, recent: intent.MATCH - 0.01)
    assert _addresses(inbox, ["alpha one", "alpha two", "alpha three"]) == [FRONT, SECOND, FRONT]


def test_a_classifier_timeout_falls_back_to_round_robin_without_holding_the_item(swarm, inbox, grouping):
    release = threading.Event()
    grouping.setenv(intent.TIMEOUT_ENV, "0.05")
    _decider(grouping, lambda new, recent: release.wait(5) and 0.9)
    try:
        items = [inbox.send("swarm", FRONT, text) for text in ("deploy one", "deploy two", "deploy three")]
    finally:
        release.set()
    assert [inbox.get(i.id).address for i in items] == [FRONT, SECOND, FRONT]
    assert [inbox.get(i.id).state for i in items] == ["pending"] * 3
    assert swarm.redis.exists(f"{pool.PREFIX}:sw") == 0


def test_a_classifier_error_falls_back_to_round_robin(swarm, inbox, grouping):
    def unavailable(state, questions, *, purpose, fallbacks):
        raise ClassifierUnavailable("no decision backend answered")

    grouping.setattr(intent, "decide", unavailable)
    assert _addresses(inbox, ["deploy one", "deploy two", "deploy three"]) == [FRONT, SECOND, FRONT]
    assert swarm.redis.keys(f"{pool.PREFIX}:sw:intent:*") == []


def test_flag_off_asks_no_classifier_and_keeps_no_history(swarm, inbox, monkeypatch):
    monkeypatch.delenv(intent.FLAG, raising=False)
    calls = _decider(monkeypatch, _same_topic)
    assert _addresses(inbox, ["deploy one", "deploy two", "deploy three"]) == [FRONT, SECOND, FRONT]
    assert calls == []
    assert swarm.redis.keys(f"{pool.PREFIX}:sw:recent:*") == []


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no"])
def test_only_a_true_flag_turns_grouping_on(monkeypatch, value):
    monkeypatch.setenv(intent.FLAG, value)
    assert intent.enabled() is False
    monkeypatch.setenv(intent.FLAG, " On ")
    assert intent.enabled() is True


def test_each_item_sends_only_first_lines_within_its_token_budget_and_is_cached(swarm, inbox, grouping):
    calls = _decider(grouping, lambda new, recent: 0.1)
    long_line = "investigate the failing merge queue run " * 10
    for seat in (FRONT, SECOND):
        swarm.redis.rpush(f"{pool.PREFIX}:sw:recent:{seat}", *[intent.first_line(long_line)] * intent.RECENT)
    item = inbox.get(inbox.send("operator", FRONT, long_line + "\n" + "body " * 2000).id)
    live = pool.live_masters(swarm.redis, SLUG)

    routes = intent.routes(inbox.pool, SLUG, [item], live)

    recent = [intent.first_line(long_line)] * intent.RECENT
    sent = intent.request(intent.first_line(long_line), recent).estimated_tokens()
    assert routes == {item.id: intent.Route("", 2 * sent)}
    assert 0 < sent <= TOKENS_PER_MASTER
    assert all(len(state["new"]) == intent.LINE_CHARS for state, _, _ in calls)
    assert len(calls) == 2
    assert intent.routes(inbox.pool, SLUG, [item], live) == {item.id: intent.Route("", 0)}
    assert len(calls) == 2


def test_a_changed_history_asks_again(swarm, inbox, grouping):
    calls = _decider(grouping, _same_topic)
    swarm.redis.rpush(f"{pool.PREFIX}:sw:recent:{SECOND}", "alpha one")
    item = inbox.get(inbox.send("operator", FRONT, "alpha two").id)
    live = pool.live_masters(swarm.redis, SLUG)
    assert intent.routes(inbox.pool, SLUG, [item], live)[item.id].seat == SECOND
    swarm.redis.lpush(f"{pool.PREFIX}:sw:recent:{SECOND}", "beta three")
    assert intent.routes(inbox.pool, SLUG, [item], live)[item.id].seat == SECOND
    assert len(calls) == 2


def test_the_best_verdict_wins_between_matching_masters():
    verdicts = {("i", FRONT): 0.75, ("i", SECOND): 0.95}
    assert intent._best("i", {FRONT: ["a"], SECOND: ["b"]}, verdicts) == SECOND
    assert intent._best("i", {FRONT: ["a"]}, verdicts) == FRONT
    assert intent._best("j", {FRONT: ["a"]}, verdicts) == ""


def test_first_line_trims_and_bounds_the_text():
    assert intent.first_line("  \n  hello there  \nsecond") == "hello there"
    assert intent.first_line("") == ""
    assert len(intent.first_line("x" * 500)) == intent.LINE_CHARS


def test_a_master_keeps_only_its_last_fifteen_items(swarm, inbox, grouping):
    _decider(grouping, lambda new, recent: 0.1)
    for n in range(40):
        inbox.send("swarm", FRONT, f"item {n}")
    recent = swarm.redis.lrange(f"{pool.PREFIX}:sw:recent:{FRONT}", 0, -1)
    assert recent == [f"item {n}" for n in range(38, 8, -2)][: intent.RECENT]
    assert 0 < swarm.redis.ttl(f"{pool.PREFIX}:sw:recent:{FRONT}") <= intent.RECENT_TTL_S
