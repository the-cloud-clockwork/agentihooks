"""The master launch lock of a swarm: one Redis set if absent key with a lease, taken by every master starter before
it launches, released when its launch ends and never released by a holder that lost it."""

from typing import Any

import pytest

from scripts.swarm import master_launch, master_lock
from scripts.swarm.store import SwarmError
from scripts.tick.steps import masters
from tests.swarm.test_cli import env  # noqa: F401
from tests.swarm.test_master_launch import ResumingRuntime, up  # noqa: F401

pytestmark = pytest.mark.unit

NOW = 1_791_380_000_000
LEASE_MS = 5 * 60 * 1000


@pytest.fixture
def swarm(up: tuple[Any, Any, ResumingRuntime]) -> tuple[Any, ResumingRuntime]:  # noqa: F811
    """The created swarm sw with no master, and its runtime."""
    store, _, rt = up
    return store, rt


def held_by(store: Any) -> str | None:
    """The raw value of swarm sw's master launch lock, None when nobody holds it."""
    return store.redis.get(store.key("sw", "master-launch-lock"))


@pytest.mark.feature("master-launch-lock")
def test_the_lock_is_the_swarm_key_set_if_absent_with_its_lease_and_holder(swarm: tuple[Any, ResumingRuntime]) -> None:
    store, _ = swarm
    assert master_lock.key(store, "sw") == store.key("sw", "master-launch-lock")
    with master_lock.held(store, "sw", "tick master") as refused:
        assert refused == ""
        assert str(held_by(store)).startswith("tick master ")
        assert LEASE_MS - 1000 < store.redis.pttl(master_lock.key(store, "sw")) <= LEASE_MS
        with master_lock.held(store, "sw", "master up") as second:
            assert second == "the master launch lock of swarm sw is held by tick master, so no second master starts"
        assert str(held_by(store)).startswith("tick master ")
    assert held_by(store) is None


@pytest.mark.feature("master-launch-lock")
def test_the_lock_is_released_when_its_launch_raises(swarm: tuple[Any, ResumingRuntime]) -> None:
    store, _ = swarm
    with pytest.raises(RuntimeError), master_lock.held(store, "sw", "tick master"):
        raise RuntimeError("launch failed")
    assert held_by(store) is None


@pytest.mark.feature("master-launch-lock")
def test_a_lock_that_changed_hands_is_never_released_by_its_old_holder(swarm: tuple[Any, ResumingRuntime]) -> None:
    store, _ = swarm
    with master_lock.held(store, "sw", "tick master"):
        store.redis.set(master_lock.key(store, "sw"), "master up 1")
    assert held_by(store) == "master up 1"


@pytest.mark.feature("master-launch-lock")
def test_a_lock_taken_over_during_its_release_is_left_to_its_new_holder(
    swarm: tuple[Any, ResumingRuntime], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = swarm
    pipeline = store.redis.pipeline

    def racing() -> Any:
        """A pipeline whose transaction starts only after another starter took the lock over."""
        pipe = pipeline()
        multi = pipe.multi

        def taken() -> None:
            """Take the lock over, then start the transaction."""
            store.redis.set(master_lock.key(store, "sw"), "master up 2")
            multi()

        monkeypatch.setattr(pipe, "multi", taken)
        return pipe

    with master_lock.held(store, "sw", "tick master"):
        monkeypatch.setattr(store.redis, "pipeline", racing)
    assert held_by(store) == "master up 2"


@pytest.mark.feature("master-launch-lock")
def test_a_refusal_whose_holder_just_expired_says_so(
    swarm: tuple[Any, ResumingRuntime], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = swarm
    monkeypatch.setattr(store.redis, "set", lambda *args, **kwargs: None)
    with master_lock.held(store, "sw", "master up") as refused:
        assert refused == (
            "the master launch lock of swarm sw is held by a starter whose lease just ended, so no second master starts"
        )


@pytest.mark.feature("master-launch-lock")
def test_a_held_lock_refuses_the_tick_master_step(swarm: tuple[Any, ResumingRuntime]) -> None:
    store, rt = swarm
    with master_lock.held(store, "sw", "master up"):
        said = masters._master("sw", store.config("sw"), store, rt, NOW)
    assert said == ["the master launch lock of swarm sw is held by master up, so no second master starts"]
    assert rt.masters == []
    assert store.agents("sw") == []


@pytest.mark.feature("master-launch-lock")
@pytest.mark.parametrize("choice", [master_launch.NEW, master_launch.REPLACE])
def test_a_held_lock_refuses_master_up(swarm: tuple[Any, ResumingRuntime], choice: str) -> None:
    store, rt = swarm
    with master_lock.held(store, "sw", "tick master"), pytest.raises(SwarmError) as refused:
        master_launch.up(store, "sw", rt, NOW, choice, input, print)
    assert str(refused.value) == "the master launch lock of swarm sw is held by tick master, so no second master starts"
    assert rt.masters == []
    assert store.agents("sw") == []


@pytest.mark.feature("master-launch-lock")
def test_the_tick_master_step_and_master_up_release_the_lock_after_their_launch(
    swarm: tuple[Any, ResumingRuntime],
) -> None:
    store, rt = swarm
    masters._master("sw", store.config("sw"), store, rt, NOW)
    assert held_by(store) is None
    rt.fail = True
    with pytest.raises(SwarmError):
        master_launch.up(store, "sw", rt, NOW, master_launch.REPLACE, input, print)
    assert held_by(store) is None
