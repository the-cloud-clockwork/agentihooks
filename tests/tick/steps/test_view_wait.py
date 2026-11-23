"""The view wait step waits for the pass view build on its own thread, so the relay steps start without it."""

from collections.abc import Callable, Iterator
from concurrent.futures import Future
from typing import Any

import pytest

from scripts.swarm import cli, delivery
from scripts.tick import registry
from scripts.tick.steps import RAISED, CallStep, actions_of, host, pass_domains, view_wait
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit
WAIT_S = 5.0


@pytest.fixture(autouse=True)
def _no_last_views(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test its own record of the last view each swarm completed."""
    monkeypatch.setattr(host, "_LAST_VIEWS", {})
    monkeypatch.setattr(host, "_VIEW_ORDER", {})
    yield


def _reader(values: dict[str, Any]) -> Callable[[str], Any]:
    """Return a reader of the pass state ``values`` holds."""
    return values.__getitem__


@pytest.mark.feature("tick-view-wait-off-scheduler")
def test_view_wait() -> None:
    """The view wait step is a kept tick lane step at the relay site reading the intent verdict and writing the view."""
    found = {step.name: step for step in registry.steps()}["view wait"]
    assert isinstance(found, CallStep)
    assert (found.group, found.lane, found.feature, found.effects, found.kept) == (
        "wrapper relay",
        "tick",
        "tick-view-wait-off-scheduler",
        frozenset(),
        True,
    )
    assert (found.reads, found.writes) == (pass_domains("judged"), pass_domains("view") | {actions_of("view wait")})


@pytest.mark.feature("tick-view-wait-off-scheduler")
def test_the_view_wait_step_stores_the_view_pass_view_waited_for(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Any]] = []
    fitted = object()

    def fit(slug: str, view: Any) -> Any:
        seen.append((slug, view))
        return fitted

    monkeypatch.setattr(host, "pass_view", fit)
    building: Future[Any] = Future()
    box: dict[str, Any] = {}
    judging = {"judged": (building, ["checked"])}
    view_wait._view_wait(cli, _reader({host.JUDGING: judging, "box": box, "slug": "sw"}))
    assert seen == [("sw", building)]
    assert box == {"view": fitted}


@pytest.mark.feature("tick-view-wait-off-scheduler")
def test_the_view_wait_step_stores_no_view_when_the_intent_check_stored_none(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(host, "pass_view", lambda slug, view: seen.append(view))
    box: dict[str, Any] = {}
    view_wait._view_wait(cli, _reader({host.JUDGING: {}, "box": box, "slug": "sw"}))
    assert (seen, box) == ([], {})


@pytest.mark.feature("tick-view-wait-off-scheduler")
def test_a_view_build_that_raised_is_held_in_the_pass_box_for_the_pass_end(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = TimeoutError("ledger read timed out")

    def fail(slug: str, view: Any) -> Any:
        raise failure

    monkeypatch.setattr(host, "pass_view", fail)
    found = {step.name: step for step in registry.steps()}["view wait"]
    assert isinstance(found, CallStep)
    box: dict[str, Any] = {}
    judging: dict[str, Any] = {"judged": (Future[Any](), [])}
    assert found.call(cli, _reader({host.JUDGING: judging, "box": box, "slug": "sw"})) == []
    assert box == {RAISED: failure}
    assert RAISED not in judging


@pytest.mark.feature("tick-view-wait-off-scheduler")
def test_outbox_migration_starts_before_a_slow_view_build_ends(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm, ledger, runtime = env
    run("sw", "create", "--repo", "/repo")
    build: Future[Any] = Future()
    order: list[str] = []
    waited = host.pass_view

    def timed(slug: str, view: Any) -> Any:
        found = waited(slug, view)
        order.append("view waited")
        return found

    def migrate(*args: Any) -> None:
        order.append("outbox migration")
        if not build.done():
            build.set_result(None)

    monkeypatch.setattr(cli, "_intent_pass", lambda *args: (build, []))
    monkeypatch.setattr(host, "_view_wait_s", lambda: WAIT_S)
    monkeypatch.setattr(host, "pass_view", timed)
    monkeypatch.setattr(delivery, "migrate_outbox", migrate)
    cli.run_tick(swarm, "sw", ledger, runtime, FakeHerdr({}))
    assert order[:2] == ["outbox migration", "view waited"]
