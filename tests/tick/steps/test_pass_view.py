"""The pass view wait fits inside the critical lane: a late build yields the swarm's last view and becomes the next."""

from collections.abc import Iterator
from concurrent.futures import Future
from typing import Any

import pytest

from scripts.tick import pool
from scripts.tick.steps import host

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_last_views(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test its own record of the last view each swarm completed."""
    monkeypatch.setattr(host, "_LAST_VIEWS", {})
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
        assert host.pass_view("sw", building) is earlier
    assert host._LAST_VIEWS == {"sw": earlier}
    building.set_result(late)
    assert host._LAST_VIEWS == {"sw": late}


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_the_wait_is_bounded_by_the_slow_lane_when_it_ends_first() -> None:
    earlier = object()
    host.pass_view("sw", _built(earlier))
    with pool.running(critical_s=60.0, slow_s=host.VIEW_RESERVE_S - 1.0):
        assert host.pass_view("sw", Future()) is earlier


@pytest.mark.feature("swarm-tick-view-lane-fit")
def test_a_failed_late_build_keeps_the_last_view() -> None:
    earlier = object()
    host.pass_view("sw", _built(earlier))
    building: Future[Any] = Future()
    with pool.running(critical_s=0.0, slow_s=0.0):
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
