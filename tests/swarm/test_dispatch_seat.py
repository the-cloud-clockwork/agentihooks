from pathlib import Path

import pytest

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import dispatch_seat, naming, prompt, seat_spawn
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.templates import DEFAULT_PROFILES
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "sw"
NOW = 1_800_000_000_000
MINUTE = 60_000
ROLE = Path(__file__).resolve().parents[2] / "profiles" / "package" / "roles" / "dispatcher"


def priority(row_id="pr1", item="questions/q1", text="Pick the release day", age=15 * MINUTE):
    return {"id": row_id, "item": item, "text": text, "at": NOW - age}


def doc(*rows):
    return {"tasks": [], "phases": [], "priorities": list(rows)}


def swarm(autonomy="full"):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", max_eng=0, max_ci=0))
    store.update(SLUG, state="running", autonomy=autonomy)
    return store


def seats(store):
    return [a for a in store.agents(SLUG) if a.lane == dispatch_seat.LANE]


def run(store, runtime, found, now=NOW):
    return dispatch_seat.run(SLUG, store.config(SLUG), store, runtime, found, now)


def test_an_unresolved_priority_at_full_autonomy_spawns_one_seat_whose_prompt_names_it():
    store, runtime = swarm(), FakeRuntime()
    actions = run(store, runtime, doc(priority()))
    [seat] = seats(store)
    assert runtime.spawned == [(dispatch_seat.LANE, seat.name, dispatch_seat.SEAT)]
    assert actions == [f"spawned dispatcher {seat.name} for 1 trigger"]
    assert naming.parse(seat.name).kind == "dispatcher"
    assert seat.seat == seat_address(SLUG, "dispatcher") == f"dispatcher@{SLUG}"
    text = prompt.build(SLUG, "/repo", dispatch_seat.LANE, seat.name, runtime.tasks[0], autonomy="full")
    assert text.startswith(f"You are {seat.name}, the dispatcher of swarm {SLUG}")
    assert "questions/q1" in text and "Pick the release day" in text and "15 minutes" in text
    assert run(store, runtime, doc(priority()), NOW + MINUTE) == []
    assert len(runtime.spawned) == 1


@pytest.mark.parametrize("autonomy", ["manual", "assist", "delegate"])
def test_below_full_autonomy_no_seat_spawns(autonomy):
    store, runtime = swarm(autonomy), FakeRuntime()
    assert run(store, runtime, doc(priority())) == []
    assert runtime.spawned == [] and seats(store) == []


def test_a_priority_younger_than_fifteen_minutes_is_no_trigger():
    store, runtime = swarm(), FakeRuntime()
    assert run(store, runtime, doc(priority(age=15 * MINUTE - 1))) == []
    assert runtime.spawned == []
    assert dispatch_seat.triggers(doc(priority(age=15 * MINUTE)), NOW)[0]["minutes"] == 15


def test_a_live_seat_is_woken_once_by_inbox_for_each_new_trigger():
    store, runtime = swarm(), FakeRuntime()
    run(store, runtime, doc(priority()))
    [seat] = seats(store)
    later = priority("pr2", "followups/f1", "Approve the lane cap", age=20 * MINUTE)
    actions = run(store, runtime, doc(priority(), later), NOW + MINUTE)
    assert actions == [f"woke {seat.name} with 1 new trigger"]
    [item] = InboxStore(store.redis).inbox(seat.name)
    assert item.sender == "swarm" and "followups/f1" in item.text and "questions/q1" not in item.text
    assert run(store, runtime, doc(priority(), later), NOW + 2 * MINUTE) == []
    assert len(runtime.spawned) == 1


def test_the_seat_ends_when_its_triggers_close_or_autonomy_drops():
    store, runtime = swarm(), FakeRuntime()
    run(store, runtime, doc(priority()))
    [seat] = seats(store)
    assert run(store, runtime, doc(), NOW + MINUTE) == [f"ended dispatcher {seat.name}: its triggers closed"]
    assert [a.state for a in seats(store)] == ["finished"]
    store, runtime = swarm(), FakeRuntime()
    run(store, runtime, doc(priority()))
    store.update(SLUG, autonomy="delegate")
    assert run(store, runtime, doc(priority()), NOW + MINUTE)[0].startswith("ended dispatcher")


def test_no_session_slot_holds_the_spawn():
    store, runtime = swarm(), FakeRuntime(full=True)
    assert run(store, runtime, doc(priority())) == ["no session slot for the dispatcher, waiting"]
    assert seats(store) == []


def test_a_failed_spawn_leaves_no_seat_record():
    store, runtime = swarm(), FakeRuntime(fail=True)
    assert run(store, runtime, doc(priority())) == ["dispatcher spawn failed: herdr down"]
    assert seats(store) == []


def test_the_tick_spawns_the_seat_at_full_autonomy_only():
    class Ledger(FakeLedger):
        def state(self, slug):
            return {**super().state(slug), "priorities": [priority()]}

    for autonomy, spawned in (("full", 1), ("delegate", 0)):
        store, runtime = swarm(autonomy), FakeRuntime()
        tick(SLUG, store, Ledger([]), runtime, NOW)
        assert sum(lane == dispatch_seat.LANE for lane, _, _ in runtime.spawned) == spawned


def test_the_seat_spawn_helper_names_records_and_places_a_seat():
    store, runtime = swarm(), FakeRuntime()
    record, placed = seat_spawn.place(
        SLUG, store.config(SLUG), store, runtime, dispatch_seat.LANE, "dispatcher", NOW, lambda r: {"id": r.task}
    )
    assert (record.lane, record.task, record.seat) == ("dispatch", "dispatcher", f"dispatcher@{SLUG}")
    assert placed.pane_id and record.name in runtime.live
    assert store.seats.occupant(record.seat) == record.name


def test_the_dispatcher_naming_type_and_profile():
    name = naming.build("dispatcher", "a1b2c3", 1)
    assert name == "dispatcher@a1b2c3-0001" and naming.lane_of(name) == "dispatch"
    assert naming.PATTERNS["pane"].fullmatch(naming.plain(name))
    assert DEFAULT_PROFILES["dispatch"] == "dispatcher"
    assert "name: dispatcher" in (ROLE / "profile.yml").read_text()
    conditions = {p.name for p in (ROLE / ".claude" / "conditions").iterdir()}
    assert "pre-edit+write+notebookedit-no_code_edits.py" in conditions
    assert "pre-mcp__serena-no_serena_writes.py" in conditions
