import importlib
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger.api import client
from scripts.swarm_v2 import hosted_ledger

pytestmark = pytest.mark.xdist_group("hosted_client_lookup")


@pytest.fixture(scope="module")
def imported_hosted():
    original = dict(vars(hosted_ledger))
    yield hosted_ledger
    vars(hosted_ledger).update(original)


def test_01_import_under_a_patched_client(monkeypatch, imported_hosted):
    factory = Mock()
    factory.return_value.request.return_value = {"data": {"id": "first"}, "revision": "1"}
    monkeypatch.setattr(client, "ResourceClient", factory)
    importlib.reload(imported_hosted)

    hosted = imported_hosted.HostedLedger("https://ledger.example", "controller", None)

    assert hosted.read("example", "tasks/first") == {"tasks": [{"id": "first"}]}
    factory.assert_called_once_with("https://ledger.example", {"X-Controller-Credential": "controller"})
    factory.return_value.request.assert_called_once_with("example", "tasks/first")


def test_02_later_read_uses_the_current_client(monkeypatch, imported_hosted):
    factory = Mock()
    factory.return_value.request.return_value = {"data": {"id": "second"}, "revision": "2"}
    monkeypatch.setattr(client, "ResourceClient", factory)

    hosted = imported_hosted.HostedLedger("https://other.example", "other-controller", None)

    assert hosted.read("example", "tasks/second") == {"tasks": [{"id": "second"}]}
    factory.assert_called_once_with("https://other.example", {"X-Controller-Credential": "other-controller"})
    factory.return_value.request.assert_called_once_with("example", "tasks/second")
