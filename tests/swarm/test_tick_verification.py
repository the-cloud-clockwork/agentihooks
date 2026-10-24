import pytest

from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, store, tasks, workers  # noqa: F401


@pytest.mark.parametrize("pr_url", ["", "https://github.com/org/repo/pull/1"])
def test_a_released_verification_owner_frees_its_seat_without_reclaiming(store, pr_url):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = workers(store)[0]
    ledger.rows["t1"].update(
        state="open",
        claimed_by="",
        claim_generation=2,
        awaiting="verify",
        contract={"after_at": 100_000_000_000_000},
        pr_url=pr_url,
    )
    store.release("sw", "t1", owner.name)
    released = dict(ledger.rows["t1"])
    ledger.rows["t2"] = {"id": "t2", "state": "open", "claimed_by": "", "lane": "eng"}
    for at in (2_000, 3_000):
        tick("sw", store, ledger, runtime, now_ms=at)
        assert ledger.rows["t1"] == released
        assert store.claimant("sw", "t1") is None
        successor = workers(store)[0]
        assert successor.task == "t2"
        assert successor.name != owner.name
        assert successor.seat == owner.seat
        assert store.seats.occupant(owner.seat).occupant == successor.name
    assert runtime.killed == [owner.name]
    assert len(runtime.spawned) == 2


@pytest.mark.parametrize("generation", [1, 2])
def test_a_verification_release_at_the_repair_write_frees_the_owner(store, monkeypatch, generation):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = workers(store)[0]
    store.put_handoff("sw", "t1", "Retained measurement handoff")
    ledger.rows["t1"].update(state="open", claimed_by="")
    update = ledger.update_task

    def released(slug, task_id, fields, by="swarm", if_state=(), if_match=None):
        ledger.rows[task_id].update(
            awaiting="verify", contract={"after_at": 100_000_000_000_000}, claim_generation=generation
        )
        return update(slug, task_id, fields, by, if_state, if_match)

    monkeypatch.setattr(ledger, "update_task", released)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claim_generation"] == generation
    assert store.claimant("sw", "t1") is None
    assert workers(store) == []
    assert store.seats.left(owner.name, owner.seat) is True
    assert runtime.killed == [owner.name]


@pytest.mark.parametrize("state", ["open", "claimed", "pr"])
@pytest.mark.parametrize("awaiting", ["", "reply"])
def test_active_owner_repair_keeps_its_seat_and_restores_its_claim(store, state, awaiting):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = workers(store)[0]
    ledger.rows["t1"].update(state=state, claimed_by="", awaiting=awaiting)
    store.release("sw", "t1", owner.name)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert ledger.rows["t1"]["claimed_by"] == owner.name
    assert ledger.rows["t1"]["state"] == ("claimed" if state == "open" else state)
    assert store.claimant("sw", "t1") == owner.name
    assert store.seats.occupant(owner.seat).occupant == owner.name
    assert runtime.killed == []
    assert len(runtime.spawned) == 1


@pytest.mark.parametrize("state", ["claimed", "pr"])
def test_a_verification_task_with_its_owner_keeps_running(store, state):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = workers(store)[0]
    ledger.rows["t1"].update(state=state, awaiting="verify")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert ledger.rows["t1"]["claimed_by"] == owner.name
    assert store.claimant("sw", "t1") == owner.name
    assert workers(store)[0].name == owner.name
    assert runtime.killed == []


def test_concurrent_verification_releases_report_each_retirement_and_retry_time(store, monkeypatch):  # noqa: F811
    from scripts.swarm import retire_watch

    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first, second = sorted(workers(store), key=lambda agent: agent.name)
    runtime.stuck.add(first.name)
    for row in ledger.rows.values():
        row.update(state="open", claimed_by="")
    update = ledger.update_task

    def released(slug, task_id, fields, by="swarm", if_state=(), if_match=None):
        ledger.rows[task_id].update(awaiting="verify", claim_generation=2)
        return update(slug, task_id, fields, by, if_state, if_match)

    monkeypatch.setattr(ledger, "update_task", released)
    actions = tick("sw", store, ledger, runtime, now_ms=2_000)
    assert f"could not retire {first.name}, retrying next tick" in actions
    assert f"retired {second.name}" in actions
    assert retire_watch.rows(store, "sw") == [
        {"agent": first.name, "ticks": 1, "since": 2_000, "at": 2_000, "process": 0, "refusal": "stuck"}
    ]
    assert all(row["claimed_by"] == "" for row in ledger.rows.values())
    assert [agent.name for agent in workers(store)] == [first.name]
    runtime.stuck.clear()
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert workers(store) == []
    assert retire_watch.rows(store, "sw") == []
