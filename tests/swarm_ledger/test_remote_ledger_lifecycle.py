import io
import json
import os
import threading
import urllib.error
from unittest.mock import patch

import pytest

from scripts.swarm import lease
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger.repository import bin_storage
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER, core, send, server
from tests.swarm_ledger.test_ledger_authority import cli_ledger as ledger
from tests.swarm_ledger.test_ledger_authority import live as authority_live
from tests.swarm_ledger.test_remote_ledger_client import hive as remote_hive

pytestmark = pytest.mark.xdist_group("fakeredis")
hive = remote_hive
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


def test_remote_lifecycle_refusals_use_swarm_errors(live):
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": "compose"}),
        patch.object(ledger, "credentials", return_value={"X-Ledger-Token": "wrong-token"}),
        pytest.raises(LedgerRefused, match="403"),
    ):
        LedgerClient().bin_closed(SLUG, 1)


def test_displaced_controller_cannot_bin_a_ledger(live):
    bin_storage.restore(SLUG)
    token = lease.EPOCH.set(7)
    try:
        with (
            patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": "compose"}),
            patch.object(ledger, "credentials", return_value={"X-Ledger-Token": live["admin"]}),
            patch.object(server.authority, "fence", side_effect=SwarmError("stale controller")),
            pytest.raises(LedgerRefused, match="stale controller"),
        ):
            LedgerClient().bin_closed(SLUG, core.now_ms() + 1)
    finally:
        lease.EPOCH.reset(token)
    assert SLUG not in bin_storage.entries()


@pytest.mark.parametrize("mode", ["compose", "distributed"])
def test_remote_agent_can_reopen_but_cannot_bin(live, hive, mode):
    core.sync(SLUG, ops=[{"op": "join", "id": f"join-remote-{mode}", "by": WORKER}])
    bin_storage.delete(SLUG)
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": mode}),
        patch.object(
            ledger,
            "credentials",
            return_value={
                "X-Ledger-Token": server.authority.hive_agent_token("member-1", SLUG, WORKER),
                "X-Ledger-Agent": WORKER,
            },
        ),
    ):
        client = LedgerClient()
        client.reopen(SLUG, WORKER)
        assert client.binned(SLUG) is False
        with pytest.raises(LedgerRefused, match="403"):
            client.bin_closed(SLUG, core.now_ms() + 1)


@pytest.mark.parametrize(
    "failure", [OSError("offline"), urllib.error.HTTPError("", 500, "outage", {}, io.BytesIO(b"outage"))]
)
def test_remote_lifecycle_outages_use_swarm_errors(live, failure):
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": "compose"}),
        patch.object(ledger, "credentials", return_value={"X-Ledger-Token": live["admin"]}),
        patch("urllib.request.urlopen", side_effect=failure),
        pytest.raises(SwarmError),
    ):
        LedgerClient().bin_closed(SLUG, 1)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"action": "delete"},
        {"action": "bin_closed"},
        {"action": "bin_closed", "closed_at": 0},
        {"action": "bin_closed", "closed_at": True},
        {"action": "restore", "closed_at": 1},
        {"action": "binned", "controller_epoch": "7"},
    ],
)
def test_lifecycle_schema_refusals_leave_storage_unchanged(live, payload):
    before = bin_storage.entries()
    status, _, _ = send(
        live,
        "POST",
        f"/api/v1/ledgers/{SLUG}/lifecycle/actions",
        json.dumps(payload).encode(),
        **{"X-Ledger-Token": live["admin"]},
    )
    assert status == 400
    assert bin_storage.entries() == before
