import json
import threading
import time
import urllib.error
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from scripts.ci_lab import budget, lease_api, metrics, scheduler
from scripts.ci_lab.capacity import Load, document
from tests.ci_lab.test_scheduler import HEAD_A, Run, _built, _read, plan, unit
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


def _document(run: Run, load: Callable[[int], Load], now: int) -> object:
    spending = budget.Budget(run.lab.client, lambda: now, POLICY)
    return json.loads(json.dumps(document(budget.observe(load(now), spending.status()))))


def _named(served: set[str], fresh: set[str]) -> set[str]:
    names = {line.split("{")[0].split(" ")[0] for line in fresh}
    return {line for line in served if line.split("{")[0].split(" ")[0] in names}


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
    with _listening(run, now) as (api, exposition):
        _built(api)
        yield api, exposition


@contextmanager
def _listening(run: Run, now: list[int]) -> Iterator[tuple[str, str]]:
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
    fresh = _fresh(run, run.engine.load, 1000)
    assert _named(served, fresh) == fresh


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
    fresh = _fresh(run, load, later)
    assert _named(_samples(text), fresh) == fresh


@pytest.mark.feature("lab-scheduler-capacity-cached")
def test_two_capacity_reads_in_a_row_build_the_load_once(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _started(lab)
    builds, _ = _counted(run, monkeypatch)
    with _serving(run, [1000]) as (api, _):
        first = json.loads(_read(f"{api}/capacity"))
        second = json.loads(_read(f"{api}/capacity"))
    assert builds == [1000]
    assert first == second


@pytest.mark.feature("lab-scheduler-capacity-cached")
def test_a_capacity_read_and_a_scrape_share_one_load_build(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _started(lab)
    builds, _ = _counted(run, monkeypatch)
    with _serving(run, [1000]) as (api, exposition):
        capacity = json.loads(_read(f"{api}/capacity"))
        assert len(json.loads(_read(f"{api}/lease", LEASE))["leases"]) == 1
        served = _samples(_read(exposition).decode())
    assert builds == [1000]
    assert capacity["classes"]["promotion"]["running"] == 0
    assert f"{RUNNING} 0.0" in served


@pytest.mark.feature("lab-scheduler-capacity-cached")
def test_the_capacity_document_equals_a_fresh_build_of_the_same_state(lab: Callable[..., Run]) -> None:
    run = _started(lab)
    assert len(run.lease()) == 1
    with _serving(run, [1000]) as (api, _):
        capacity = json.loads(_read(f"{api}/capacity"))
    assert capacity["classes"]["promotion"]["running"] == 1
    assert capacity == _document(run, run.engine.load, 1000)


@pytest.mark.feature("lab-scheduler-capacity-cached")
def test_the_capacity_route_serves_the_build_the_metrics_port_refreshed(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _started(lab)
    builds, load = _counted(run, monkeypatch)
    later = 1000 + scheduler.REFRESH_MS
    now = [1000]
    with _serving(run, now) as (api, exposition):
        assert json.loads(_read(f"{api}/capacity"))["classes"]["promotion"]["running"] == 0
        assert len(json.loads(_read(f"{api}/lease", LEASE))["leases"]) == 1
        now[0] = later
        assert AGE_ZERO in _poll(exposition, AGE_ZERO)
        capacity = json.loads(_read(f"{api}/capacity"))
    assert builds == [1000, later]
    assert capacity["classes"]["promotion"]["running"] == 1
    assert capacity == _document(run, load, later)


@pytest.mark.feature("lab-scheduler-capacity-cached")
def test_the_grades_route_reads_every_request_beside_the_cached_capacity(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _started(lab)
    builds, _ = _counted(run, monkeypatch)
    graded = run.grade(HEAD_A).run
    with _serving(run, [1000]) as (api, _):
        assert json.loads(_read(f"{api}/capacity"))["classes"]["promotion"]["pending"] == 2
        first = json.loads(_read(f"{api}/grades/{HEAD_A}"))["grades"]
        run.lab.client.delete(scheduler.GRADES + graded)
        second = json.loads(_read(f"{api}/grades/{HEAD_A}"))["grades"]
    assert [grade["run"] for grade in first] == [graded]
    assert second == []
    assert builds == [1000]


def _refused(url: str) -> tuple[int, object]:
    with pytest.raises(urllib.error.HTTPError) as refused:
        _read(url)
    return refused.value.code, json.loads(refused.value.read())


@pytest.mark.feature("lab-scheduler-first-build-at-bind")
def test_listen_starts_the_first_build_at_bind_and_no_read_waits_for_it(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _started(lab)
    assert len(run.lease()) == 1
    builds, load = _counted(run, monkeypatch)
    counted = run.engine.load
    entered, gate = threading.Event(), threading.Event()

    def held(now_ms: int) -> Load:
        entered.set()
        assert gate.wait(10)
        return counted(now_ms)

    monkeypatch.setattr(run.engine, "load", held)
    unbuilt = (503, {"error": "the load snapshot is not built yet"})
    with _listening(run, [1000]) as (api, exposition):
        try:
            assert entered.wait(10)
            assert _refused(f"{api}/capacity") == unbuilt
            assert _refused(exposition.removesuffix("/metrics") + "/capacity") == unbuilt
            scraped = _samples(_read(exposition).decode())
        finally:
            gate.set()
        capacity = json.loads(_built(api))
        served = _samples(_read(exposition).decode())
    fresh = _fresh(run, load, 1000)
    assert _named(scraped, fresh) == set()
    assert not any(line.startswith("ci_lab_load_age_seconds") for line in scraped)
    assert _named(served, fresh) == fresh
    assert f"{RUNNING} 1.0" in served
    assert capacity == _document(run, load, 1000)
    assert builds == [1000]
