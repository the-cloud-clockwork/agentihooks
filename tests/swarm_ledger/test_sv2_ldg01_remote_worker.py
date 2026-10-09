import io
import os
import urllib.error
from unittest.mock import patch

import pytest

from scripts.swarm_ledger import ledger
from tests import sv2_ldg01_cases as cases
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.xdist_group("fakeredis")

live = _authority_live
REMOTE = {
    "AGENTIHOOKS_DEPLOYMENT": "distributed",
    "AGENTIHOOKS_SWARM": "rig-grade-swarm",
    "AGENTIHOOKS_AGENT_NAME": WORKER,
    "LEDGER_URL": "https://hub.example",
    "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "launch-token",
}


def test_worker_with_a_read_only_home_reads_its_task_and_posts_an_update(live, tmp_path):
    first = cases.positive(live, tmp_path / "first", "first")
    second = cases.positive(live, tmp_path / "second", "second")
    assert first == second
    assert first[cases.METRIC] == 0


def test_a_missing_remote_token_is_unauthenticated_and_never_uses_the_local_credential(live, tmp_path):
    first = cases.negative(live, tmp_path / "first", "first")
    second = cases.negative(live, tmp_path / "second", "second")
    assert first == second
    assert first[cases.METRIC] == 1


def test_a_transient_outage_retries_reads_without_starting_a_server(live):
    first = cases.recovery(live, "first")
    second = cases.recovery(live, "second")
    assert first == second
    assert (first["read_attempts"], first["servers_started"], first["replayed_effects"]) == (3, 0, 1)
    assert set(cases.manifest()) == {"remote-worker-home.json"}


def test_a_remote_read_gives_up_after_its_bounded_attempts():
    sleeps = []
    with (
        patch.dict(os.environ, REMOTE),
        patch.object(ledger, "request", side_effect=ConnectionRefusedError("down")) as request,
        patch.object(ledger.time, "sleep", sleeps.append),
        patch.object(ledger.repository, "exists", side_effect=AssertionError("local repository read")),
        patch.object(ledger.subprocess, "run") as run,
        pytest.raises(SystemExit, match="^ledger server not answering on https://hub.example: down$"),
    ):
        ledger.call(SLUG)
    assert request.call_count == ledger.REMOTE_READ_ATTEMPTS == 3
    assert sleeps == [ledger.REMOTE_READ_PAUSE, 2 * ledger.REMOTE_READ_PAUSE]
    run.assert_not_called()


def test_a_remote_write_runs_once_through_an_outage():
    with (
        patch.dict(os.environ, REMOTE),
        patch.object(ledger, "request", side_effect=ConnectionRefusedError("down")) as request,
        patch.object(ledger.time, "sleep") as sleep,
        patch.object(ledger.repository, "exists", side_effect=AssertionError("local repository read")),
        pytest.raises(SystemExit, match="not answering"),
    ):
        ledger.call(SLUG, [{"op": "ack"}])
    request.assert_called_once_with(SLUG, [{"op": "ack"}], False)
    sleep.assert_not_called()


def test_a_remote_refusal_is_not_retried_and_counts_a_refused_credential():
    body = b'{"error": {"code": "forbidden", "message": "Missing or wrong ledger credential"}}'
    refusal = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(body))
    before = ledger.ledger_remote_auth_failures_total()
    with (
        patch.dict(os.environ, REMOTE),
        patch.object(ledger, "request", side_effect=refusal) as request,
        pytest.raises(SystemExit, match=f"^server refused: 403 {body.decode()}$"),
    ):
        ledger.call(SLUG)
    assert request.call_count == 1
    assert ledger.ledger_remote_auth_failures_total() == before + 1


def test_a_remote_permission_refusal_is_not_an_auth_failure():
    body = b'{"error": {"code": "forbidden", "message": "Checkbox changes need the operator"}}'
    refusal = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(body))
    before = ledger.ledger_remote_auth_failures_total()
    with patch.dict(os.environ, REMOTE), patch.object(ledger, "request", side_effect=refusal):
        with pytest.raises(SystemExit, match="^server refused: 403"):
            ledger.call(SLUG, [{"op": "ack"}])
    assert ledger.ledger_remote_auth_failures_total() == before


def test_remote_resource_reads_retry_and_local_reads_run_once():
    replies = [ConnectionResetError("reset"), {"data": {"title": "T"}}]

    class Client:
        def __init__(self, base, credentials, timeout=10):
            pass

        def request(self, slug, path, payload=None):
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    with (
        patch.dict(os.environ, REMOTE),
        patch("scripts.swarm_ledger.api.client.ResourceClient", Client),
        patch.object(ledger.time, "sleep") as sleep,
    ):
        assert ledger.resource(SLUG, "metadata") == {"title": "T"}
    sleep.assert_called_once_with(ledger.REMOTE_READ_PAUSE)
    replies[:] = [ConnectionResetError("reset"), {"data": {}}]
    with patch("scripts.swarm_ledger.api.client.ResourceClient", Client), patch.dict(os.environ, {}):
        os.environ.pop("AGENTIHOOKS_DEPLOYMENT", None)
        with patch.object(ledger, "credentials", return_value={}), pytest.raises(ConnectionResetError):
            ledger.resource(SLUG, "metadata")


def test_unauthenticated_refusals_count_each_reason():
    before = ledger.ledger_remote_auth_failures_total()
    with patch.dict(os.environ, REMOTE):
        os.environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN")
        os.environ.pop("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", None)
        with pytest.raises(ledger.Unauthenticated, match="^unauthenticated: a remote ledger client needs"):
            ledger.credentials(SLUG)
        os.environ.pop("AGENTIHOOKS_AGENT_NAME")
        with pytest.raises(ledger.Unauthenticated, match="^unauthenticated: a remote ledger client needs a pinned"):
            ledger.credentials(SLUG)
    assert ledger.ledger_remote_auth_failures_total() == before + 2
