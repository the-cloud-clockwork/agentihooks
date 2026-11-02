from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger import ledger_core
from scripts.swarm_ledger.api import admin
from scripts.swarm_ledger.api.errors import APIError


@pytest.fixture
def bin_services():
    return SimpleNamespace(
        ledger=SimpleNamespace(core=ledger_core, bin_ledger=Mock()),
        repository=SimpleNamespace(restore=Mock(return_value=True)),
        swarm=SimpleNamespace(status=Mock(return_value=None)),
        policy=SimpleNamespace(allowed_origins={"http://localhost"}),
    )


@pytest.fixture
def bin_handler():
    return SimpleNamespace(headers={"Origin": "http://localhost"}, exists=Mock(return_value=True))


@pytest.mark.parametrize("action", ["delete", "restore"])
@pytest.mark.parametrize("slug", ["ledger\n", "ledger\nextra", "!ledger", "ledger!", "", "a" * 122])
def test_bin_schema_refuses_invalid_names_before_lookup(bin_handler, bin_services, action, slug):
    with pytest.raises(APIError) as caught:
        admin.bin_action(bin_handler, bin_services, {"action": action, "slug": slug})

    assert caught.value.status == 400
    assert caught.value.envelope() == {
        "error": {"code": "schema_invalid", "message": "Request does not match the resource schema"}
    }
    bin_handler.exists.assert_not_called()
    bin_services.ledger.bin_ledger.assert_not_called()
    bin_services.repository.restore.assert_not_called()
    bin_services.swarm.status.assert_not_called()


@pytest.mark.parametrize("action", ["delete", "restore"])
@pytest.mark.parametrize("slug", ["ledger", "0", "ledger-1.name_2", "a" * 121])
def test_bin_schema_accepts_valid_names(bin_handler, bin_services, action, slug):
    assert admin.bin_action(bin_handler, bin_services, {"action": action, "slug": slug}) == {
        "slug": slug,
        "action": action,
    }
    bin_handler.exists.assert_called_once_with(slug)
    if action == "delete":
        bin_services.ledger.bin_ledger.assert_called_once_with(slug)
        bin_services.repository.restore.assert_not_called()
    else:
        bin_services.repository.restore.assert_called_once_with(slug)
        bin_services.ledger.bin_ledger.assert_not_called()
