import json
from dataclasses import replace

import pytest

from scripts.handoff import transfers
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import SpawnError, tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.unit
DOC = "# Handoff v2\n## Next\nCheck the saved proof and verify it passed.\n## Read first\nNone\n"


@pytest.fixture
def setup():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    old = AgentRecord("sw-eng-1", "eng", "t1", seat="eng-1@sw", state="finished")
    store.put_agent("sw", old)
    store.seats.occupy(old.seat, old.name, 1)
    return store, old


def test_tick_binds_a_successor_without_claiming_continuity(setup):
    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 2)
    store.put_handoff("sw", "t1", DOC, seat=old.seat)
    runtime = FakeRuntime()
    tick("sw", store, FakeLedger([{"id": "t1"}]), runtime, 10)
    result = transfers.get(store, "sw", transfer["id"])
    assert result["successor"] == runtime.spawned[0][1]
    assert result["binding"]["state"] == "live"
    assert result["continuity"]["state"] == "pending"
    assert runtime.tasks[0]["transfer"]["id"] == transfer["id"]


@pytest.mark.parametrize("lane", ["eng", "master"])
def test_binding_is_stamped_when_the_successor_attaches_not_at_tick_start(setup, monkeypatch, lane):
    store, old = setup
    store.drop_agent("sw", old.name)
    old = replace(old, lane=lane, task="master" if lane == "master" else "t1")
    store.put_agent("sw", old)
    store.put_handoff("sw", old.task, DOC, seat=old.seat)
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 30)
    clock = iter(range(100, 200))
    monkeypatch.setattr(transfers, "now_ms", lambda: next(clock))
    tick("sw", store, FakeLedger([] if lane == "master" else [{"id": "t1"}]), FakeRuntime(), 10)
    result = transfers.get(store, "sw", transfer["id"])
    assert (result["at"], result["attached_at"]) == (30, 100)
    assert result["binding"] == {"state": "live", "at": 101, "session": result["successor"]}
    assert result["successor"]


def test_a_failed_launch_stamps_its_absent_binding_when_it_fails(setup, monkeypatch):
    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 30)
    new = replace(old, name="sw-eng-2")
    store.seats.occupy(new.seat, new.name, 3)
    clock = iter(range(100, 200))
    monkeypatch.setattr(transfers, "now_ms", lambda: next(clock))
    transfers.attach(store, "sw", new)
    transfers.failed(store, "sw", new)
    result = transfers.get(store, "sw", transfer["id"])
    assert result["binding"] == {"state": "absent", "at": 101, "session": "sw-eng-2", "reason": "Launch failed"}
    retry = next(r for r in transfers.list_transfers(store, "sw") if r.get("retry_of") == transfer["id"])
    assert retry["at"] == 101


@pytest.mark.parametrize("state", ["starting", "launching"])
@pytest.mark.parametrize("lane", ["eng", "ci", "master"])
def test_a_successor_whose_launch_is_unrecorded_keeps_a_pending_binding(setup, state, lane):
    from scripts.swarm.tick import observe_bindings

    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 30)
    new = replace(old, name="sw-eng-2", lane=lane, state=state)
    store.put_agent("sw", new)
    store.seats.occupy(new.seat, new.name, 3)
    transfers.attach(store, "sw", new)
    runtime = FakeRuntime()
    observe_bindings(store, "sw", runtime)
    assert transfers.get(store, "sw", transfer["id"])["binding"] == {"state": "pending"}
    store.put_agent("sw", replace(new, state="working", launched_at=40))
    runtime.live.add(new.name)
    observe_bindings(store, "sw", runtime)
    assert transfers.get(store, "sw", transfer["id"])["binding"]["state"] == "live"


def test_a_launched_successor_missing_from_the_live_names_is_absent(setup):
    from scripts.swarm.tick import observe_bindings

    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 30)
    new = replace(old, name="sw-eng-2", state="working", launched_at=40)
    store.put_agent("sw", new)
    store.seats.occupy(new.seat, new.name, 3)
    transfers.attach(store, "sw", new)
    observe_bindings(store, "sw", FakeRuntime())
    assert transfers.get(store, "sw", transfer["id"])["binding"]["state"] == "absent"


def test_a_live_binding_is_kept_when_the_successor_later_leaves_the_live_names(setup):
    from scripts.swarm.tick import observe_bindings

    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 30)
    new = replace(old, name="sw-eng-2", state="working", launched_at=40)
    store.put_agent("sw", new)
    store.seats.occupy(new.seat, new.name, 3)
    transfers.attach(store, "sw", new)
    runtime = FakeRuntime()
    runtime.live.add(new.name)
    observe_bindings(store, "sw", runtime)
    bound = transfers.get(store, "sw", transfer["id"])["binding"]
    runtime.live.discard(new.name)
    observe_bindings(store, "sw", runtime)
    assert transfers.get(store, "sw", transfer["id"])["binding"] == bound
    assert bound["state"] == "live"


def test_detach_frees_only_the_waiting_spawns_unbound_transfers(setup):
    store, old = setup
    other = transfers.record(store, "sw", old, "recycle", DOC, 10)
    live = transfers.record(store, "sw", old, "recycle", DOC, 20)
    waiting = transfers.record(store, "sw", old, "recycle", DOC, 30)
    rows = {
        other["id"]: {**other, "successor": "sw-eng-9"},
        live["id"]: {**live, "successor": "sw-eng-2", "binding": {"state": "live"}},
        waiting["id"]: {**waiting, "successor": "sw-eng-2"},
    }
    for row in rows.values():
        store.redis.hset(store.key("sw", "transfers"), row["id"], json.dumps(row))
    transfers.detach(store, "sw", replace(old, name="sw-eng-2"))
    assert transfers.list_transfers(store, "sw") == [
        rows[other["id"]],
        rows[live["id"]],
        {**rows[waiting["id"]], "successor": ""},
    ]


def test_a_spawn_waiting_on_a_full_account_leaves_its_task_and_transfer_for_the_next_spawn(setup):
    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 30)
    store.put_handoff("sw", "t1", DOC, seat=old.seat)
    ledger = FakeLedger([{"id": "t1"}])
    full = SpawnError("no approved account has quota and a free launch slot", "unavailable")
    tick("sw", store, ledger, FakeRuntime(crash=full), 10)
    [row] = transfers.list_transfers(store, "sw")
    assert (row["id"], row["successor"], row["binding"]) == (transfer["id"], "", {"state": "pending"})
    assert ledger.rows["t1"]["state"] == "open"
    assert store.claims("sw", "t1") == 0
    assert store.launch_failure("sw", "t1") == ""
    assert [row["state"] for row in store.launches("sw")] == ["waiting"]
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, 20)
    assert transfers.get(store, "sw", transfer["id"])["successor"] == runtime.spawned[0][1]


def test_now_ms_reads_the_wall_clock_in_milliseconds(monkeypatch):
    monkeypatch.setattr(transfers.time, "time", lambda: 12.3456)
    assert transfers.now_ms() == 12345


def test_successor_confirms_reading_and_the_first_next_action(setup):
    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 2)
    new = replace(old, name="sw-eng-2", state="working", pane_id="pane")
    store.put_agent("sw", new)
    store.seats.occupy(new.seat, new.name, 3)
    transfers.attach(store, "sw", new)
    result = transfers.confirm(store, "sw", transfer["id"], new, "Check the saved proof and verify it passed.", 4)
    assert result["continuity"] == {
        "state": "confirmed",
        "at": 4,
        "by": "sw-eng-2",
        "next": "Check the saved proof and verify it passed.",
    }
    assert result["binding"]["state"] == "pending"
    assert transfers.get(store, "sw", transfer["id"]) == result


def test_a_different_next_action_or_an_old_occupant_cannot_confirm(setup):
    store, old = setup
    transfer = transfers.record(store, "sw", old, "recycle", DOC, 2)
    new = replace(old, name="sw-eng-2", state="working")
    store.put_agent("sw", new)
    store.seats.occupy(new.seat, new.name, 3)
    transfers.attach(store, "sw", new)
    with pytest.raises(SwarmError, match="first Next action"):
        transfers.confirm(store, "sw", transfer["id"], new, "Build something else", 4)
    store.seats.occupy(new.seat, "sw-eng-3", 5)
    with pytest.raises(SwarmError, match="occupant"):
        transfers.confirm(store, "sw", transfer["id"], new, transfer["next"], 6)


def test_a_missing_handoff_is_an_explicit_continuity_gap(setup):
    store, old = setup
    transfer = transfers.record(store, "sw", old, "restore", "", 2)
    assert transfer["continuity"]["state"] == "unknown"
    assert transfer["continuity"]["gap"] == "No handoff document"


@pytest.mark.parametrize("lane", ["eng", "ci", "master"])
def test_a_failed_successor_keeps_its_outcome_and_a_retry_can_confirm(setup, monkeypatch, lane):
    store, old = setup
    store.drop_agent("sw", old.name)
    old = replace(old, lane=lane, task="master" if lane == "master" else "t1")
    store.put_agent("sw", old)
    store.update("sw", max_eng=int(lane == "eng"), max_ci=int(lane == "ci"))
    original = transfers.record(store, "sw", old, "recycle", DOC, 2)
    store.put_handoff("sw", old.task, DOC, seat=old.seat)
    runtime = FakeRuntime()
    spawn = runtime.spawn

    def fail(config, target_lane, *args, **kwargs):
        if target_lane == lane:
            raise RuntimeError("launch failed")
        return spawn(config, target_lane, *args, **kwargs)

    monkeypatch.setattr(runtime, "spawn", fail)
    ledger = FakeLedger([] if lane == "master" else [{"id": "t1", "lane": lane}])
    tick("sw", store, ledger, runtime, 10)
    assert transfers.get(store, "sw", original["id"])["binding"]["state"] == "absent"
    monkeypatch.setattr(runtime, "spawn", spawn)
    tick("sw", store, ledger, runtime, 20)
    task = runtime.masters[-1][1] if lane == "master" else runtime.tasks[-1]
    retry = task["transfer"]
    assert retry is not None
    assert retry["id"] != original["id"]
    successor = next(a for a in store.agents("sw") if a.name == retry["successor"])
    result = transfers.confirm(store, "sw", retry["id"], successor, retry["next"], 21)
    assert result["binding"]["state"] == "live"
    assert result["continuity"]["state"] == "confirmed"


def test_codex_master_confirms_the_adapted_next_without_changing_the_record(setup):
    from scripts.profiles.codex_master import next_action

    store, old = setup
    old = replace(old, lane="master", task="master", seat="master@sw")
    store.seats.occupy(old.seat, old.name, 1)
    action = "Rearm a Monitor on the ledger."
    document = "# Handoff v2\n## Next\n" + action + "\n## Read first\nNone\n"
    transfer = transfers.record(store, "sw", old, "recycle", document, 2)
    new = replace(old, name="master-new", harness="codex", state="working")
    store.seats.occupy(new.seat, new.name, 3)
    transfers.attach(store, "sw", new)

    result = transfers.confirm(store, "sw", transfer["id"], new, next_action(action, "sw"), 4)
    assert result["continuity"]["state"] == "confirmed"
    assert result["continuity"]["next"] == next_action(action, "sw")
    assert result["next"] == action
    assert result["handoff"] == document
    with pytest.raises(SwarmError, match="exactly"):
        transfers.confirm(store, "sw", transfer["id"], new, "some other action", 5)


@pytest.mark.parametrize(("lane", "harness"), [("master", "claude"), ("eng", "codex")])
def test_other_successors_keep_the_original_monitor_next(setup, lane, harness):
    from scripts.profiles.codex_master import next_action

    store, old = setup
    old = replace(old, lane=lane, task="master" if lane == "master" else "t1", seat=f"{lane}@sw")
    store.seats.occupy(old.seat, old.name, 1)
    action = "Rearm a Monitor on the ledger."
    transfer = transfers.record(store, "sw", old, "recycle", "# Handoff v2\n## Next\n" + action, 2)
    new = replace(old, name="successor", harness=harness, state="working")
    store.seats.occupy(new.seat, new.name, 3)
    transfers.attach(store, "sw", new)
    with pytest.raises(SwarmError, match="exactly"):
        transfers.confirm(store, "sw", transfer["id"], new, next_action(action, "sw"), 4)
    assert transfers.confirm(store, "sw", transfer["id"], new, action, 5)["continuity"]["state"] == "confirmed"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_transfer_next_uses_valid_heading_whitespace(newline):
    action = "Read the inbox."
    assert transfers.first_next(newline.join(["## Next   ", "- " + action, "## Read first", "None"])) == action


@pytest.mark.parametrize("text", ["", "# Handoff v2\n## Intent\nNone\n", "## Next\n\n"])
def test_a_handoff_without_a_next_action_has_an_empty_confirmation(text):
    assert transfers.first_next(text) == ""


def test_next_action_keeps_leading_letters_and_removes_only_bullet_spacing():
    assert transfers.first_next("## Next\n- Xray the saved proof.\n## Read first\nNone\n") == "Xray the saved proof."
