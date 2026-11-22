import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from scripts.ci_lab import budget, lease_api, metrics, scheduler
from scripts.ci_lab.capacity import Load
from tests.ci_lab.test_scheduler import HEAD_A, Run, _read, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab
POLICY = budget.Policy(100.0, {})
LEASE = json.dumps({"worker": "w", "free_slots": 1, "capabilities": [], "wait_ms": 0}).encode()
AGE_ZERO = "\nci_lab_load_age_seconds 0.0\n"
RUNNING = 'ci_lab_units_running{class="promotion"}'


def _samples(text: str) -> set[str]:
    return {line for line in text.splitlines() if line and not line.startswith("#")}


def _fresh(run: Run, load: Callable[[int], Load], now: int) -> set[str]:
    registry = metrics.Registry()
    spending = budget.Budget(run.lab.client, lambda: now, POLICY)
    metrics.observe_load(registry, budget.observe(load(now), spending.status()))
    return _samples(registry.render())


def _counted(run: Run, monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], Callable[[int], Load]]:
    builds: list[int] = []
    load = run.engine.load

    def counted(now_ms: int) -> Load:
        builds.append(now_ms)
        return load(now_ms)

    monkeypatch.setattr(run.engine, "load", counted)
    return builds, load


@contextmanager
def _serving(run: Run, now: list[int]) -> Iterator[tuple[str, str]]:
    spending = budget.Budget(run.lab.client, lambda: now[0], POLICY)
    routes = lease_api.ControlRoutes(run.lab.client, "tok", lambda: now[0])
    servers = scheduler.listen(run.engine, run.leases(), routes, spending, lambda: now[0], (0, 0, 0))
    loops = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for loop in loops:
        loop.start()
    api, _, exposition = (f"http://127.0.0.1:{server.server_address[1]}" for server in servers)
    try:
        yield api, f"{exposition}/metrics"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for loop in loops:
            loop.join(10)


def _poll(url: str, needle: str) -> str:
    deadline = time.monotonic() + 10
    text = _read(url).decode()
    while needle not in text and time.monotonic() < deadline:
        time.sleep(0.05)
        text = _read(url).decode()
    return text


def _started(make: Callable[..., Run]) -> Run:
    run = make({HEAD_A: plan({"lint": (unit(1), unit(2))})})
    run.push(HEAD_A)
    run.tick(1000)
    return run


@pytest.mark.feature("lab-scheduler-cached-snapshot")
def test_a_scrape_serves_the_series_of_a_fresh_load_build_of_the_same_state(lab: Callable[..., Run]) -> None:
    run = _started(lab)
    assert len(run.lease()) == 1
    with _serving(run, [1000]) as (_, exposition):
        served = _samples(_read(exposition).decode())
    assert f"{RUNNING} 1.0" in served
    assert _fresh(run, run.engine.load, 1000) <= served


@pytest.mark.feature("lab-scheduler-cached-snapshot")
def test_two_scrapes_in_a_row_build_the_load_once(lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch) -> None:
    run = _started(lab)
    builds, _ = _counted(run, monkeypatch)
    with _serving(run, [1000]) as (_, exposition):
        first = _read(exposition).decode()
        second = _read(exposition).decode()
    assert builds == [1000]
    assert _samples(first) == _samples(second)


@pytest.mark.feature("lab-scheduler-cached-snapshot")
def test_a_later_scrape_serves_the_cached_load_with_its_age_while_the_lab_moves(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _started(lab)
    builds, _ = _counted(run, monkeypatch)
    now = [1000]
    with _serving(run, now) as (api, exposition):
        assert f"{RUNNING} 0.0" in _read(exposition).decode()
        assert len(json.loads(_read(f"{api}/lease", LEASE))["leases"]) == 1
        now[0] = 3500
        text = _read(exposition).decode()
    assert f"{RUNNING} 0.0" in text
    assert "\nci_lab_load_age_seconds 2.5\n" in text
    assert builds == [1000]


@pytest.mark.feature("lab-scheduler-cached-snapshot")
def test_the_metrics_port_rebuilds_the_load_on_its_own_cadence_and_serves_the_fresh_build(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _started(lab)
    builds, load = _counted(run, monkeypatch)
    later = 1000 + scheduler.REFRESH_MS
    now = [1000]
    with _serving(run, now) as (api, exposition):
        assert f"{RUNNING} 0.0" in _read(exposition).decode()
        assert len(json.loads(_read(f"{api}/lease", LEASE))["leases"]) == 1
        now[0] = later
        text = _poll(exposition, AGE_ZERO)
    assert AGE_ZERO in text
    assert builds == [1000, later]
    assert f"{RUNNING} 1.0" in _samples(text)
    assert _fresh(run, load, later) <= _samples(text)
