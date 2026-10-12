import pytest

from scripts.inbox import wake
from scripts.swarm import outage
from scripts.swarm.store import AgentRecord
from tests.inbox.test_wake import AGENTS, MASTER_NAME, FakeHerdr, FakeLedger, W, events, run, sent_at
from tests.inbox.test_wake import inbox as inbox
from tests.inbox.test_wake import server as server

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture(autouse=True)
def outages(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)


def test_the_ladder_pauses_for_an_outage_and_resumes_where_it_stopped(inbox):
    item = inbox.send(MASTER_NAME, "sw-eng-1", "review my diff")
    herdr, ledger, start = FakeHerdr({"p1": "idle"}), FakeLedger(), sent_at(item)
    run(inbox, herdr, ledger, start + 1)
    assert events(inbox, item.id) == ["woken"]
    outage.observe("sw", "provider", ["sw-eng-1 (claude): API Error: 529"], start + 100_000)
    for n in range(1, 4):
        assert run(inbox, herdr, ledger, start + n * W) == []
    assert events(inbox, item.id) == ["woken"] and len(herdr.prompts) == 1
    outage.observe("sw", "provider", [], start + 1_000_000)
    assert run(inbox, herdr, ledger, start + 1 + 900_000 + W - 1) == []
    assert run(inbox, herdr, ledger, start + 1 + 900_000 + W) == [f"woke sw-eng-1 for message {item.id}"]
    assert events(inbox, item.id) == ["woken", "woken"]


def test_escalation_to_the_master_and_operator_waits_out_the_outage(inbox):
    item = inbox.send("sw-eng-2", "sw-eng-1", "review my diff")
    herdr, ledger, start = FakeHerdr({"p1": "working"}), FakeLedger(), sent_at(item)
    outage.observe("sw", "redis", ["Redis ping check failed: ConnectionError"], start + W - 1)
    assert run(inbox, herdr, ledger, start + 5 * W) == []
    outage.observe("sw", "redis", [], start + 5 * W)
    assert run(inbox, herdr, ledger, start + 5 * W) == []
    run(inbox, herdr, ledger, start + 5 * W + 1)
    assert events(inbox, item.id) == ["escalated_master"] and ledger.followups == []


def test_an_outage_withdraws_no_message_whose_recipient_stops_resolving(inbox, monkeypatch):
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {})
    item = inbox.send(MASTER_NAME, "gone-name", "review my diff")
    outage.observe("sw", "provider", ["sw-eng-1 (claude): API Error: 529"], sent_at(item))
    assert run(inbox, FakeHerdr({}), FakeLedger(), sent_at(item) + 5 * W) == []
    assert inbox.get(item.id).state == "pending"


def test_the_immediate_codex_wake_waits_for_the_outage_to_end(inbox):
    codex = AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", harness="codex")
    item = inbox.send(MASTER_NAME, "sw-eng-1", "review my diff")
    herdr, start = FakeHerdr({"p1": "idle"}), sent_at(item)
    outage.observe("sw", "provider", ["sw-eng-1 (codex): stream disconnected before completion"], start)
    assert wake.wake_now(inbox, "sw", [codex, *AGENTS[1:]], herdr, start + 1, W) == []
    outage.observe("sw", "provider", [], start + 2)
    assert wake.wake_now(inbox, "sw", [codex, *AGENTS[1:]], herdr, start + 3, W) == [
        f"woke sw-eng-1 for message {item.id}"
    ]
