import io
import json
import os
import re
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts.swarm_ledger import ledger
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER

REMOTE = {
    "AGENTIHOOKS_DEPLOYMENT": "distributed",
    "AGENTIHOOKS_SWARM": "rig-grade-swarm",
    "AGENTIHOOKS_AGENT_NAME": WORKER,
    "LEDGER_URL": "https://hub.example",
    "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "launch-token",
}


@pytest.fixture
def remote():
    with patch.dict(os.environ, REMOTE), patch.object(ledger, "BASE", ""):
        yield


def refusal(code, message):
    body = json.dumps({"error": {"code": "forbidden", "message": message}}).encode()
    return urllib.error.HTTPError("u", code, "Refused", {}, io.BytesIO(body)), body.decode()


def test_a_remote_read_refused_with_a_client_error_is_not_retried(remote):
    error, body = refusal(404, "No such ledger")
    with (
        patch.object(ledger, "request", side_effect=error) as request,
        patch.object(ledger.time, "sleep") as sleep,
        pytest.raises(SystemExit, match=f"^server refused: 404 {re.escape(body)}$"),
    ):
        ledger.call(SLUG)
    assert request.call_count == 1
    sleep.assert_not_called()


def test_a_refusal_body_that_is_not_utf8_is_shown_with_replacement_characters(remote):
    error = urllib.error.HTTPError("u", 400, "Refused", {}, io.BytesIO(b"\xffbad"))
    with (
        patch.object(ledger, "request", side_effect=error),
        pytest.raises(SystemExit, match="^server refused: 400 �bad$"),
    ):
        ledger.call(SLUG)


def test_a_remote_permission_refusal_is_not_an_auth_failure(remote):
    error, body = refusal(403, "Checkbox changes need the operator")
    before = ledger.ledger_remote_auth_failures_total()
    with patch.object(ledger, "request", side_effect=error):
        with pytest.raises(SystemExit, match=f"^server refused: 403 {re.escape(body)}$") as refused:
            ledger.call(SLUG, [{"op": "ack"}])
    assert not isinstance(refused.value, ledger.Unauthenticated)
    assert ledger.ledger_remote_auth_failures_total() == before


def test_a_refused_credential_on_a_remote_resource_read_is_unauthenticated(remote):
    error, _ = refusal(403, "Missing or wrong ledger credential")
    with (
        patch.object(ledger, "client", return_value=SimpleNamespace(request=Mock(side_effect=error))),
        pytest.raises(ledger.Unauthenticated, match="^unauthenticated: the ledger server refused the credential$"),
    ):
        ledger.resource(SLUG, "metadata")


def test_a_request_uses_the_bounded_timeout_by_default(remote: None) -> None:
    client = Mock()
    client.return_value.snapshot.return_value = {"tasks": []}
    with patch("scripts.tick.adapters.database_ledger.client", client):
        assert ledger.request(SLUG) == {"tasks": []}
    client.assert_called_once_with("https://hub.example", 10)


@pytest.mark.parametrize("ops", [[{"op": "ack"}], []])
@pytest.mark.parametrize("failure", ["outage", "server error"])
def test_a_remote_write_runs_once_through_an_outage_or_a_server_error(
    remote: None, ops: list[dict[str, str]], failure: str
) -> None:
    error = ConnectionRefusedError("down") if failure == "outage" else refusal(503, "unavailable")[0]
    with (
        patch.object(ledger, "request", side_effect=error) as request,
        patch.object(ledger.time, "sleep") as sleep,
        pytest.raises(SystemExit, match="^(ledger server not answering|server refused: 503)"),
    ):
        ledger.call(SLUG, ops)
    request.assert_called_once_with(SLUG, ops)
    sleep.assert_not_called()


def test_resource_and_export_reads_retry_a_storage_failure_with_a_fresh_client_and_bounded_timeouts(
    remote: None,
) -> None:
    built: list[float] = []
    replies: list[object] = [ConnectionResetError("reset"), {"data": {"title": "T"}}, OSError("gone"), {"data": {}}]

    def request(slug: str, path: str, payload: object = None) -> object:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def client(slug: str, timeout: float) -> SimpleNamespace:
        built.append(timeout)
        return SimpleNamespace(request=request)

    with patch.object(ledger, "client", client), patch.object(ledger.time, "sleep") as sleep:
        assert ledger.resource(SLUG, "metadata") == {"title": "T"}
        assert ledger.export(SLUG) == {}
    assert built == [10, 10, 30, 30]
    assert sleep.call_count == 2


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_hive_credential_is_unauthenticated(remote, code):
    from scripts.tick.adapters import database_ledger

    error, _ = refusal(code, "Missing or wrong hive credential")
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": "hive-credential"}),
        patch.object(database_ledger, "exchanged", side_effect=error),
        pytest.raises(
            ledger.Unauthenticated, match=f"^unauthenticated: the ledger API refused the hive credential: {code}$"
        ),
    ):
        ledger.launch_token(SLUG, WORKER)


def test_a_launch_token_server_error_is_not_an_auth_failure(remote):
    from scripts.tick.adapters import database_ledger

    error, _ = refusal(503, "unavailable")
    before = ledger.ledger_remote_auth_failures_total()
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": "hive-credential"}),
        patch.object(database_ledger, "exchanged", side_effect=error) as exchanged,
        pytest.raises(urllib.error.HTTPError),
    ):
        ledger.launch_token(SLUG, WORKER)
    assert ledger.ledger_remote_auth_failures_total() == before
    assert exchanged.call_count == 1


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_hive_credential_is_asked_once(remote, code):
    from scripts.tick.adapters import database_ledger

    error, _ = refusal(code, "Missing or wrong hive credential")
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": "hive-credential"}),
        patch.object(database_ledger, "exchanged", side_effect=error) as exchanged,
        pytest.raises(ledger.Unauthenticated),
    ):
        ledger.launch_token(SLUG, WORKER)
    assert exchanged.call_count == 1


def test_unauthenticated_refusals_count_each_refusal(remote):
    before = ledger.ledger_remote_auth_failures_total()
    os.environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN")
    os.environ.pop("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", None)
    for _ in range(2):
        with pytest.raises(ledger.Unauthenticated, match="^unauthenticated: a remote ledger client needs"):
            ledger.credentials(SLUG)
    os.environ.pop("AGENTIHOOKS_AGENT_NAME")
    with pytest.raises(ledger.Unauthenticated, match="^unauthenticated: a remote ledger client needs a pinned"):
        ledger.credentials(SLUG)
    assert ledger.ledger_remote_auth_failures_total() == before + 3
