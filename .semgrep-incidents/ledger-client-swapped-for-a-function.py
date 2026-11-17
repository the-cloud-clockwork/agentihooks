from unittest import mock
from unittest.mock import patch

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


def test_closure_function_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    def client() -> Ledger:
        return Ledger()

    def swap() -> None:
        # ruleid: ledger-client-swapped-for-a-function
        monkeypatch.setattr(ledger_client, "LedgerClient", client)

    swap()


class ClassBodySwap:
    def client() -> Ledger:
        return Ledger()

    # ruleid: ledger-client-swapped-for-a-function
    setattr(ledger_client, "LedgerClient", client)

    def __init__(self) -> None:
        self.lines: list[str] = []


def test_mock_patch_swap() -> None:
    # ruleid: ledger-client-swapped-for-a-function
    with patch("scripts.swarm.ledger_client.LedgerClient", lambda: Ledger()):
        pass
    # ruleid: ledger-client-swapped-for-a-function
    with mock.patch("scripts.swarm.ledger_client.LedgerClient", new=refuse):
        pass


def test_mock_patch_object_swap() -> None:
    # ruleid: ledger-client-swapped-for-a-function
    with patch.object(ledger_client, "LedgerClient", lambda: Ledger()):
        pass
    # ruleid: ledger-client-swapped-for-a-function
    with mock.patch.object(client_module, "LedgerClient", new=refuse):
        pass


# ruleid: ledger-client-swapped-for-a-function
@patch("scripts.swarm.ledger_client.LedgerClient", lambda: Ledger())
def test_decorated_patch_swap() -> None:
    pass


def test_assignment_swap() -> None:
    # ruleid: ledger-client-swapped-for-a-function
    ledger_client.LedgerClient = lambda: Ledger()


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


def test_mock_patch_class_swap() -> None:
    # ok: ledger-client-swapped-for-a-function
    with patch("scripts.swarm.ledger_client.LedgerClient", Ledger):
        pass
    # ok: ledger-client-swapped-for-a-function
    with patch.object(ledger_client, "LedgerClient", new=client_class(Ledger())):
        pass
    # ok: ledger-client-swapped-for-a-function
    with patch("scripts.swarm.cli.LedgerClient", lambda: Ledger()):
        pass


def built() -> Ledger:
    def made() -> Ledger:
        return Ledger()

    return made()


def test_sibling_scope_name_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    made = client_class(built())
    # ok: ledger-client-swapped-for-a-function
    monkeypatch.setattr(ledger_client, "LedgerClient", made)
