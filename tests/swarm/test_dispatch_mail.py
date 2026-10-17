import json

import pytest

from scripts.inbox import cli
from scripts.inbox.store import InboxStore
from scripts.swarm import dispatch_seat
from tests.swarm.test_dispatch_seat import NOW, SLUG, doc, run, seats, swarm
from tests.swarm.test_tick import FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group(name="dispatch_mail")]


@pytest.fixture
def mailbox(monkeypatch):
    store = swarm()
    inbox = InboxStore(store.redis)
    inbox.seats.occupy(f"dispatcher@{SLUG}", "former", NOW)
    monkeypatch.setattr(cli, "connect", lambda: inbox)
    monkeypatch.setattr(cli, "registered_name", lambda: "")
    monkeypatch.setattr("scripts.inbox.addresses.get_active_sessions", lambda **kwargs: {"alice": {"name": "alice"}})
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "alice")
    return store, inbox


@pytest.mark.parametrize("disposition", ["reply", "close"])
def test_public_dispatcher_work_activates_and_retains_until_disposition(mailbox, monkeypatch, capsys, disposition):
    store, inbox = mailbox
    runtime = FakeRuntime()
    assert cli.main(["send", f"dispatcher@{SLUG}", "Consider this evidence when ranking the checks fix"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["state"] == "pending"
    actions = run(store, runtime, doc())
    assert len(runtime.spawned) == 1, actions
    [seat] = seats(store)
    assert seat.state != "finished"
    inbox.seats.occupy(seat.seat, seat.name, NOW)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", seat.name)
    assert cli.main(["read", sent["id"]]) == 0
    capsys.readouterr()
    run(store, runtime, doc(), NOW + 60_000)
    assert seats(store)[0].state != "finished"
    assert sent["id"] in dispatch_seat.refusal(SLUG, store.config(SLUG), store, doc(), NOW)
    args = ["reply", sent["id"], "Evidence considered; ranking remains unchanged"]
    if disposition == "close":
        args = ["close", sent["id"], "done", "Evidence considered; ranking remains unchanged"]
    assert cli.main(args) == 0
    capsys.readouterr()
    assert inbox.get(sent["id"]).state == "done"
    run(store, runtime, doc(), NOW + 120_000)
    assert seats(store)[0].state == "finished"
