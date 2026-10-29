import threading
import time
from contextlib import contextmanager

import pytest

from scripts.swarm_ledger import ledger, ports
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger.api.errors import APIError

SLUG = "hive-work"
WORKER = "engineer@323133-0256"
HIVE_WORK = [
    ("agent_member", lambda: authority.hive_principal(SLUG, "hive.m.t", WORKER)),
    ("issue_agent", lambda: authority.hive_agent_token("m", SLUG, WORKER)),
]


@contextmanager
def every_permit_held():
    from scripts.swarm_ledger import hive_work

    held = [hive_work.SLOTS.acquire(timeout=1) for _ in range(hive_work.PERMITS)]
    try:
        assert held == [True] * hive_work.PERMITS
        yield
    finally:
        for _ in range(sum(held)):
            hive_work.SLOTS.release()


def refusal(call):
    try:
        call()
    except APIError as exc:
        return exc
    return None


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(ports.swarm().store, "redis_client", lambda: None)


@pytest.mark.parametrize(("work", "call"), HIVE_WORK)
def test_hive_credential_work_runs_at_most_four_requests_at_once(monkeypatch, no_redis, work, call):
    guard, release = threading.Lock(), threading.Event()
    running, peak, results = [0], [0], []

    def blocked(*given):
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(5)
        with guard:
            running[0] -= 1

    monkeypatch.setattr(ports.hive().auth, work, blocked)
    threads = [threading.Thread(target=lambda: results.append(refusal(call))) for _ in range(8)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while running[0] < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    release.set()
    for thread in threads:
        thread.join(10)
    assert peak[0] == 4
    assert results == [None] * 8


@pytest.mark.parametrize(("work", "call"), HIVE_WORK)
def test_hive_credential_work_that_cannot_start_in_time_is_refused_busy(monkeypatch, no_redis, work, call):
    from scripts.swarm_ledger import hive_work

    ran = []
    monkeypatch.setattr(ports.hive().auth, work, lambda *given: ran.append(given))
    monkeypatch.setattr(hive_work, "WAIT_S", 0.05)
    refused = []
    with every_permit_held():
        thread = threading.Thread(target=lambda: refused.append(refusal(call)), daemon=True)
        thread.start()
        thread.join(5)
        assert refused != []
    assert ran == []
    assert refused[0].status == 503
    assert refused[0].envelope() == {
        "error": {"code": "busy", "message": "The ledger is busy with other hive credential requests"}
    }


def test_the_busy_wait_is_half_the_client_request_timeout():
    from scripts.swarm_ledger import hive_work

    assert hive_work.WAIT_S == ledger.REQUEST_TIMEOUT / 2
