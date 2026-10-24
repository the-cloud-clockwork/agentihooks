import pytest

from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, store, tasks, workers  # noqa: F401


@pytest.mark.parametrize("pr_url", ["", "https://github.com/org/repo/pull/1"])
def test_a_released_verification_owner_frees_its_seat_without_reclaiming(store, pr_url):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = workers(store)[0]
    ledger.rows["t1"].update(
        state="open", claimed_by="", claim_generation=2, awaiting="verify", after_at=100_000, pr_url=pr_url
    )
    store.release("sw", "t1", owner.name)
    released = dict(ledger.rows["t1"])
    for at in (2_000, 3_000):
        tick("sw", store, ledger, runtime, now_ms=at)
        assert ledger.rows["t1"] == released
        assert store.claimant("sw", "t1") is None
        assert workers(store) == []
        assert store.seats.occupant(owner.seat).occupant == ""
    assert runtime.killed == [owner.name]
    assert len(runtime.spawned) == 1


@pytest.mark.parametrize("generation", [1, 2])
def test_a_verification_release_at_the_repair_write_frees_the_owner(store, monkeypatch, generation):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = workers(store)[0]
    ledger.rows["t1"].update(state="open", claimed_by="")
    update = ledger.update_task

    def released(slug, task_id, fields, by="swarm", if_state=(), if_match=None):
        ledger.rows[task_id].update(awaiting="verify", after_at=100_000, claim_generation=generation)
        return update(slug, task_id, fields, by, if_state, if_match)

    monkeypatch.setattr(ledger, "update_task", released)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claim_generation"] == generation
    assert store.claimant("sw", "t1") is None
    assert workers(store) == []
    assert store.seats.occupant(owner.seat).occupant == ""
    assert runtime.killed == [owner.name]


@pytest.mark.parametrize("state", ["open", "claimed", "pr"])
def test_active_owner_repair_keeps_its_seat_and_restores_its_claim(store, state):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    owner = workers(store)[0]
    ledger.rows["t1"].update(state=state, claimed_by="")
    store.release("sw", "t1", owner.name)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert ledger.rows["t1"]["claimed_by"] == owner.name
    assert ledger.rows["t1"]["state"] == ("claimed" if state == "open" else state)
    assert store.claimant("sw", "t1") == owner.name
    assert store.seats.occupant(owner.seat).occupant == owner.name
    assert runtime.killed == []
    assert len(runtime.spawned) == 1
