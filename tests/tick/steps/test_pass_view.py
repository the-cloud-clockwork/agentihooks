"""The pass view wait fits inside the critical lane: a late build yields the swarm's last view and becomes the next."""

from collections.abc import Iterator
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.tick import pool
from scripts.tick.steps import host

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_last_views(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test its own record of the last view each swarm completed."""
    monkeypatch.setattr(host, "_LAST_VIEWS", {})
    monkeypatch.setattr(host, "_VIEW_ORDER", {})
    yield


def _built(value: Any) -> Future[Any]:
    """Return a view build that already ended with ``value``."""
    done: Future[Any] = Future()
    done.set_result(value)
    return done


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_a_built_view_is_returned_and_kept_for_the_next_pass() -> None:
    view = object()
    assert host.pass_view("sw", _built(view)) is view
    assert host._LAST_VIEWS == {"sw": view}


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_a_view_that_is_no_future_is_returned_as_is() -> None:
    view = object()
    assert host.pass_view("sw", view) is view
    assert host.pass_view("sw", None) is None
    assert host._LAST_VIEWS == {}


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_a_late_build_yields_the_last_view_and_becomes_the_next_fallback() -> None:
    earlier, late = object(), object()
    host.pass_view("sw", _built(earlier))
    building: Future[Any] = Future()
    with pool.running(critical_s=host.VIEW_RESERVE_S + 0.05, slow_s=60.0):
        bound = host._view_wait_s()
        assert bound is not None and bound <= 0.05
        assert host.pass_view("sw", building) is earlier
    assert host._LAST_VIEWS == {"sw": earlier}
    building.set_result(late)
    assert host._LAST_VIEWS == {"sw": late}


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_the_wait_is_bounded_by_the_slow_lane_when_it_ends_first() -> None:
    earlier = object()
    host.pass_view("sw", _built(earlier))
    with pool.running(critical_s=60.0, slow_s=host.VIEW_RESERVE_S - 1.0):
        assert host._view_wait_s() == 0.0
        assert host.pass_view("sw", Future()) is earlier


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_a_failed_late_build_keeps_the_last_view() -> None:
    earlier = object()
    host.pass_view("sw", _built(earlier))
    building: Future[Any] = Future()
    with pool.running(critical_s=0.0, slow_s=0.0):
        assert host._view_wait_s() == 0.0
        assert host.pass_view("sw", building) is earlier
    building.set_exception(RuntimeError("github down"))
    assert host._LAST_VIEWS == {"sw": earlier}


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_a_swarm_with_no_earlier_view_waits_for_its_build() -> None:
    view = object()
    building: Future[Any] = Future()
    with pool.running(critical_s=0.0, slow_s=0.0):
        building.set_result(view)
        assert host.pass_view("other", building) is view


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_views_are_kept_per_swarm() -> None:
    first, second = object(), object()
    host.pass_view("a", _built(first))
    with pool.running(critical_s=0.0, slow_s=0.0):
        assert host.pass_view("b", _built(second)) is second
        assert host.pass_view("a", Future()) is first


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_an_older_build_ending_last_never_replaces_a_newer_view() -> None:
    host.pass_view("sw", _built(object()))
    older: Future[Any] = Future()
    newer: Future[Any] = Future()
    old_view, new_view = object(), object()
    with pool.running(critical_s=0.0, slow_s=0.0):
        host.pass_view("sw", older)
        host.pass_view("sw", newer)
    newer.set_result(new_view)
    older.set_result(old_view)
    assert host._LAST_VIEWS == {"sw": new_view}


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_a_build_that_raises_its_own_timeout_in_time_raises_it() -> None:
    host.pass_view("sw", _built(object()))
    failed: Future[Any] = Future()
    failed.set_exception(TimeoutError("ledger read timed out"))
    with pool.running(critical_s=60.0, slow_s=60.0), pytest.raises(TimeoutError, match="ledger read timed out"):
        host.pass_view("sw", failed)


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_the_wait_has_no_bound_outside_a_pass() -> None:
    assert host._view_wait_s() is None


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_the_relay_hook_takes_the_view_through_pass_view(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Any]] = []
    fitted = object()

    def fit(slug: str, view: Any) -> Any:
        seen.append((slug, view))
        return fitted

    monkeypatch.setattr(host, "pass_view", fit)
    building: Future[Any] = Future()
    wrapper = SimpleNamespace(
        ticks=SimpleNamespace(close=lambda: None),
        judging={"judged": (building, ["checked"])},
        box={},
        slug="sw",
        checked=[],
    )
    assert cli._Wrapper._judged(wrapper) is True  # pyright: ignore[reportArgumentType]
    assert seen == [("sw", building)]
    assert wrapper.box == {"view": fitted}
    assert wrapper.checked == ["checked"]
