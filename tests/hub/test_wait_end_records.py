import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import idle, waits
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord, RedisStore

ME = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/7"
HELD = {"kind": "checks", "target": URL, "head": "first"}
OUTCOME = f"checks on {URL}, now green"


@pytest.mark.feature("waits-end-keeps-item-woke-and-history")
def test_the_polled_pass_ends_a_green_checks_wait_with_one_item_woke_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fakeredis

    monkeypatch.setattr(waits, "required_green", lambda target, head: True)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
    idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on=HELD)
    inbox = InboxStore(store.redis)
    pulls = {URL: PullRequest("OPEN", None, 1, False, True, head="first")}
    assert waits.end_pass(store, "sw", {}, inbox, pulls.get, 5_000, pulls.get) == [f"ended the wait of {ME}: {OUTCOME}"]
    assert [item.text for item in inbox.inbox("eng-1@sw")] == [
        f"Your wait on {OUTCOME} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    ]
    assert idle.wait(store.redis, "sw", ME) is None
    assert idle.woke(store.redis, "sw", ME) == idle.waited(store.redis, "sw", ME) == 5_000
    assert idle.wait_history(store.redis, "sw", ME) == [{"until": 5_000, "reason": "", "at": 1, "on": HELD}]
