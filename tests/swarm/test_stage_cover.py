import json

import pytest

from scripts.gates import Who
from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck_remedy, clearance, cli, dispatch_seat, stage_cover
from scripts.swarm.health import standing
from scripts.swarm.ledger_events import Mail
from scripts.swarm.store import ASSIST, MANUAL_SCALING, AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_bottleneck_remedy import FakeLedger, fire, home, shipped  # noqa: F401
from tests.swarm.test_dispatch_seat import NOW, doc, held, swarm
from tests.swarm.test_tick import FakeLedger as TickLedger

pytestmark = pytest.mark.unit

SLUG = "sw"
MASTER, ENGINEER, DISPATCHER = "master@c4d5e6-0001", "engineer@c4d5e6-0002", "dispatcher@c4d5e6-0003"


def ledger_doc(task_state="open", phase_done=False, **extra):
    return {
        "tasks": [{"id": "t7", "state": task_state, "lane": "eng", **extra}],
        "phases": [{"id": "p3", "done": phase_done}],
        "priorities": [],
    }


def put(store, stage, item="tasks/t7", reason="owned", found=None):
    request = stage_cover.Request(stage, item, reason, MASTER)
    return stage_cover.cover(store, SLUG, found or ledger_doc(), request, NOW)


def stored(store):
    raw = store.redis.hgetall(store.key(SLUG, stage_cover.KEY))
    return {stage: json.loads(value) for stage, value in raw.items()}


def master_items(store):
    mail = Mail(InboxStore(store.redis), store, SLUG)
    return [item.text for item in InboxStore(store.redis).inbox(mail.master)]


@pytest.mark.parametrize("item", ["tasks/t7", "phases/p3"])
def test_a_cover_names_its_open_item_reason_author_and_time(item):
    store = swarm()
    row = put(store, "merge_queue", item, "owned by the queue fix")
    assert row == {"stage": "merge_queue", "item": item, "reason": "owned by the queue fix", "by": MASTER, "at": NOW}
    assert stored(store) == {"merge_queue": row}
    assert stage_cover.covered(store, SLUG, ledger_doc(), "merge_queue")


@pytest.mark.parametrize("spoken", ["merge queue", "Merge-Queue", " MERGE_QUEUE "])
def test_a_spoken_stage_name_is_stored_under_its_stage(spoken):
    store = swarm()
    assert put(store, spoken)["stage"] == "merge_queue"
    assert list(stored(store)) == ["merge_queue"]


def test_an_unknown_stage_is_refused_with_the_names_it_takes():
    store = swarm()
    with pytest.raises(SwarmError, match="no bottleneck stage or share is named ci overrunn; name one of .*ci_overrun"):
        put(store, "ci overrunn")
    assert stored(store) == {}


def test_a_stage_with_a_standing_finding_is_accepted_though_no_share_lists_it():
    store = swarm()
    standing.fire(store.redis, SLUG, "account_burn", standing.Reading(5.0, 2.0, "hours", "e1"), NOW)
    assert put(store, "account_burn")["stage"] == "account_burn"


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
    ids=["missing task", "done task", "out of scope task", "done phase", "other kind", "bare id"],
)
def test_a_cover_on_a_missing_or_closed_item_is_refused(item, found):
    store = swarm()
    with pytest.raises(SwarmError, match="is not an open phase or task"):
        put(store, "ci_overrun", item, found=found)
    assert stored(store) == {}


def test_a_cover_without_a_reason_is_refused():
    with pytest.raises(SwarmError, match="needs a reason"):
        put(swarm(), "ci_overrun", reason="  ")


def test_a_new_cover_on_a_stage_replaces_the_old_one():
    store = swarm()
    put(store, "ci_overrun", "tasks/t7", "first owner")
    put(store, "ci_overrun", "phases/p3", "second owner")
    assert {key: stored(store)["ci_overrun"][key] for key in ("item", "reason")} == {
        "item": "phases/p3",
        "reason": "second owner",
    }


@pytest.mark.parametrize(
    "closed", [ledger_doc("done"), ledger_doc(out_of_scope=True)], ids=["done task", "out of scope task"]
)
def test_the_cover_lapses_and_is_forgotten_once_its_task_closes(closed):
    store = swarm()
    put(store, "ci_overrun")
    assert stage_cover.covered(store, SLUG, closed, "ci_overrun") is False
    assert stored(store) == {}
    assert stage_cover.covered(store, SLUG, ledger_doc(), "ci_overrun") is False


def test_the_cover_lapses_once_its_phase_is_done():
    store = swarm()
    put(store, "seat_wait", "phases/p3")
    assert stage_cover.covered(store, SLUG, ledger_doc(phase_done=True), "seat_wait") is False
    assert stored(store) == {}


def test_a_ledger_read_with_no_tasks_keeps_the_cover():
    store = swarm()
    put(store, "ci_overrun")
    assert stage_cover.covered(store, SLUG, {"tasks": [], "phases": []}, "ci_overrun")
    assert list(stored(store)) == ["ci_overrun"]


def test_the_cover_lapses_once_its_task_is_gone_from_a_ledger_listing_others():
    store = swarm()
    put(store, "ci_overrun")
    others = {"tasks": [{"id": "t8", "state": "open"}], "phases": []}
    assert stage_cover.covered(store, SLUG, others, "ci_overrun") is False
    assert stored(store) == {}


def test_a_lapse_never_deletes_a_cover_written_after_the_read():
    store = swarm()
    old = json.dumps(put(store, "ci_overrun", "tasks/t7", "first owner"))
    put(store, "ci_overrun", "phases/p3", "second owner")
    stage_cover._lapse(store.redis, store.key(SLUG, stage_cover.KEY), "ci_overrun", old)
    assert stored(store)["ci_overrun"]["reason"] == "second owner"


def test_an_item_id_holding_a_slash_is_found_and_lapses_once_gone():
    store = swarm()
    nested = {"tasks": [{"id": "a/b", "state": "open"}], "phases": []}
    put(store, "ci_overrun", "tasks/a/b", found=nested)
    assert stage_cover.covered(store, SLUG, nested, "ci_overrun")
    assert stage_cover.covered(store, SLUG, {"tasks": [{"id": "t8", "state": "open"}]}, "ci_overrun") is False
    assert stored(store) == {}


def test_a_ledger_without_phases_refuses_a_phase_cover_and_keeps_an_existing_one():
    store = swarm()
    with pytest.raises(SwarmError, match="phases/p3 is not an open phase or task"):
        put(store, "ci_overrun", "phases/p3", found={"tasks": []})
    put(store, "ci_overrun", "phases/p3")
    assert stage_cover.covered(store, SLUG, {"tasks": []}, "ci_overrun")


def test_the_remedy_reading_leaves_sibling_covers_out_by_default():
    store = swarm()
    put(store, "merge_queue")
    assert stage_cover.covered(store, SLUG, ledger_doc(), "review") is False
    assert stage_cover.covered(store, SLUG, ledger_doc(), "seat_wait", share=True) is False


def test_the_unknown_stage_refusal_lists_every_name_comma_separated():
    with pytest.raises(SwarmError) as refused:
        put(swarm(), "nowhere")
    assert "name one of build, ci, ci_overrun, ci_queue, ci_run, claim_wait, engineering, host" in str(refused.value)


def test_a_standing_stage_is_covered_under_its_exact_name():
    store = swarm()
    standing.fire(store.redis, SLUG, "Burn-rate:a-b", standing.Reading(5.0, 2.0, "hours", "e1"), NOW)
    assert put(store, "Burn-rate:a-b")["stage"] == "Burn-rate:a-b"
    assert stage_cover.covered(store, SLUG, ledger_doc(), "Burn-rate:a-b")


@pytest.mark.parametrize(
    ("covered_name", "asked", "share", "expected"),
    [
        ("merge_queue", "review", True, True),
        ("quota_hold", "quota", True, True),
        ("seat_wait", "ci", True, False),
        ("merge_queue", "review", False, False),
        ("intent_fix", "merge_queue", False, False),
        ("review", "merge_queue", False, True),
        ("quota", "quota_hold", False, True),
    ],
    ids=[
        "stage frees its share seat",
        "hold frees its share seat",
        "other share seat stays",
        "stage leaves the review remedy",
        "sibling remedy stays",
        "share silences its stage",
        "quota silences its hold",
    ],
)
def test_a_share_cover_covers_its_stages_and_a_stage_cover_frees_only_its_share_seat(
    covered_name, asked, share, expected
):
    store = swarm()
    put(store, covered_name)
    assert stage_cover.covered(store, SLUG, ledger_doc(), asked, share=share) is expected


@pytest.fixture
def remedy_store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=2, scaling=MANUAL_SCALING, autonomy=ASSIST))
    return saved


@pytest.mark.parametrize(
    ("stage", "cover_name", "item"),
    [
        ("review", "review", "tasks/t7"),
        ("merge_queue", "merge queue", "phases/p3"),
        ("quota_hold", "quota", "tasks/t7"),
    ],
)
def test_a_covered_stage_proposes_no_task_and_fires_again_once_the_cover_lapses(
    remedy_store,
    home,  # noqa: F811
    shipped,  # noqa: F811
    stage,
    cover_name,
    item,
):
    store = remedy_store
    put(store, cover_name, item, "owned by the fix")
    fire(store, stage)
    run = bottleneck_remedy.remedy_pass
    assert run(SLUG, store, FakeLedger(), ledger_doc(), NOW) == []
    assert master_items(store) == []
    assert store.redis.hgetall(store.key(SLUG, bottleneck_remedy.KEY)) == {}

    closed = ledger_doc("done", phase_done=True)
    assert run(SLUG, store, FakeLedger(), closed, NOW)
    [text] = master_items(store)
    assert text.startswith(f"The bottleneck hunter proposes a task for the {stage.replace('_', ' ')} stage")


@pytest.mark.parametrize(("share", "cover_name"), [("review", "merge_queue"), ("review", "review"), ("quota", "quota")])
def test_a_covered_share_holds_no_dispatcher_seat_and_triggers_again_once_the_cover_lapses(share, cover_name):
    store = swarm()
    held(store, share)
    config = store.config(SLUG)
    put(store, cover_name, "phases/p3")
    assert dispatch_seat.open_triggers(SLUG, config, store, {**doc(), **ledger_doc()}, NOW, False) == []
    assert dispatch_seat.refusal(SLUG, config, store, {**doc(), **ledger_doc()}, NOW) == ""

    closed = {**doc(), **ledger_doc(phase_done=True)}
    found = dispatch_seat.open_triggers(SLUG, config, store, closed, NOW, False)
    assert [trigger["id"] for trigger in found] == [f"bottleneck:{share}"]


@pytest.fixture
def crew(monkeypatch):
    store = swarm()
    store.put_agent(SLUG, AgentRecord(MASTER, "master", "master", seat=f"master@{SLUG}"))
    store.put_agent(SLUG, AgentRecord(ENGINEER, "eng", "t1"))
    store.put_agent(SLUG, AgentRecord(DISPATCHER, "dispatch", "dispatcher", seat=f"dispatcher@{SLUG}"))
    ledger = TickLedger([{"id": "t7"}])
    ledger.phases = [{"id": "p3", "done": False}]
    ledger.read, state = [], ledger.state
    ledger.state = lambda slug: ledger.read.append(slug) or state(slug)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    return store


def acting(monkeypatch, name="", swarm_slug=""):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", swarm_slug)


@pytest.mark.parametrize(
    ("name", "by", "item"),
    [("", "operator", "tasks/t7"), (MASTER, MASTER, "phases/p3"), (DISPATCHER, DISPATCHER, "tasks/t7")],
)
def test_the_operator_master_or_dispatcher_covers_a_stage(crew, monkeypatch, capsys, name, by, item):
    acting(monkeypatch, name, SLUG if name else "")
    assert cli.main([SLUG, "cover", "ci overrun", item, "--reason", "owned by the ci fix"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"stage": "ci_overrun", "item": item, "reason": "owned by the ci fix", "by": by, "at": NOW}
    assert stored(crew)["ci_overrun"] == printed
    assert cli.LedgerClient().read == [SLUG]


def test_the_cover_command_requires_a_reason(crew, monkeypatch):
    acting(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main([SLUG, "cover", "ci_overrun", "tasks/t7"])
    assert stored(crew) == {}


@pytest.mark.parametrize("name", [MASTER, DISPATCHER])
def test_the_master_and_dispatcher_cover_under_their_own_names(crew, name):
    assert clearance.coverer(crew, SLUG, Who(name=name, swarm=SLUG)) == name


def test_the_operator_covers_as_the_operator(crew, monkeypatch):
    acting(monkeypatch)
    assert clearance.coverer(crew, SLUG, Who()) == clearance.OPERATOR


def test_an_engineer_is_no_coverer(crew):
    with pytest.raises(SwarmError, match=f"{ENGINEER} is neither"):
        clearance.coverer(crew, SLUG, Who(name=ENGINEER, swarm=SLUG))


def test_an_engineer_cannot_cover_a_stage(crew, monkeypatch, capsys):
    acting(monkeypatch, ENGINEER, SLUG)
    assert cli.main([SLUG, "cover", "ci_overrun", "tasks/t7", "--reason", "mine"]) == 1
    assert "is neither" in capsys.readouterr().err
    assert stored(crew) == {}


def test_the_cover_command_refuses_a_closed_item(crew, monkeypatch, capsys):
    acting(monkeypatch)
    assert cli.main([SLUG, "cover", "ci_overrun", "tasks/t8", "--reason", "owned"]) == 1
    assert "tasks/t8 is not an open phase or task" in capsys.readouterr().err
    assert stored(crew) == {}
