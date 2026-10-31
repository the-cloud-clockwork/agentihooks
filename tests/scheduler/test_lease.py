import json
from threading import Event
from unittest.mock import Mock

import pytest

from scripts.events.redis_state import RedisStateStore

pytestmark = pytest.mark.unit


@pytest.fixture
def state():
    import fakeredis

    return RedisStateStore(fakeredis.FakeRedis(decode_responses=True), "scheduler")


class InlineThread:
    def __init__(self, *, target, args, daemon):
        self.target, self.args = target, args
        assert daemon is True
        self.joined = False

    def start(self):
        self.target(*self.args)

    def join(self):
        self.joined = True


@pytest.mark.parametrize("lose_ownership", [False, True])
def test_dispatch_lease_renews_until_stopped_and_joins_before_release(monkeypatch, state, lose_ownership):
    from scripts.scheduler import lease

    stopped = Mock(spec=Event)
    stopped.wait.side_effect = [False, True]
    renewed = []
    renew = lease.renew

    def counted(store, held, **kwargs):
        renewed.append(held.token)
        if lose_ownership:
            raise RuntimeError("scheduler lost its dispatch lock")
        renew(store, held, **kwargs)

    monkeypatch.setattr(lease, "Event", lambda: stopped)
    monkeypatch.setattr(lease, "Thread", InlineThread)
    monkeypatch.setattr(lease, "renew", counted)
    with lease.dispatch_lock(state, "proof.lock") as held:
        lease.verify_lease(state, held)
        stopped.set.assert_not_called()
    assert renewed == [held.token]
    assert stopped.wait.call_args_list[0].args == (lease.RENEW_SECONDS,)
    stopped.set.assert_called_once_with()
    assert "lease" not in json.loads(state.get("proof.lock").value)


def test_renewal_extends_the_expiry_and_a_stale_lease_cannot_renew(state):
    from scripts.scheduler import lease

    held = lease.acquire(state, "proof.lock", now=100.0)
    lease.renew(state, held, now=120.0)
    lease.verify_lease(state, held, now=100.0 + lease.LEASE_SECONDS + 5)
    with pytest.raises(RuntimeError, match="^scheduler lost its dispatch lock$"):
        lease.renew(state, held, now=200.0)


def test_a_conflicted_renewal_renews_again_on_the_next_beat(monkeypatch, state):
    from scripts.events.bus import StateConflict
    from scripts.scheduler import lease

    held = lease.acquire(state, "proof.lock")
    stopped = Mock(spec=Event)
    stopped.wait.side_effect = [False, False, True]
    calls, renew = [], lease.renew

    def conflicted_once(store, renewed, **kwargs):
        calls.append(renewed.token)
        if len(calls) == 1:
            raise StateConflict("scheduler lease 'proof.lock' kept changing")
        renew(store, renewed, **kwargs)

    monkeypatch.setattr(lease, "renew", conflicted_once)
    lease._renew(state, held, stopped)
    assert calls == [held.token, held.token]
    lease.verify_lease(state, held)


def test_a_lost_lease_ends_the_renewal_without_another_beat(monkeypatch, state):
    from scripts.scheduler import lease

    held = lease.acquire(state, "proof.lock", now=100.0)
    stopped = Mock(spec=Event)
    stopped.wait.side_effect = [False, False, True]
    lease._renew(state, held, stopped)
    assert stopped.wait.call_count == 1


def test_a_renewal_stamps_its_expiry_at_the_attempt_that_writes(monkeypatch, state):
    from scripts.events.bus import StateConflict
    from scripts.scheduler import lease

    held = lease.acquire(state, "proof.lock", now=100.0)
    clock = iter([110.0, 125.0])
    monkeypatch.setattr(lease.time, "time", lambda: next(clock))
    update, raced = state.update, []

    def conflicted_once(key, value, revision):
        if not raced:
            raced.append(revision)
            raise StateConflict(f"state key {key!r} is not at revision {revision}")
        return update(key, value, revision)

    monkeypatch.setattr(state, "update", conflicted_once)
    lease.renew(state, held)
    assert json.loads(state.get("proof.lock").value)["lease"] == {"token": held.token, "expires": 155.0}


def test_a_lost_lock_and_a_busy_lock_name_their_own_errors(state):
    from scripts.scheduler import lease

    held = lease.acquire(state, "proof.lock", now=100.0)
    with pytest.raises(lease.Busy, match="^another scheduler is dispatching on this machine$"):
        lease.acquire(state, "proof.lock", now=101.0)
    with pytest.raises(lease.LostLock, match="^scheduler lost its dispatch lock$"):
        lease.verify_lease(state, held, now=100.0 + lease.LEASE_SECONDS)
    assert issubclass(lease.LostLock, RuntimeError) and issubclass(lease.Busy, RuntimeError)


def test_release_leaves_a_successor_lease_in_place(state):
    from scripts.scheduler import lease

    held = lease.acquire(state, "proof.lock", now=100.0)
    successor = lease.acquire(state, "proof.lock", now=100.0 + lease.LEASE_SECONDS)
    lease.release(state, held)
    lease.verify_lease(state, successor, now=100.0 + lease.LEASE_SECONDS + 1)


def test_a_held_wait_retries_until_the_holder_releases(monkeypatch, state):
    from scripts.scheduler import lease

    first = lease.acquire(state, "proof.lock")
    sleeps = []
    monkeypatch.setattr(lease.time, "sleep", lambda seconds: (sleeps.append(seconds), lease.release(state, first)))
    with lease.held(state, "proof.lock", 5) as second:
        lease.verify_lease(state, second)
    assert sleeps == [lease.WAIT_POLL_S]
    with pytest.raises(RuntimeError, match="^another scheduler"):
        with lease.held(state, "proof.lock", 0):
            with lease.held(state, "proof.lock", 0):
                pass


@pytest.mark.parametrize("decode", [False, True])
def test_fenced_dispatch_accepts_both_redis_response_encodings(monkeypatch, decode):
    import fakeredis

    from scripts.scheduler import lease
    from scripts.scheduler.core import Scheduler

    monkeypatch.setattr(lease, "Thread", Mock())
    scheduler = Scheduler(RedisStateStore(fakeredis.FakeRedis(decode_responses=decode), "scheduler"), machine="proof")
    timer = scheduler.timer(owner="operator", payload="notice", delay=1, now=0)
    timer.start(now=0)
    assert scheduler.run_due(now=1) == 1
    assert timer.state == "fired"


@pytest.mark.parametrize("stored", [None, {"token": "successor", "expires": 10**12}, {"token": "owner", "expires": 0}])
def test_lease_verification_rejects_absent_changed_or_expired_ownership(state, stored):
    from scripts.scheduler import lease

    if stored is not None:
        state.create("proof.lock", json.dumps({"lease": stored}).encode())
    with pytest.raises(RuntimeError, match="^scheduler lost its dispatch lock$"):
        lease.verify_lease(state, lease.Lease("proof.lock", "owner"))


def test_a_lease_is_written_with_sorted_keys(state, monkeypatch):
    from scripts.scheduler import lease

    monkeypatch.setattr(lease.uuid, "uuid4", lambda: Mock(hex="owner"))
    state.create("proof.lock", b'{"z": 1}')
    lease.acquire(state, "proof.lock", now=100.0)
    assert state.get("proof.lock").value == b'{"lease": {"expires": 130.0, "token": "owner"}, "z": 1}'


def test_a_lease_is_lost_at_its_exact_expiry(state):
    from scripts.scheduler import lease

    held = lease.acquire(state, "proof.lock", now=100.0)
    with pytest.raises(RuntimeError, match="^scheduler lost its dispatch lock$"):
        lease.verify_lease(state, held, now=100.0 + lease.LEASE_SECONDS)


def test_a_release_by_a_former_holder_writes_nothing(state):
    from scripts.scheduler import lease

    lease.acquire(state, "proof.lock", now=100.0)
    before = state.get("proof.lock")
    lease.release(state, lease.Lease("proof.lock", "stranger"))
    assert state.get("proof.lock") == before


def test_a_conflicting_lease_write_retries_on_the_new_revision(state, monkeypatch):
    from scripts.events.bus import StateConflict
    from scripts.scheduler import lease

    create = state.create

    def racing(key, value):
        monkeypatch.setattr(state, "create", create)
        create(key, b'{"z": 1}')
        raise StateConflict("raced")

    monkeypatch.setattr(state, "create", racing)
    held = lease.acquire(state, "proof.lock", now=100.0)
    lease.verify_lease(state, held, now=101.0)
    assert json.loads(state.get("proof.lock").value)["z"] == 1


def test_a_lease_that_never_settles_is_refused(state, monkeypatch):
    from scripts.events.bus import StateConflict
    from scripts.scheduler import lease

    attempts = []

    def conflict(key, value):
        attempts.append(key)
        raise StateConflict("raced")

    monkeypatch.setattr(state, "create", conflict)
    with pytest.raises(StateConflict) as refused:
        lease.acquire(state, "proof.lock", now=100.0)
    assert str(refused.value) == "scheduler lease 'proof.lock' kept changing"
    assert attempts == ["proof.lock"] * lease.WRITE_TRIES


def test_a_held_wait_gives_up_at_its_exact_deadline(state, monkeypatch):
    from scripts.scheduler import lease

    lease.acquire(state, "proof.lock")
    monkeypatch.setattr(lease.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(lease.time, "sleep", lambda seconds: pytest.fail(f"slept {seconds}"))
    with pytest.raises(RuntimeError, match="^another scheduler"):
        with lease.held(state, "proof.lock", 0):
            pass
