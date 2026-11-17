import pytest

import scripts.swarm.ledger_client as client_module
from scripts.swarm import cli, ledger_client
from tests.swarm.client_class import client_class


class Ledger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def say(self, slug: str, text: str) -> None:
        self.lines.append(text)


def refuse() -> Ledger:
    raise AssertionError("no ledger")


def test_path_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = Ledger()
    # ruleid: ledger-client-swapped-for-a-function
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", lambda: ledger)


def test_module_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = Ledger()
    # ruleid: ledger-client-swapped-for-a-function
    monkeypatch.setattr(ledger_client, "LedgerClient", lambda **_: ledger)


def test_aliased_module_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    # ruleid: ledger-client-swapped-for-a-function
    monkeypatch.setattr(client_module, "LedgerClient", lambda *args, **kwargs: Ledger(), raising=True)


def test_local_import_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import ledger_client as imported

    # ruleid: ledger-client-swapped-for-a-function
    monkeypatch.setattr(imported, "LedgerClient", lambda: Ledger())


def test_builtin_setattr_swap() -> None:
    # ruleid: ledger-client-swapped-for-a-function
    setattr(ledger_client, "LedgerClient", lambda: Ledger())


def test_function_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    def client() -> Ledger:
        return Ledger()

    # ruleid: ledger-client-swapped-for-a-function
    monkeypatch.setattr(ledger_client, "LedgerClient", client)
    # ruleid: ledger-client-swapped-for-a-function
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", client)


def test_module_function_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    # ruleid: ledger-client-swapped-for-a-function
    monkeypatch.setattr(ledger_client, "LedgerClient", refuse)


def test_class_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    # ok: ledger-client-swapped-for-a-function
    monkeypatch.setattr(ledger_client, "LedgerClient", Ledger)
    # ok: ledger-client-swapped-for-a-function
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient", client_class(Ledger()))


def test_importer_binding_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    # ok: ledger-client-swapped-for-a-function
    monkeypatch.setattr(cli, "LedgerClient", lambda: Ledger())


def test_method_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    # ok: ledger-client-swapped-for-a-function
    monkeypatch.setattr(ledger_client.LedgerClient, "_call", lambda self, *args: {})
