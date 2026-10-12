import pytest

from tests import sv2_mst04_cases as cases

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def world(tmp_path, monkeypatch):
    from scripts.swarm_v2 import masters

    monkeypatch.setattr(cases.core, "LEDGER_DIR", tmp_path / "ledgers")
    cases.core.LEDGER_DIR.mkdir()
    world = TwoHives()
    cases.core.sync(
        cases.SLUG,
        ops=[
            {"op": "join", "id": f"join-{name}", "by": name, "role": "orchestrator"}
            for name in (world.lead_name, world.second_name)
        ],
    )
    table = masters.MasterSeats(world.redis)
    table.own_hive(cases.SLUG, "local", cases.LEAD)
    table.own_hive(cases.SLUG, "anton", cases.SECOND)
    return world


class TwoHives(cases.World):
    def seat(self, name, address):
        from scripts.swarm.store import MASTER, AgentRecord

        hive = "local" if address == cases.LEAD else "anton"
        name = self.store.next_name(cases.SLUG, MASTER)
        self.store.start_execution(
            cases.SLUG, AgentRecord(name, MASTER, MASTER, pane_id=f"p-{name}", seat=address, hive=hive)
        )
        self.store.seats.occupy(address, name, 1)
        if address == cases.LEAD:
            self.lead = self.lead_name = name
        else:
            self.second_name = name


def add(world, kind, hive="anton"):
    return world.write(
        {"op": "add_item", "id": "targeted", "by": cases.ENGINEER, "list": kind, "text": "Capacity ready", "hive": hive}
    )


@pytest.mark.parametrize("kind,command", [("questions", "question"), ("followups", "followup")])
def test_cli_names_the_hive_in_the_ledger_write(monkeypatch, kind, command):
    from scripts.swarm_ledger import ledger

    captured = []
    monkeypatch.setattr(ledger, "send", lambda args, op, **fields: captured.append((op, fields)) or {})
    args = ledger.build_parser().parse_args([command, "add", "Capacity ready", "--hive", "anton"])
    getattr(ledger, f"cmd_{command}")(args)
    assert captured == [("add_item", {"list": kind, "text": "Capacity ready", "hive": "anton"})]


@pytest.mark.parametrize("kind", ["questions", "followups"])
def test_hive_target_survives_the_ledger_write(world, kind):
    assert add(world, kind)["accepted"]
    item = next(i for i in world.doc()[kind] if i["id"] == "targeted")
    assert item["hive"] == "anton"


@pytest.mark.parametrize("hive", ["", "a/b", "a\nb", " a", 7, None, "a" * 129])
def test_malformed_hive_target_is_refused(hive):
    from scripts.swarm_ledger import ledger_agent_ops

    op = {"op": "add_item", "id": "f", "by": cases.ENGINEER, "list": "followups", "text": "Capacity", "hive": hive}
    with pytest.raises(ValueError) as error:
        ledger_agent_ops.check(op)
    assert str(error.value) == "hive needs an identifier of up to 128 letters, digits, dots, underscores or hyphens"


@pytest.mark.parametrize("kind", ["questions", "followups"])
def test_event_relay_sends_hive_items_once_to_the_owner(world, kind):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events

    inbox = InboxStore(world.redis)
    ledger_events.event_pass(inbox, world.store, cases.SLUG, world.doc(), None, 0)
    add(world, kind)
    doc = world.doc()
    ledger_events.event_pass(inbox, world.store, cases.SLUG, doc, None, 0)
    received = inbox.mailbox(world.second_name)
    assert len(received) == 1
    assert received[0].address == cases.SECOND
    assert received[0].ref == f"{cases.SLUG}:event:{kind}/targeted"
    assert inbox.deliver(received[0].id, world.lead_name) is None
    assert inbox.deliver(received[0].id, world.second_name).state == "delivered"
    assert inbox.deliver(received[0].id, world.second_name) is None
    assert ledger_events.event_pass(inbox, world.store, cases.SLUG, doc, None, 0) == []


def test_question_is_decided_only_by_its_hive_owner_and_posted_once(world):
    from scripts.swarm_v2.masters_channel import MastersChannel

    add(world, "questions")
    op = {"op": "answer", "id": "answer", "by": world.lead_name, "item": "questions/targeted", "text": "Proceed"}
    assert not world.write(op)["accepted"]
    op = {**op, "by": world.second_name}
    assert world.write(op)["accepted"]
    assert world.write(op)["accepted"]
    posts = MastersChannel(world.redis).read(cases.SLUG, world.lead_name)
    assert len(posts) == 1
    assert posts[0].seat == cases.SECOND
    assert len(next(q for q in world.doc()["questions"] if q["id"] == "targeted")["answers"]) == 1


def test_absent_hive_master_falls_back_to_lead(world):
    from scripts.swarm_v2 import masters

    add(world, "questions")
    world.down(world.second_name)
    table = masters.MasterSeats(world.redis)
    assert (
        masters.route(
            "questions/targeted", world.doc(), lambda: table.owners(cases.SLUG, world.doc()), [cases.LEAD], cases.LEAD
        )
        == cases.LEAD
    )
    op = {"op": "answer", "id": "answer", "by": world.lead_name, "item": "questions/targeted", "text": "Proceed"}
    assert world.write(op)["accepted"]


def test_operator_comment_routes_to_hive_owner_but_chat_stays_with_lead(world):
    from scripts.swarm import operator_mail
    from scripts.swarm_v2 import masters

    add(world, "questions")
    doc = world.doc()

    def owners():
        return masters.MasterSeats(world.redis).owners(cases.SLUG, doc)

    agents = world.store.agents(cases.SLUG)
    assert operator_mail.addresses(cases.SLUG, {"target": "questions/targeted"}, doc, agents, owners) == [cases.SECOND]
    assert operator_mail.addresses(cases.SLUG, {"target": "chat"}, doc, agents, owners) == [cases.LEAD]


@pytest.mark.parametrize("command", ["question", "followup"])
def test_untargeted_cli_retains_the_original_write(monkeypatch, command):
    from scripts.swarm_ledger import ledger

    captured = []
    monkeypatch.setattr(ledger, "send", lambda args, op, **fields: captured.append(fields) or {})
    args = ledger.build_parser().parse_args([command, "add", "Capacity ready"])
    getattr(ledger, f"cmd_{command}")(args)
    assert captured == [{"list": "questions" if command == "question" else "followups", "text": "Capacity ready"}]


@pytest.mark.parametrize("hive", ["anton", "a.B_1-2", "a" * 128])
def test_valid_hive_target_is_checked_and_persisted(hive):
    from types import SimpleNamespace

    from scripts.swarm_ledger import ledger_agent_ops

    recorded = []
    op = {"op": "add_item", "id": "q", "by": cases.ENGINEER, "list": "questions", "text": "Capacity", "hive": hive}
    ledger_agent_ops.check(op)
    doc = {}
    ctx = SimpleNamespace(record=lambda *args, **kwargs: recorded.append((args, kwargs)))
    assert ledger_agent_ops.apply(doc, op, ctx)
    assert doc == {"questions": [{"id": "q", "text": "Capacity", "comments": [], "answers": [], "hive": hive}]}
    assert recorded == [((cases.ENGINEER, "added", "questions/q"), {"text": "Capacity"})]


@pytest.mark.parametrize("kind", ["questions", "followups"])
def test_untargeted_or_missing_items_stay_with_the_lead(world, kind):
    from scripts.swarm_v2 import masters

    doc = world.doc()
    doc[kind] = [{"id": "untargeted", "text": "Capacity"}]
    table = masters.MasterSeats(world.redis)

    def owners():
        return table.owners(cases.SLUG, doc)

    for item in ("untargeted", "absent"):
        assert masters.owner_of(f"{kind}/{item}", doc, owners()) == ""
        assert masters.route(f"{kind}/{item}", doc, owners, [cases.LEAD, cases.SECOND], cases.LEAD) == cases.LEAD


def test_followup_escalation_reaches_the_hive_owner(world):
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events

    inbox = InboxStore(world.redis)
    ledger_events.event_pass(inbox, world.store, cases.SLUG, world.doc(), None, 0)
    add(world, "followups")
    doc = world.doc()
    added = next(e for e in doc["_meta"]["events"] if e["target"] == "followups/targeted")
    now = added["at"] + ledger_events.FOLLOWUP_RAISE_MS
    ledger_events.event_pass(inbox, world.store, cases.SLUG, doc, None, now)
    received = inbox.mailbox(world.second_name)
    assert len(received) == 2
    assert all(item.address == cases.SECOND for item in received)
    assert "still open fifteen minutes" in received[1].text
    assert ledger_events.event_pass(inbox, world.store, cases.SLUG, doc, None, now) == []
