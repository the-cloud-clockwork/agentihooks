import os
import threading
from unittest.mock import patch

import pytest

from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger.repository import bin_storage
from tests.swarm_ledger.test_ledger_authority import SLUG
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
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": mode}),
        patch.object(ledger, "credentials", return_value={"X-Ledger-Token": live["admin"]}),
        patch.object(bin_storage, "bin_closed", server_bin),
    ):
        client = LedgerClient()
        assert client.bin_closed(SLUG, 1) is True
        assert client.bin_closed(SLUG, 1) is False
