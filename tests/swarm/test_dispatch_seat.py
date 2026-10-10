import json
from pathlib import Path

import pytest

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import dispatch_seat, naming, prompt, seat_spawn
from scripts.swarm import tick as tick_module
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
    [item] = InboxStore(store.redis).inbox(f"dispatcher@{SLUG}")
    assert InboxStore(store.redis).inbox(seat.name) == []
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


def test_done_from_the_seat_is_refused_while_a_trigger_is_open_and_names_it():
    store = swarm()
    refused = dispatch_seat.refusal(SLUG, store.config(SLUG), store, doc(priority()), NOW)
    assert refused == (
        "dispatcher triggers are still open; settle them, or the tick ends your seat once they close:\n"
        "- The priority on questions/q1 is unresolved after 15 minutes: Pick the release day"
    )


def test_done_from_the_seat_names_every_open_trigger_one_per_line():
    from scripts.swarm import dev_red

    store = swarm()
    dev_red.hold(store.redis, SLUG, "t9", 77)
    found = {**doc(priority()), "tasks": [{"id": "t9", "state": "blocked"}]}
    assert dispatch_seat.refusal(SLUG, store.config(SLUG), store, found, NOW).splitlines()[1:] == [
        "- The priority on questions/q1 is unresolved after 15 minutes: Pick the release day",
        "- Dev Tests run 77 is red and holds these blocked tasks: t9. Propose a freeze or a focus to the master if one "
        "would help.",
    ]


HANDED = "dispatcher@a1b2c3-0001"


@pytest.mark.parametrize("by", ["master@a1b2c3-0001", "engineer@a1b2c3-0002", "ledger", "dispatcher", None])
def test_a_priority_the_dispatcher_handed_to_the_operator_is_no_trigger_while_others_stay(by):
    handed = {**priority("pr2", "tasks/t9", "Release the seven held tasks"), "by": HANDED}
    other = {**priority("pr3", "followups/f1", "Approve the lane cap"), "by": by}
    assert dispatch_seat.triggers(doc(handed, other), NOW) == [
        {"id": "pr3", "item": "followups/f1", "text": "Approve the lane cap", "minutes": 15}
    ]


def test_done_from_the_seat_passes_with_handed_priorities_open_and_refuses_with_an_unsettled_one():
    store = swarm()
    handed = [
        {**priority(f"pr{n}", f"followups/f{n}", f"Rotate token {n}"), "by": HANDED.replace("0001", f"000{n}")}
        for n in range(1, 4)
    ]
    assert dispatch_seat.refusal(SLUG, store.config(SLUG), store, doc(*handed), NOW) == ""
    unsettled = priority("pr9", "tasks/t9", "Unblock the deploy")
    assert dispatch_seat.refusal(SLUG, store.config(SLUG), store, doc(*handed, unsettled), NOW).splitlines()[1:] == [
        "- The priority on tasks/t9 is unresolved after 15 minutes: Unblock the deploy"
    ]


@pytest.mark.parametrize("autonomy, age", [("full", 15 * MINUTE - 1), ("delegate", 15 * MINUTE)])
def test_done_from_the_seat_passes_once_no_trigger_is_open(autonomy, age):
    store = swarm(autonomy)
    assert dispatch_seat.refusal(SLUG, store.config(SLUG), store, doc(priority(age=age)), NOW) == ""


def test_done_from_the_seat_passes_while_the_swarm_sleeps_as_the_tick_raises_nothing_then():
    store = swarm()
    store.redis.set(store.key(SLUG, "master-retired-tasks"), json.dumps(["t1"]))
    found = {**doc(priority()), "tasks": [{"id": "t1", "state": "done"}]}
    assert dispatch_seat.refusal(SLUG, store.config(SLUG), store, found, NOW) == ""
    woken = {**found, "tasks": [{"id": "t2", "state": "open"}]}
    assert dispatch_seat.refusal(SLUG, store.config(SLUG), store, woken, NOW).startswith("dispatcher triggers")
    assert dispatch_seat.refusal(SLUG, store.config(SLUG), store, found, NOW).startswith("dispatcher triggers")


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
        SLUG, store.config(SLUG), store, runtime, dispatch_seat.LANE, NOW, lambda r: {"id": r.task}
    )
    assert (record.lane, record.task, record.seat) == ("dispatch", "dispatcher", f"dispatcher@{SLUG}")
    assert placed.pane_id and record.name in runtime.live
    assert store.seats.occupant(record.seat).occupant == record.name


def test_the_dispatcher_naming_type_and_profile():
    name = naming.build("dispatcher", "a1b2c3", 1)
    assert name == "dispatcher@a1b2c3-0001" and naming.lane_of(name) == "dispatch"
    assert naming.PATTERNS["pane"].fullmatch(naming.plain(name))
    assert DEFAULT_PROFILES["dispatch"] == "dispatcher"
    assert "name: dispatcher" in (ROLE / "profile.yml").read_text()
    conditions = {p.name for p in (ROLE / ".claude" / "conditions").iterdir()}
    assert "pre-edit+write+notebookedit-no_code_edits.py" in conditions
    assert "pre-mcp__serena-no_serena_writes.py" in conditions


NAME = "dispatcher@a1b2c3-0001"
LED = f"agentihooks ledger --slug {SLUG} --as {NAME}"
PROMPT = f"""You are {NAME}, the dispatcher of swarm {SLUG}, working beside its master in the repo /repo. \
The swarm runs at full autonomy.
The tick woke you because its deterministic passes could not settle these triggers:
- The priority on questions/q1 is unresolved after 15 minutes: Pick the release day
Your seat dispatcher@{SLUG} has no history yet: no handoff document, no recap and no learned notes.

Before anything else, run once: {LED} join. Then read the ledger with agentihooks ledger --slug {SLUG} show and \
each trigger's item in it.
Settle each trigger within the swarm's autonomy with the agentihooks commands, the classifiers and read only sub \
agents: comment on its item with {LED} comment <item> "<text>", close a decided follow up, rank a task, and clear the \
priority once it is resolved with {LED} priority clear <priority id>.
A decision only the operator can make goes to the master; never ask the operator yourself. You never edit code or \
config files, commit, merge or claim a task.
After each trigger, tell the master what you did: agentihooks msg send master@{SLUG} "<plain words>".
New triggers arrive as inbox messages: answer one with agentihooks msg reply <id> "<text>", or close it with \
agentihooks msg close <id> done "<where the work went>".
While you wait on a trigger, declare it: agentihooks swarm {SLUG} wait 30 --reason "<what you wait on>".
When every trigger is closed, run {LED} leave, then agentihooks swarm {SLUG} done and stop: the swarm ends your \
session.
Write ledger comments and messages in plain words: no ids, paths, hashes or dashes.
"""
TRIGGER_LINE = "- The priority on questions/q1 is unresolved after 15 minutes: Pick the release day\n"


def test_the_dispatcher_prompt_is_built_from_its_triggers_and_seat():
    task = {"seat": f"dispatcher@{SLUG}", "triggers": dispatch_seat.triggers(doc(priority()), NOW)}
    assert prompt.build(SLUG, "/repo", dispatch_seat.LANE, NAME, task, autonomy="full") == PROMPT
    bare = prompt.build_dispatcher(SLUG, "/repo", NAME, {"seat": f"dispatcher@{SLUG}"}, "full")
    assert bare == PROMPT.replace(TRIGGER_LINE, "")
    assert "The swarm runs at delegate autonomy." in prompt.build_dispatcher(SLUG, "/repo", NAME, {})


def test_triggers_read_only_stale_priorities_and_tolerate_a_ledger_without_any():
    assert dispatch_seat.triggers({}, NOW) == []
    rows = [priority(), priority("pr2", age=30 * MINUTE + 59_999), priority("pr3", age=MINUTE)]
    assert dispatch_seat.triggers(doc(*rows), NOW) == [
        {"id": "pr1", "item": "questions/q1", "text": "Pick the release day", "minutes": 15},
        {"id": "pr2", "item": "questions/q1", "text": "Pick the release day", "minutes": 30},
    ]


def test_the_spawned_seat_carries_its_triggers_time_and_sent_marks():
    store, runtime = swarm(), FakeRuntime()
    found = doc(priority(), priority("pr2", "followups/f1", "Approve the lane cap"))
    assert run(store, runtime, found) == [f"spawned dispatcher {NAME} for 2 triggers"]
    [seat] = seats(store)
    assert (seat.started_at, seat.task, seat.state, seat.pane_id) == (NOW, "dispatcher", "working", "w1:p1")
    task = runtime.tasks[0]
    assert (task["id"], task["title"], task["seat"]) == ("dispatcher", "Dispatch", f"dispatcher@{SLUG}")
    assert task["triggers"] == dispatch_seat.triggers(found, NOW)
    sent = store.redis.hgetall(store.key(SLUG, dispatch_seat.SENT))
    assert {key: json.loads(value) for key, value in sent.items()} == {t["id"]: t for t in task["triggers"]}


def test_the_wake_message_lists_each_new_trigger():
    store, runtime = swarm(), FakeRuntime()
    run(store, runtime, doc(priority()))
    rows = [priority(), priority("pr2", "followups/f1", "A"), priority("pr3", "tasks/t1", "B", age=16 * MINUTE)]
    assert run(store, runtime, doc(*rows)) == [f"woke {NAME} with 2 new triggers"]
    [item] = InboxStore(store.redis).inbox(f"dispatcher@{SLUG}")
    assert item.text == (
        f"New dispatcher triggers in swarm {SLUG}:\n"
        "- The priority on followups/f1 is unresolved after 15 minutes: A\n"
        "- The priority on tasks/t1 is unresolved after 16 minutes: B\n"
        "Settle each one, then tell the master what you did."
    )


def test_a_finished_seat_is_never_woken_and_the_closed_triggers_are_forgotten():
    store, runtime = swarm(), FakeRuntime()
    run(store, runtime, doc(priority()))
    run(store, runtime, doc(), NOW + MINUTE)
    assert not store.redis.exists(store.key(SLUG, dispatch_seat.SENT))
    actions = run(store, runtime, doc(priority()), NOW + 2 * MINUTE)
    assert actions == ["spawned dispatcher dispatcher@a1b2c3-0002 for 1 trigger"]
    assert InboxStore(store.redis).inbox(f"dispatcher@{SLUG}") == []


def test_a_stopping_or_sleeping_swarm_spawns_no_seat():
    store, runtime = swarm(), FakeRuntime()
    assert dispatch_seat.run(SLUG, store.config(SLUG), store, runtime, doc(priority()), NOW, sleeping=True) == []
    store.update(SLUG, state="stopping")
    assert run(store, runtime, doc(priority())) == []
    assert runtime.spawned == []


def test_a_full_host_holds_the_seat_spawn():
    from tests.swarm.test_host_gate import MEMORY, _decide

    store, runtime = swarm(), FakeRuntime()
    _decide(store, 1, slug=SLUG, granted_at=NOW - MINUTE)
    tick_module._spend_host(store, "earlier", NOW - 1)
    held = f"holding the dispatcher spawn: host memory room 1, 1 spawned since it was granted: {MEMORY}"
    assert run(store, runtime, doc(priority())) == [held]
    assert tick_module.spawn_holds(store, SLUG) == [held]
    assert seat_spawn.host_hold(SLUG, swarm(), NOW, "dispatcher") == ""


def test_a_failed_seat_spawn_names_the_record_and_the_error():
    store, runtime = swarm(), FakeRuntime(fail=True)
    with pytest.raises(seat_spawn.SeatFailed) as raised:
        seat_spawn.place(SLUG, store.config(SLUG), store, runtime, dispatch_seat.LANE, NOW, lambda r: {"id": r.task})
    failed = raised.value
    assert str(failed) == "herdr down" and str(failed.error) == "herdr down"
    assert failed.record.name == NAME and [a.name for a in store.agents(SLUG)] == [NAME]


def test_the_seat_spawn_helper_stamps_the_seat_and_the_host_spend():
    store, runtime = swarm(), FakeRuntime()
    record, _ = seat_spawn.place(
        SLUG, store.config(SLUG), store, runtime, dispatch_seat.LANE, NOW, lambda r: {"id": r.task}
    )
    assert (record.started_at, record.state, record.name) == (NOW, "starting", NAME)
    assert [(e["occupant"], e["at"]) for e in store.seats.history(record.seat)] == [(NAME, NOW)]
    assert store.redis.zrange(tick_module.HOST_SPENDS, 0, -1, withscores=True) == [(NAME, float(NOW))]
    assert runtime.tasks == [{"id": "dispatcher"}]
    assert int(store.names.entry(NAME)["spawned_at"]) == NOW


def test_the_swarm_config_reaches_the_seat_spawn():
    class Recording(FakeRuntime):
        def spawn(self, config, lane, name, task):
            self.configs = [*getattr(self, "configs", []), config]
            return super().spawn(config, lane, name, task)

    store, runtime = swarm(), Recording()
    config = store.config(SLUG)
    dispatch_seat.run(SLUG, config, store, runtime, doc(priority()), NOW)
    assert runtime.configs == [config]


def test_a_sleeping_swarm_tick_spawns_no_dispatcher():
    class Ledger(FakeLedger):
        def state(self, slug):
            return {**super().state(slug), "priorities": [priority()]}

    store, runtime = swarm(), FakeRuntime()
    store.redis.set(store.key(SLUG, "master-retired-tasks"), json.dumps([]))
    tick(SLUG, store, Ledger([]), runtime, NOW)
    assert [lane for lane, _, _ in runtime.spawned if lane == dispatch_seat.LANE] == []


REPORT = {
    "bottleneck": "review",
    "window_hours": 4,
    "total": 7200.0,
    "seconds": {"engineering": 1800.0, "ci": 1800.0, "review": 3600.0, "host": 0.0, "quota": 0.0},
    "at": NOW - 5 * MINUTE,
}


def held(store, named="review", ticks=3):
    from scripts.swarm import bottleneck, lane_split

    store.redis.set(store.key(SLUG, bottleneck.KEY), json.dumps({**REPORT, "bottleneck": named}))
    store.redis.set(store.key(SLUG, lane_split.KEY), json.dumps({"named": named, "ticks": ticks, "at": REPORT["at"]}))


def test_a_bottleneck_no_lane_rule_covers_spawns_a_seat_naming_it():
    from scripts.swarm import bottleneck

    store, runtime = swarm(), FakeRuntime()
    held(store)
    assert run(store, runtime, doc()) == [f"spawned dispatcher {NAME} for 1 trigger"]
    [trigger] = runtime.tasks[0]["triggers"]
    report = bottleneck.line(REPORT, NOW)
    assert trigger == {"id": "bottleneck:review", "kind": "bottleneck", "ticks": 3, "text": report}
    text = prompt.build(SLUG, "/repo", dispatch_seat.LANE, NAME, runtime.tasks[0], autonomy="full")
    assert (
        f"- The bottleneck report named the same share 3 ticks running and no lane rule covers it: {report}\n" in text
    )
    assert report.startswith("bottleneck review and queue: 50 percent")


@pytest.mark.parametrize(("named", "ticks"), [("review", 2), ("ci", 3), ("engineering", 4), ("", 3)])
def test_a_short_or_covered_bottleneck_is_no_trigger(named, ticks):
    store = swarm()
    held(store, named, ticks)
    assert dispatch_seat.uncovered(store, SLUG, NOW) == []
    assert dispatch_seat.uncovered(swarm(), SLUG, NOW) == []


def test_a_bottleneck_whose_report_stopped_fifteen_minutes_ago_closes():
    store, runtime = swarm(), FakeRuntime()
    held(store)
    fresh = REPORT["at"] + dispatch_seat.STALE_MS
    assert [t["id"] for t in dispatch_seat.uncovered(store, SLUG, fresh)] == ["bottleneck:review"]
    assert dispatch_seat.uncovered(store, SLUG, fresh + 1) == []
    run(store, runtime, doc())
    assert run(store, runtime, doc(), fresh + 1) == [f"ended dispatcher {NAME}: its triggers closed"]


def test_a_red_dev_holding_blocked_tasks_asks_for_a_freeze_or_focus():
    from scripts.swarm import dev_red

    store, runtime = swarm(), FakeRuntime()
    for task_id, run_id in (("t2", 41), ("t1", 42), ("t3", 43)):
        dev_red.hold(store.redis, SLUG, task_id, run_id)
    tasks = [{"id": "t2", "state": "blocked"}, {"id": "t1", "state": "blocked"}, {"id": "t3", "state": "open"}]
    found = {**doc(), "tasks": tasks}
    assert dispatch_seat.red_dev(store, SLUG, found) == [
        {"id": "dev-red:42", "kind": "freeze", "run": 42, "text": "t1, t2"}
    ]
    assert run(store, runtime, found) == [f"spawned dispatcher {NAME} for 1 trigger"]
    text = prompt.build(SLUG, "/repo", dispatch_seat.LANE, NAME, runtime.tasks[0], autonomy="full")
    assert (
        "- Dev Tests run 42 is red and holds these blocked tasks: t1, t2. Propose a freeze or a focus to the master "
        "if one would help.\n" in text
    )
    assert dispatch_seat.red_dev(store, SLUG, {**doc(), "tasks": tasks[2:]}) == []
    assert dispatch_seat.red_dev(swarm(), SLUG, found) == []
    assert dispatch_seat.red_dev(store, SLUG, {}) == []


def test_every_trigger_kind_wakes_the_live_seat_once_and_closes_with_its_signal():
    from scripts.swarm import dev_red

    store, runtime = swarm(), FakeRuntime()
    run(store, runtime, doc(priority()))
    held(store)
    dev_red.hold(store.redis, SLUG, "t1", 42)
    found = {**doc(priority()), "tasks": [{"id": "t1", "state": "blocked"}]}
    assert run(store, runtime, found, NOW + MINUTE) == [f"woke {NAME} with 2 new triggers"]
    [item] = InboxStore(store.redis).inbox(f"dispatcher@{SLUG}")
    assert "no lane rule covers it" in item.text and "Dev Tests run 42 is red" in item.text
    assert run(store, runtime, found, NOW + 2 * MINUTE) == []
    store.update(SLUG, autonomy="delegate")
    assert run(store, runtime, found, NOW + 3 * MINUTE) == [f"ended dispatcher {NAME}: its triggers closed"]
