import os
import threading
from unittest.mock import patch

import pytest

from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger.repository import bin_storage
from tests.swarm_ledger.test_ledger_authority import SLUG, core
from tests.swarm_ledger.test_ledger_authority import cli_ledger as ledger
from tests.swarm_ledger.test_ledger_authority import live as authority_live

pytestmark = pytest.mark.xdist_group("fakeredis")
live = authority_live


@pytest.mark.parametrize("mode", ["compose", "distributed"])
def test_remote_bin_uses_the_server_and_preserves_conditional_result(live, mode):
    original = bin_storage.bin_closed

    def server_bin(*args, **kwargs):
        assert threading.current_thread() is not threading.main_thread(), "client touched the ledger folder"
        return original(*args, **kwargs)

    bin_storage.restore(SLUG)
    closed_at = core.now_ms() + 1
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": mode}),
        patch.object(ledger, "credentials", return_value={"X-Ledger-Token": live["admin"]}),
        patch.object(bin_storage, "bin_closed", server_bin),
    ):
        client = LedgerClient()
        assert client.bin_closed(SLUG, closed_at) is True
        assert client.bin_closed(SLUG, closed_at) is False


@pytest.mark.parametrize("mode", ["compose", "distributed"])
def test_remote_reopen_restores_on_the_server(live, mode):
    original = bin_storage.restore

    def server_restore(*args, **kwargs):
        assert threading.current_thread() is not threading.main_thread(), "client touched the ledger folder"
        return original(*args, **kwargs)

    core.sync(SLUG, ops=[{"op": "close", "id": "close-for-remote", "by": "operator"}])
    bin_storage.delete(SLUG)
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": mode}),
        patch.object(ledger, "credentials", return_value={"X-Ledger-Token": live["admin"]}),
        patch.object(bin_storage, "restore", server_restore),
    ):
        client = LedgerClient()
        client.reopen(SLUG, "operator")
        assert client.closed(SLUG) is False
        assert client.binned(SLUG) is False
    assert SLUG not in bin_storage.entries()


@pytest.mark.parametrize("mode", ["compose", "distributed"])
@pytest.mark.parametrize("operation", ["restore", "bin_closed"])
def test_remote_local_storage_calls_name_the_server_route(mode, operation):
    import ledger_bin

    args = (SLUG,) if operation == "restore" else (SLUG, 1)
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": mode}),
        patch.object(bin_storage, "_transaction", side_effect=AssertionError("client opened storage")),
        pytest.raises(SystemExit, match=f"/api/v1/ledgers/{SLUG}/lifecycle/actions"),
    ):
        getattr(ledger_bin, operation)(*args)
