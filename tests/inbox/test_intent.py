import threading
import time

import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.inbox import intent, pool
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

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
        return DecisionResult(answers={name: Answer(type="noul", noul=noul) for name in questions}, source="liquid-d1")

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

    def slow(new, recent):
        release.wait(5)
        return 0.9

    _decider(grouping, slow)
    try:
        items = [inbox.send("swarm", FRONT, text) for text in ("deploy one", "deploy two", "deploy three")]
    finally:
        release.set()
    assert [inbox.get(i.id).address for i in items] == [FRONT, SECOND, FRONT]
    assert [inbox.get(i.id).state for i in items] == ["pending"] * 3
    assert swarm.redis.exists(f"{pool.PREFIX}:sw") == 0


@pytest.mark.wall_clock
def test_a_slow_classifier_holds_a_send_only_until_its_deadline(swarm, inbox, grouping):
    release = threading.Event()
    grouping.setenv(intent.TIMEOUT_ENV, "0.05")
    _decider(grouping, lambda new, recent: release.wait(5))
    swarm.redis.rpush(f"{pool.PREFIX}:sw:recent:{SECOND}", "deploy one")
    started = time.monotonic()
    try:
        inbox.send("swarm", FRONT, "deploy two")
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 2


def test_a_classifier_error_falls_back_to_round_robin(swarm, inbox, grouping):
    def unavailable(state, questions, *, purpose, fallbacks):
        raise ClassifierUnavailable("no decision backend answered")

    grouping.setattr(intent, "decide", unavailable)
    assert _addresses(inbox, ["deploy one", "deploy two", "deploy three"]) == [FRONT, SECOND, FRONT]
    caches = swarm.redis.keys(f"{pool.PREFIX}:sw:intent:*")
    assert len(caches) == 2
    assert [set(swarm.redis.hkeys(cache)) for cache in caches] == [{intent.TOKENS}] * 2


def test_an_unreadable_timeout_setting_keeps_the_default_deadline(swarm, inbox, grouping):
    grouping.setenv(intent.TIMEOUT_ENV, "soon")
    _decider(grouping, _same_topic)
    swarm.redis.rpush(f"{pool.PREFIX}:sw:recent:{SECOND}", "deploy one")
    assert intent.timeout_s() == intent.DEFAULT_TIMEOUT_S
    assert _addresses(inbox, ["deploy two"]) == [SECOND]


def test_flag_off_asks_no_classifier_and_keeps_no_history(swarm, inbox, monkeypatch):
    monkeypatch.setenv(intent.FLAG, "false")
    calls = _decider(monkeypatch, _same_topic)
    assert _addresses(inbox, ["deploy one", "deploy two", "deploy three"]) == [FRONT, SECOND, FRONT]
    assert calls == []
    assert swarm.redis.keys(f"{pool.PREFIX}:sw:recent:*") == []


@pytest.mark.parametrize(("off", "on"), [("", "1"), ("0", " true "), ("false", "YES"), ("off", " On "), ("no", "on")])
def test_grouping_is_on_unless_the_flag_says_otherwise(monkeypatch, off, on):
    monkeypatch.setenv(intent.FLAG, off)
    assert intent.enabled() is False
    monkeypatch.setenv(intent.FLAG, on)
    assert intent.enabled() is True
    monkeypatch.delenv(intent.FLAG)
    assert intent.enabled() is True


def _cache(swarm, item_id):
    return swarm.redis.hgetall(f"{pool.PREFIX}:sw:intent:{item_id}")


def _repool(inbox, item_id, holder):
    inbox.return_to_pool(item_id, holder)
    inbox.pool.distribute(SLUG)
    return inbox.get(item_id).address


def _long_item(swarm, inbox):
    long_line = "investigate the failing merge queue run " * 10
    recent = [intent.first_line(long_line)] * intent.RECENT
    for seat in (FRONT, SECOND):
        swarm.redis.rpush(f"{pool.PREFIX}:sw:recent:{seat}", *recent)
    item = inbox.send("swarm", FRONT, long_line + "\n" + "body " * 2000)
    return item, intent.request(recent[0], recent).estimated_tokens()


def test_each_item_sends_only_first_lines_within_its_token_budget_and_is_cached(swarm, inbox, grouping):
    calls = _decider(grouping, lambda new, recent: 0.1)
    item, sent = _long_item(swarm, inbox)

    assert inbox.get(item.id).address == FRONT
    assert _cache(swarm, item.id)[intent.TOKENS] == str(2 * sent)
    assert sent <= TOKENS_PER_MASTER
    assert [len(state["new"]) for state, _, _ in calls] == [intent.LINE_CHARS] * 2
    assert _repool(inbox, item.id, FRONT) == SECOND
    assert len(calls) == 2
    assert _cache(swarm, item.id)[intent.TOKENS] == str(2 * sent)


def test_an_unreadable_cached_verdict_asks_again(swarm, inbox, grouping):
    calls = _decider(grouping, lambda new, recent: 0.1)
    item, sent = _long_item(swarm, inbox)
    cache = f"{pool.PREFIX}:sw:intent:{item.id}"
    for field in swarm.redis.hkeys(cache):
        if field != intent.TOKENS:
            swarm.redis.hset(cache, field, "unreadable")

    assert _repool(inbox, item.id, FRONT) == SECOND
    assert len(calls) == 4
    assert _cache(swarm, item.id)[intent.TOKENS] == str(4 * sent)


def test_a_changed_history_asks_again(swarm, inbox, grouping):
    calls = _decider(grouping, _same_topic)
    swarm.redis.rpush(f"{pool.PREFIX}:sw:recent:{FRONT}", "beta zero")
    swarm.redis.rpush(f"{pool.PREFIX}:sw:recent:{SECOND}", "alpha one")
    item = inbox.send("swarm", FRONT, "alpha two")

    assert inbox.get(item.id).address == SECOND
    assert _repool(inbox, item.id, SECOND) == FRONT
    assert sorted(state["recent"] for state, _, _ in calls) == [
        ["alpha one"],
        ["alpha two", "alpha one"],
        ["beta zero"],
    ]


@pytest.mark.parametrize(
    ("front", "second", "expected"),
    [
        (0.75, 0.95, SECOND),
        (0.95, 0.75, FRONT),
        (intent.MATCH, 0.2, FRONT),
        (intent.MATCH - 0.01, 0.2, SECOND),
    ],
)
def test_the_best_verdict_at_or_above_the_floor_wins(swarm, inbox, grouping, front, second, expected):
    _decider(grouping, lambda new, recent: front if recent == ["front work"] else second)
    swarm.redis.rpush(f"{pool.PREFIX}:sw:recent:{FRONT}", "front work")
    swarm.redis.rpush(f"{pool.PREFIX}:sw:recent:{SECOND}", "second work")
    swarm.redis.set(f"{pool.PREFIX}:sw:last", FRONT)
    assert _addresses(inbox, ["any work"]) == [expected]


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


def test_the_classifier_runs_one_thread_per_ask_up_to_its_cap(swarm, inbox, grouping):
    sizes = []
    real = intent.ThreadPoolExecutor

    def sized(workers):
        sizes.append(workers)
        return real(workers)

    grouping.setattr(intent, "ThreadPoolExecutor", sized)
    _decider(grouping, lambda new, recent: 0.1)
    for seat in (FRONT, SECOND):
        swarm.redis.rpush(f"{pool.PREFIX}:sw:recent:{seat}", "earlier work")
    grouping.setattr(intent, "MAX_WORKERS", 1)
    inbox.send("swarm", FRONT, "more work")
    grouping.setattr(intent, "MAX_WORKERS", 16)
    inbox.send("swarm", FRONT, "other work")
    assert sizes == [1, 2]
