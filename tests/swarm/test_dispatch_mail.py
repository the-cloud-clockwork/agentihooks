import json

import pytest

from scripts.inbox import cli
from scripts.inbox.store import InboxStore
from scripts.swarm import dispatch_seat
from tests.swarm.test_dispatch_seat import NOW, SLUG, doc, priority, run, seats, swarm
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


@pytest.mark.parametrize(
    "policy,expected",
    [
        ("paused", "paused"),
        ("stopped", "stopped"),
        ("stopping", "stopping"),
        ("delegate", "autonomy"),
        ("running", "pending lifecycle checks"),
    ],
)
def test_sender_reports_dispatcher_ownership_and_activation_deferral(mailbox, capsys, policy, expected):
    store, inbox = mailbox
    if policy == "delegate":
        store.update(SLUG, autonomy=policy)
    else:
        store.update(SLUG, state=policy)
    assert cli.main(["send", f"dispatcher@{SLUG}", "Consider rank evidence"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["dispatch"] == {"owner": f"dispatcher@{SLUG}", "consumer": "", "activation": expected}
    assert inbox.get(sent["id"]).state == "pending"


@pytest.mark.parametrize("receipt", ["pending", "delivered", "confirmed", "read"])
@pytest.mark.parametrize("named", [False, True])
def test_mail_arriving_as_structured_triggers_clear_retains_its_consumer(mailbox, monkeypatch, capsys, receipt, named):
    from tests.swarm.test_dispatch_seat import priority

    store, inbox = mailbox
    runtime = FakeRuntime()
    run(store, runtime, doc(priority()))
    [seat] = seats(store)
    address = seat.name if named else seat.seat
    assert cli.main(["send", address, "Consider new rank evidence"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["dispatch"] == {"owner": seat.seat, "consumer": seat.name, "activation": "recorded consumer"}
    if receipt in ("delivered", "confirmed"):
        assert inbox.deliver(sent["id"], seat.name).state == "delivered"
    if receipt == "confirmed":
        assert inbox.confirm(sent["id"], seat.name).state == "confirmed"
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", seat.name)
    if receipt == "read":
        assert cli.main(["read", sent["id"]]) == 0
        capsys.readouterr()
    assert run(store, runtime, doc(), NOW + 60_000) == []
    assert [item.id for item in inbox.open_items(seat.seat)] == [sent["id"]]
    assert seats(store)[0].state != "finished"
    assert len(runtime.spawned) == 1
    assert cli.main(["close", sent["id"], "handoff", "master@" + SLUG]) == 0
    capsys.readouterr()
    run(store, runtime, doc(), NOW + 120_000)
    assert seats(store)[0].state == "finished"


@pytest.mark.parametrize("policy", ["paused", "delegate", "stopped", "sleeping", "capacity"])
def test_accepted_mail_waits_for_existing_policy_before_activation(mailbox, capsys, policy):
    store, inbox = mailbox
    runtime = FakeRuntime(full=policy == "capacity")
    if policy in ("paused", "stopped"):
        store.update(SLUG, state=policy)
    elif policy == "delegate":
        store.update(SLUG, autonomy=policy)
    assert cli.main(["send", f"dispatcher@{SLUG}", "Consider rank evidence"]) == 0
    sent = json.loads(capsys.readouterr().out)
    dispatch_seat.run(SLUG, store.config(SLUG), store, runtime, doc(), NOW, sleeping=policy == "sleeping")
    assert not runtime.spawned
    assert inbox.get(sent["id"]).state == "pending"
    store.update(SLUG, state="running", autonomy="full")
    runtime.full = False
    run(store, runtime, doc(), NOW + 60_000)
    assert len(runtime.spawned) == 1


def test_information_does_not_activate_or_retain_a_dispatcher(mailbox, capsys):
    from tests.swarm.test_dispatch_seat import priority

    store, inbox = mailbox
    runtime = FakeRuntime()
    assert cli.main(["send", f"dispatcher@{SLUG}", "--fyi", "Status only"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert "dispatch" not in sent
    assert inbox.get(sent["id"]).fyi
    assert run(store, runtime, doc()) == []
    assert not runtime.spawned
    run(store, runtime, doc(priority()))
    run(store, runtime, doc(), NOW + 60_000)
    assert seats(store)[0].state == "finished"


def test_send_response_loss_replays_one_item_and_duplicate_delivery_retains_one_owner(mailbox, monkeypatch, capsys):
    from redis.exceptions import ConnectionError

    from scripts.inbox import outbox

    store, inbox = mailbox
    original = cli.apply
    attempts = []

    def lose_response(mail, record):
        attempts.append(record["item"])
        result = original(mail, record)
        if len(attempts) == 1:
            raise ConnectionError("response lost after acceptance")
        return result

    monkeypatch.setattr(cli, "apply", lose_response)
    assert cli.main(["send", f"dispatcher@{SLUG}", "Consider rank evidence"]) == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["state"] == "queued"
    report = outbox.flush(inbox)
    assert report.already == [queued["id"]]
    assert attempts == [queued["id"], queued["id"]]
    assert len(inbox.open_items(f"dispatcher@{SLUG}")) == 1
    runtime = FakeRuntime()
    run(store, runtime, doc())
    [seat] = seats(store)
    delivered = inbox.deliver(queued["id"], seat.name)
    assert delivered.state == "delivered"
    assert inbox.deliver(queued["id"], seat.name) is None
    assert [item.id for item in inbox.redeliver(delivered.updated_at + 301_000, 300_000)] == [queued["id"]]
    run(store, runtime, doc(), NOW + 60_000)
    assert len(runtime.spawned) == 1
    assert seats(store)[0].state != "finished"
    assert inbox.deliver(queued["id"], seat.name).state == "delivered"
    assert inbox.confirm(queued["id"], seat.name).state == "confirmed"
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", seat.name)
    assert cli.main(["reply", queued["id"], "Evidence considered"]) == 0
    capsys.readouterr()
    run(store, runtime, doc(), NOW + 120_000)
    assert seats(store)[0].state == "finished"


def test_departure_does_not_suppress_accepted_mail_for_the_successor(mailbox, capsys):
    from tests.swarm.test_dispatch_seat import crew

    store, inbox = mailbox
    runtime = FakeRuntime()
    assert cli.main(["send", f"dispatcher@{SLUG}", "Consider rank evidence"]) == 0
    sent = json.loads(capsys.readouterr().out)
    run(store, runtime, doc())
    [seat] = seats(store)
    departed = crew(doc(), joined={seat.name: [NOW + 10_000]})
    run(store, runtime, departed, NOW + 60_000)
    run(store, runtime, doc(), NOW + 120_000)
    assert len(runtime.spawned) == 2
    assert inbox.get(sent["id"]).state == "pending"
    assert inbox.seats.occupant(seat.seat).occupant != seat.name


def test_public_work_sent_to_a_finished_dispatcher_belongs_to_its_successor(mailbox, capsys):
    from tests.swarm.test_dispatch_seat import priority

    store, inbox = mailbox
    runtime = FakeRuntime()
    run(store, runtime, doc(priority()))
    [seat] = seats(store)
    run(store, runtime, doc(), NOW + 60_000)
    assert seats(store)[0].state == "finished"
    assert cli.main(["send", seat.name, "Consider fresh rank evidence"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["dispatch"] == {"owner": seat.seat, "consumer": "", "activation": "pending lifecycle checks"}
    run(store, runtime, doc(), NOW + 120_000)
    assert len(runtime.spawned) == 2
    successor = next(a for a in seats(store) if a.state != "finished")
    assert sent["to"] == successor.seat
    assert inbox.deliver(sent["id"], successor.name).state == "delivered"


def test_a_failed_consumer_launch_retries_without_losing_accepted_mail(mailbox, capsys):
    store, inbox = mailbox
    runtime = FakeRuntime(fail=True)
    assert cli.main(["send", f"dispatcher@{SLUG}", "Consider rank evidence"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert "spawn failed" in run(store, runtime, doc())[0]
    assert inbox.get(sent["id"]).state == "pending"
    runtime.fail = False
    run(store, runtime, doc(), NOW + 60_000)
    assert len(runtime.spawned) == 1
    [seat] = seats(store)
    assert inbox.deliver(sent["id"], seat.name).state == "delivered"


@pytest.mark.parametrize("disposition", ["missing", "done", "cancel"])
@pytest.mark.parametrize("remaining", ["none", "mail", "priority"])
@pytest.mark.parametrize("boundary", ["capacity", "prepare"])
def test_launch_rechecks_mail_after_capacity_checks(mailbox, monkeypatch, capsys, disposition, remaining, boundary):
    store, inbox = mailbox
    runtime = FakeRuntime()
    assert cli.main(["send", f"dispatcher@{SLUG}", "Consider obsolete evidence"]) == 0
    stale = json.loads(capsys.readouterr().out)["id"]
    expected = []
    if remaining == "mail":
        assert cli.main(["send", f"dispatcher@{SLUG}", "Consider current evidence"]) == 0
        current = json.loads(capsys.readouterr().out)["id"]
        expected = [f"inbox:{current}"]

    def settle_mail(*args):
        if disposition == "missing":
            store.redis.delete(inbox.key("item", stale))
        else:
            closer = inbox.seats.occupant(f"dispatcher@{SLUG}").occupant
            inbox.close(stale, closer, disposition, "Evidence considered; ranking unchanged")
        return ""

    if boundary == "capacity":
        monkeypatch.setattr(dispatch_seat.seat_spawn, "host_hold", settle_mail)
    else:
        from scripts.swarm import tick

        primed = tick.primed

        def settle_during_prepare(*args):
            task = primed(*args)
            settle_mail()
            return task

        monkeypatch.setattr(tick, "primed", settle_during_prepare)
    ledger = doc() if remaining != "priority" else doc(priority("p1"))
    if remaining == "priority":
        expected = ["p1"]
    actions = run(store, runtime, ledger)
    if remaining == "none":
        assert actions == []
        assert runtime.spawned == []
        assert seats(store) == []
    else:
        assert len(runtime.spawned) == 1, actions
        assert [trigger["id"] for trigger in runtime.tasks[0]["triggers"]] == expected
        assert store.redis.hkeys(store.key(SLUG, dispatch_seat.SENT)) == expected


@pytest.mark.parametrize("change", ["read", "confirmed", "redirected", "delivered"])
@pytest.mark.parametrize("boundary", ["before", "capacity"])
def test_launch_needs_mail_still_waiting_for_the_dispatcher(mailbox, monkeypatch, capsys, change, boundary):
    store, inbox = mailbox
    runtime = FakeRuntime()
    assert cli.main(["send", f"dispatcher@{SLUG}", "Consider evidence already taken up"]) == 0
    item = json.loads(capsys.readouterr().out)["id"]

    def settle(*args):
        occupant = inbox.seats.occupant(f"dispatcher@{SLUG}").occupant
        if change == "read":
            assert inbox.read(item, occupant).state == "read"
        elif change == "redirected":
            assert inbox.redirect(item, "swarm", "alice", "taken up by alice").address == "alice"
        else:
            assert inbox.deliver(item, occupant).state == "delivered"
        if change == "confirmed":
            assert inbox.confirm(item, occupant).state == "confirmed"
        return ""

    if boundary == "before":
        settle()
    else:
        monkeypatch.setattr(dispatch_seat.seat_spawn, "host_hold", settle)
    launches = change == "delivered"
    assert bool(run(store, runtime, doc())) == launches
    assert len(runtime.spawned) == int(launches)
    assert len(seats(store)) == int(launches)


@pytest.mark.parametrize("lane,state", [("eng", "working"), ("dispatch", "finished")])
def test_other_or_finished_consumers_do_not_own_dispatcher_work(mailbox, capsys, lane, state):
    from scripts.swarm.store import AgentRecord

    store, inbox = mailbox
    name = store.next_name(SLUG, lane, NOW)
    store.put_agent(SLUG, AgentRecord(name, lane, "other", state=state))
    inbox.send("alice", name, "Work belonging to another consumer")
    runtime = FakeRuntime()
    assert run(store, runtime, doc()) == []
    assert not runtime.spawned
    assert cli.main(["send", f"dispatcher@{SLUG}", "Consider evidence"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["dispatch"] == {"owner": f"dispatcher@{SLUG}", "consumer": "", "activation": "pending lifecycle checks"}


def test_legacy_named_mail_retains_its_live_dispatcher(mailbox):
    from tests.swarm.test_dispatch_seat import priority

    store, inbox = mailbox
    runtime = FakeRuntime()
    run(store, runtime, doc(priority()))
    [seat] = seats(store)
    item = inbox.send("alice", seat.name, "Consider evidence already accepted by name")
    assert run(store, runtime, doc(), NOW + 60_000) == []
    assert seats(store)[0].state != "finished"
    assert inbox.deliver(item.id, seat.name).state == "delivered"


def test_a_generated_trigger_notice_does_not_retain_the_dispatcher_after_its_triggers_close(mailbox):
    from tests.swarm.test_dispatch_seat import priority

    store, inbox = mailbox
    runtime = FakeRuntime()
    first = priority()
    second = {**first, "id": "pr2", "item": "tasks/t6"}
    run(store, runtime, doc(first))
    [seat] = seats(store)
    assert run(store, runtime, doc(first, second), NOW + 60_000)
    [notice] = inbox.open_items(seat.seat)
    assert notice.sender == "swarm"
    assert notice.text.startswith("New dispatcher triggers in swarm ")
    run(store, runtime, doc(), NOW + 120_000)
    assert seats(store)[0].state == "finished"


@pytest.mark.parametrize(
    "sender,text",
    [
        ("swarm", "Consider the new integration evidence"),
        ("alice", "New dispatcher triggers in swarm are evidence for judgment"),
    ],
)
def test_actionable_mail_is_not_mistaken_for_a_generated_trigger_notice(mailbox, sender, text):
    store, inbox = mailbox
    item = inbox.send(sender, f"dispatcher@{SLUG}", text)
    runtime = FakeRuntime()
    run(store, runtime, doc())
    assert len(runtime.spawned) == 1
    [seat] = seats(store)
    assert inbox.deliver(item.id, seat.name).state == "delivered"


@pytest.mark.parametrize("addressed_to", ["seat", "occupant"])
@pytest.mark.parametrize("delivery", ["pending", "delivered", "read"])
def test_master_followup_during_dispatcher_exit_reaches_successor(mailbox, monkeypatch, capsys, addressed_to, delivery):
    from scripts.inbox import exits
    from scripts.swarm import cli as swarm_cli

    store, inbox = mailbox
    runtime = FakeRuntime()
    first = inbox.send("master", f"dispatcher@{SLUG}", "Rank the launch blocker first")
    run(store, runtime, doc())
    [departing] = seats(store)
    inbox.seats.occupy(departing.seat, departing.name, NOW)
    assert inbox.read(first.id, departing.name).state == "read"
    inbox.reply(first.id, departing.name, "The launch blocker ranks first")
    assert inbox.get(first.id).state == "done"
    monkeypatch.setattr(swarm_cli, "LedgerClient", lambda: type("Ledger", (), {"state": lambda self, slug: doc()})())
    monkeypatch.setattr(swarm_cli, "now_ms", lambda: NOW)
    settle = exits.settle
    sent = []

    def arriving(inbox_store, name, seat, reason):
        address = departing.seat if addressed_to == "seat" else departing.name
        followup = inbox_store.send("master", address, "Group the remaining launch blockers next")
        sent.append(followup)
        if delivery == "delivered":
            assert inbox_store.deliver(followup.id, departing.name).state == delivery
        elif delivery == "read":
            assert inbox_store.read(followup.id, departing.name).state == delivery
        settle(inbox_store, name, seat, reason)

    monkeypatch.setattr(exits, "settle", arriving)
    swarm_cli._dispatcher_done(store, SLUG, departing)
    capsys.readouterr()
    [followup] = sent
    assert inbox.get(followup.id).state == "pending"
    assert inbox.get(followup.id).address == departing.seat
    exits.sweep(inbox, SLUG, store, lambda: {})
    assert inbox.get(followup.id).state == "pending"
    run(store, runtime, doc(), NOW + 60_000)
    [successor] = [agent for agent in seats(store) if agent.state != "finished"]
    assert successor.name != departing.name
    inbox.seats.occupy(successor.seat, successor.name, NOW + 60_000)
    assert inbox.read(followup.id, successor.name).text == followup.text
    assert followup.id in dispatch_seat.refusal(SLUG, store.config(SLUG), store, doc(), NOW + 60_000)
    inbox.reply(followup.id, successor.name, "Remaining launch blockers grouped")
    assert inbox.get(followup.id).state == "done"
    assert inbox.open_items("master")[-1].text == "Remaining launch blockers grouped"
    run(store, runtime, doc(), NOW + 120_000)
    assert all(agent.state == "finished" for agent in seats(store))
