from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import delivery, ledger_client
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused, LedgerUnreachable
from scripts.swarm.store import SwarmError

SLUG = "reply-proof"


def _page(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
    from scripts.swarm_ledger import ledger_core
    from scripts.swarm_ledger.repository.legacy import fresh
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    repository = SQLiteLedgerRepository(tmp_path / "page.db", domain=ledger_core)
    doc, meta, _ = fresh(
        {"title": "Reply proof", "overview": "Inbox replies", "phases": [], "questions": [], "followups": []},
        core=ledger_core,
    )
    assert repository.create_document(SLUG, doc, meta, token="fixture")
    repository.apply_ops(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m-ask", "text": "status please"}])
    return repository


def test_a_reply_to_the_operator_waits_out_a_timing_out_ledger_page_and_lands_once(tmp_path, monkeypatch):
    import fakeredis

    repository = _page(tmp_path, monkeypatch)
    ledger = ledger_client._ledger()
    attempts = []

    def request(slug, ops=None, service=False, timeout=None):
        attempts.append(ops)
        if len(attempts) <= 2:
            raise TimeoutError("timed out")
        state, rejected = repository.apply_ops(slug, ops=ops)
        return {**state, "rejected": rejected}

    monkeypatch.setattr(ledger.ledger_link, "remote", lambda *args, **kwargs: True)
    monkeypatch.setattr(ledger, "base", lambda *args, **kwargs: "http://ledger.test")
    monkeypatch.setattr(ledger, "request", request)
    box = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    asked = box.send("operator", "master", "status please", ref=f"{SLUG}:1:m-ask")
    answer = box.reply(asked.id, "master", "the work is complete")
    agents = [SimpleNamespace(name="master")]

    for _ in range(2):
        assert delivery.relay_to_page(box, SLUG, agents, LedgerClient()) == 0
        assert box.get(answer.id).state == "pending"
    alerts = [item for item in box.inbox("master") if item.sender == delivery.BY]
    assert [item.text for item in alerts] == [
        delivery.UNSHOWN.format(
            id=answer.id, reason="ledger reply-proof: ledger server not answering on http://ledger.test: timed out"
        )
    ]
    assert alerts[0].fyi

    assert delivery.relay_to_page(box, SLUG, agents, LedgerClient()) == 1
    assert box.get(answer.id).state == "done"
    assert len(attempts) == 3 and {ops[0]["id"] for ops in attempts} == {answer.id}
    state, _ = repository.apply_ops(SLUG)
    assert [(post["id"], post["text"]) for post in state["chat"]] == [
        ("m-ask", "status please"),
        (answer.id, "the work is complete"),
    ]
    assert delivery.relay_to_page(box, SLUG, agents, LedgerClient()) == 0


def test_an_unreachable_page_stops_the_pass_and_alerts_the_front_door():
    import fakeredis

    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 2, 1))
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t", pane_id="p1"))
    box = InboxStore(store.redis)
    first = box.send("sw-eng-1", "operator", "the docs are merged")
    second = box.send("sw-eng-1", "operator", "the tests are green")
    tried = []

    class DownLedger:
        def relay(self, slug, text, by, op_id, line):
            tried.append(op_id)
            raise LedgerUnreachable("ledger sw: server refused: 503 busy")

    assert delivery.relay_to_page(box, "sw", store.agents("sw"), DownLedger()) == 0
    assert len(tried) == 1
    assert {box.get(first.id).state, box.get(second.id).state} == {"pending"}
    alerts = box.inbox("master@sw")
    assert [item.sender for item in alerts] == [delivery.BY]
    assert alerts[0].text == delivery.UNSHOWN.format(id=tried[0], reason="ledger sw: server refused: 503 busy")


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("ledger server not answering on http://ledger.test: timed out", LedgerUnreachable),
        ("server refused: 503 busy", LedgerUnreachable),
        ("server refused: 409 conflict", LedgerRefused),
        ("rejected: chat clock time", SwarmError),
    ],
)
def test_the_client_names_an_unreachable_ledger_apart_from_a_refusal(monkeypatch, message, error):
    class Ledger:
        Missing = LookupError

        @staticmethod
        def call(slug, ops, service=False):
            raise SystemExit(message)

    monkeypatch.setattr(ledger_client, "_ledger", lambda: Ledger)
    with pytest.raises(SwarmError) as raised:
        LedgerClient().state("sw")
    assert type(raised.value) is error


@pytest.mark.parametrize("write", ["relay", "relay_comment"])
def test_a_post_queued_in_the_local_outbox_is_not_yet_shown(monkeypatch, write):
    ledger = SimpleNamespace(Missing=LookupError, call=lambda slug, ops, service=False: {"queued": ops[0]["id"]})
    monkeypatch.setattr(ledger_client, "_ledger", lambda: ledger)
    args = ("sw", "hello", "sw-master-1", "op-1") if write == "relay" else ("sw", "phases/p1", "hello", "m", "op-1")
    with pytest.raises(LedgerUnreachable, match="waits in the local outbox"):
        getattr(LedgerClient(), write)(*args)
