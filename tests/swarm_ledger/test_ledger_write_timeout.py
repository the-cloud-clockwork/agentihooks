"""A hosted ledger write waits as long as the server may hold it on the ledger writer lock."""

import pytest

from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.repository import postgres

ADD = {"op": "add", "id": "a1", "by": "eng", "item": "chat", "text": "hello"}


class Answering:
    def snapshot(self, slug: str) -> dict:
        return {"read": slug}

    def mutate(self, slug: str, ops: list) -> dict:
        return {"wrote": slug, "ops": ops}


@pytest.fixture
def timeouts(monkeypatch: pytest.MonkeyPatch) -> list:
    seen = []

    def client(slug: str, service: bool = False, timeout: float = -1.0, headers: dict | None = None) -> Answering:
        seen.append((slug, service, timeout))
        return Answering()

    monkeypatch.setattr(ledger, "client", client)
    return seen


@pytest.mark.feature("ledger-write-timeout")
def test_a_write_waits_past_the_server_writer_lock_budget(timeouts: list) -> None:
    assert ledger.request("ledger", [ADD], service=True) == {"wrote": "ledger", "ops": [ADD]}
    assert timeouts == [("ledger", True, 40)]
    assert ledger.WRITE_TIMEOUT == postgres.LOCK_TRIES * postgres.LOCK_WAIT + ledger.REQUEST_TIMEOUT


@pytest.mark.feature("ledger-write-timeout")
def test_an_empty_operation_list_is_still_a_write(timeouts: list) -> None:
    assert ledger.request("ledger", []) == {"wrote": "ledger", "ops": []}
    assert timeouts == [("ledger", False, 40)]


@pytest.mark.feature("ledger-write-timeout")
def test_a_read_keeps_the_request_timeout(timeouts: list) -> None:
    assert ledger.request("ledger") == {"read": "ledger"}
    assert timeouts == [("ledger", False, 10)]


@pytest.mark.feature("ledger-write-timeout")
def test_an_explicit_timeout_wins_for_a_write(timeouts: list) -> None:
    assert ledger.request("ledger", [ADD], timeout=2) == {"wrote": "ledger", "ops": [ADD]}
    assert timeouts == [("ledger", False, 2)]
