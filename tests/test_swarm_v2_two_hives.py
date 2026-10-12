from dataclasses import replace

import pytest

from tests import sv2_mst04_cases as cases

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def world(tmp_path, monkeypatch):
    from scripts.swarm_v2 import masters

    monkeypatch.setattr(cases.core, "LEDGER_DIR", tmp_path / "ledgers")
    cases.core.LEDGER_DIR.mkdir()
    world = cases.World()
    table = masters.MasterSeats(world.redis)
    table.own_hive(cases.SLUG, "local", cases.LEAD)
    table.own_hive(cases.SLUG, "anton", cases.SECOND)
    for name, hive in ((cases.LEAD_NAME, "local"), (cases.SECOND_NAME, "anton")):
        agent = next(a for a in world.store.agents(cases.SLUG) if a.name == name)
        world.store.put_agent(cases.SLUG, replace(agent, hive=hive))
    return world


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
    received = inbox.inbox(cases.SECOND_NAME)
    assert len(received) == 1
    assert received[0].address == cases.SECOND
    assert received[0].ref == f"{cases.SLUG}:event:{kind}/targeted"
    assert inbox.deliver(received[0].id, cases.LEAD_NAME) is None
    assert inbox.deliver(received[0].id, cases.SECOND_NAME).state == "delivered"
    assert inbox.deliver(received[0].id, cases.SECOND_NAME) is None
    assert ledger_events.event_pass(inbox, world.store, cases.SLUG, doc, None, 0) == []


def test_question_is_decided_only_by_its_hive_owner_and_posted_once(world):
    from scripts.swarm_v2.masters_channel import MastersChannel

    add(world, "questions")
    op = {"op": "answer", "id": "answer", "by": cases.LEAD_NAME, "item": "questions/targeted", "text": "Proceed"}
    assert not world.write(op)["accepted"]
    op = {**op, "by": cases.SECOND_NAME}
    assert world.write(op)["accepted"]
    assert world.write(op)["accepted"]
    posts = MastersChannel(world.redis).read(cases.SLUG, cases.LEAD_NAME)
    assert len(posts) == 1
    assert posts[0].seat == cases.SECOND
    assert len(next(q for q in world.doc()["questions"] if q["id"] == "targeted")["answers"]) == 1


def test_absent_hive_master_falls_back_to_lead(world):
    from scripts.swarm_v2 import masters

    add(world, "questions")
    world.down(cases.SECOND_NAME)
    table = masters.MasterSeats(world.redis)
    assert (
        masters.route(
            "questions/targeted", world.doc(), lambda: table.owners(cases.SLUG, world.doc()), [cases.LEAD], cases.LEAD
        )
        == cases.LEAD
    )
    op = {"op": "answer", "id": "answer", "by": cases.LEAD_NAME, "item": "questions/targeted", "text": "Proceed"}
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
