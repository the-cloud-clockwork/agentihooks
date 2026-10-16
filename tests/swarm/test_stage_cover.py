import json

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck_remedy, cli, dispatch_seat, stage_cover
from scripts.swarm.ledger_events import Mail
from scripts.swarm.store import ASSIST, MANUAL_SCALING, AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_bottleneck_remedy import FakeLedger, fire, home, shipped  # noqa: F401
from tests.swarm.test_dispatch_seat import NOW, doc, held, swarm
from tests.swarm.test_tick import FakeLedger as TickLedger

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "sw"
MASTER, ENGINEER, DISPATCHER = "master@c4d5e6-0001", "engineer@c4d5e6-0002", "dispatcher@c4d5e6-0003"


def ledger_doc(task_state="open", phase_done=False, **extra):
    return {
        "tasks": [{"id": "t7", "state": task_state, "lane": "eng", **extra}],
        "phases": [{"id": "p3", "done": phase_done}],
        "priorities": [],
    }


def master_items(store):
    mail = Mail(InboxStore(store.redis), store, SLUG)
    return [item.text for item in InboxStore(store.redis).inbox(mail.master)]


@pytest.mark.parametrize("item", ["tasks/t7", "phases/p3"])
def test_a_cover_names_its_open_item_reason_author_and_time(item):
    store = swarm()
    row = stage_cover.cover(store, SLUG, ledger_doc(), "merge queue", item, "owned by the queue fix", MASTER, NOW)
    assert row == {"stage": "merge_queue", "item": item, "reason": "owned by the queue fix", "by": MASTER, "at": NOW}
    assert stage_cover.covered(store, SLUG, ledger_doc(), "merge_queue")
    assert stage_cover.read(store, SLUG) == {"merge_queue": row}


@pytest.mark.parametrize(
    ("item", "found"),
    [
        ("tasks/t9", ledger_doc()),
        ("tasks/t7", ledger_doc("done")),
        ("tasks/t7", ledger_doc(out_of_scope=True)),
        ("phases/p3", ledger_doc(phase_done=True)),
        ("plans/a", ledger_doc()),
        ("t7", ledger_doc()),
    ],
)
def test_a_cover_on_a_missing_or_closed_item_is_refused(item, found):
    store = swarm()
    with pytest.raises(SwarmError, match="is not an open phase or task"):
        stage_cover.cover(store, SLUG, found, "ci_overrun", item, "owned", MASTER, NOW)
    assert stage_cover.read(store, SLUG) == {}


def test_a_cover_without_a_reason_is_refused():
    with pytest.raises(SwarmError, match="needs a reason"):
        stage_cover.cover(swarm(), SLUG, ledger_doc(), "ci_overrun", "tasks/t7", "  ", MASTER, NOW)


@pytest.mark.parametrize(
    "closed", [ledger_doc("done"), ledger_doc(out_of_scope=True), {"tasks": [], "phases": []}], ids=str
)
def test_the_cover_lapses_and_is_forgotten_once_its_task_closes(closed):
    store = swarm()
    stage_cover.cover(store, SLUG, ledger_doc(), "ci_overrun", "tasks/t7", "owned", MASTER, NOW)
    assert stage_cover.covered(store, SLUG, closed, "ci_overrun") is False
    assert stage_cover.read(store, SLUG) == {}
    assert stage_cover.covered(store, SLUG, ledger_doc(), "ci_overrun") is False


def test_the_cover_lapses_once_its_phase_is_done():
    store = swarm()
    stage_cover.cover(store, SLUG, ledger_doc(), "seat_wait", "phases/p3", "owned", MASTER, NOW)
    assert stage_cover.covered(store, SLUG, ledger_doc(phase_done=True), "seat_wait") is False


def test_an_uncovered_stage_is_not_covered_and_another_stage_keeps_its_cover():
    store = swarm()
    stage_cover.cover(store, SLUG, ledger_doc(), "seat_wait", "tasks/t7", "owned", MASTER, NOW)
    assert stage_cover.covered(store, SLUG, ledger_doc(), "ci_overrun") is False
    assert stage_cover.covered(store, SLUG, ledger_doc(), "seat_wait")


@pytest.fixture
def remedy_store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=2, scaling=MANUAL_SCALING, autonomy=ASSIST))
    return saved


def test_a_covered_stage_proposes_no_task_and_fires_again_once_the_cover_lapses(remedy_store, home, shipped):  # noqa: F811
    store = remedy_store
    stage_cover.cover(store, SLUG, ledger_doc(), "review", "tasks/t7", "owned by the review fix", MASTER, NOW)
    fire(store, "review")
    run = bottleneck_remedy.remedy_pass
    assert run(SLUG, store, FakeLedger(), ledger_doc(), lambda: None, NOW) == []
    assert master_items(store) == []
    assert store.redis.hgetall(store.key(SLUG, bottleneck_remedy.KEY)) == {}

    assert run(SLUG, store, FakeLedger(), ledger_doc("done"), lambda: None, NOW)
    [text] = master_items(store)
    assert text.startswith("The bottleneck hunter proposes a task for the review stage")


def test_a_covered_share_holds_no_dispatcher_seat_and_triggers_again_once_the_cover_lapses():
    store = swarm()
    held(store, "review")
    config = store.config(SLUG)
    stage_cover.cover(store, SLUG, ledger_doc(), "review", "tasks/t7", "owned", MASTER, NOW)
    assert dispatch_seat.open_triggers(SLUG, config, store, {**doc(), **ledger_doc()}, NOW, False) == []
    assert dispatch_seat.refusal(SLUG, config, store, {**doc(), **ledger_doc()}, NOW) == ""

    found = dispatch_seat.open_triggers(SLUG, config, store, {**doc(), **ledger_doc("done")}, NOW, False)
    assert [trigger["id"] for trigger in found] == ["bottleneck:review"]


@pytest.fixture
def crew(monkeypatch):
    store = swarm()
    store.put_agent(SLUG, AgentRecord(MASTER, "master", "master", seat=f"master@{SLUG}"))
    store.put_agent(SLUG, AgentRecord(ENGINEER, "eng", "t1"))
    store.put_agent(SLUG, AgentRecord(DISPATCHER, "dispatch", "dispatcher", seat=f"dispatcher@{SLUG}"))
    ledger = TickLedger([{"id": "t7"}])
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    return store


def acting(monkeypatch, name="", swarm_slug=""):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", swarm_slug)


@pytest.mark.parametrize(("name", "by"), [("", "operator"), (MASTER, MASTER), (DISPATCHER, DISPATCHER)])
def test_the_operator_master_or_dispatcher_covers_a_stage(crew, monkeypatch, capsys, name, by):
    acting(monkeypatch, name, SLUG if name else "")
    assert cli.main([SLUG, "cover", "ci_overrun", "tasks/t7", "--reason", "owned by the ci fix"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert {key: printed[key] for key in ("stage", "item", "reason", "by")} == {
        "stage": "ci_overrun",
        "item": "tasks/t7",
        "reason": "owned by the ci fix",
        "by": by,
    }
    assert stage_cover.read(crew, SLUG)["ci_overrun"]["by"] == by


def test_an_engineer_cannot_cover_a_stage(crew, monkeypatch, capsys):
    acting(monkeypatch, ENGINEER, SLUG)
    assert cli.main([SLUG, "cover", "ci_overrun", "tasks/t7", "--reason", "mine"]) == 1
    assert "is neither" in capsys.readouterr().err
    assert stage_cover.read(crew, SLUG) == {}


def test_the_cover_command_refuses_a_closed_item(crew, monkeypatch, capsys):
    acting(monkeypatch)
    assert cli.main([SLUG, "cover", "ci_overrun", "tasks/t8", "--reason", "owned"]) == 1
    assert "tasks/t8 is not an open phase or task" in capsys.readouterr().err
