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
    with patch("scripts.tick.adapters.writer_client.client", client):
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


def test_remote_resource_and_export_reads_retry_with_fresh_credentials_and_bounded_timeouts(
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


def test_a_remote_read_retries_a_server_error_and_returns_the_next_answer(remote: None) -> None:
    error, _ = refusal(503, "unavailable")
    request = Mock(side_effect=[error, {"data": {"tasks": []}}])
    with (
        patch.object(ledger, "client", return_value=SimpleNamespace(request=request)),
        patch.object(ledger.time, "sleep") as sleep,
    ):
        assert ledger.resource(SLUG, "metadata") == {"tasks": []}
    assert request.call_count == 2
    sleep.assert_called_once_with(0.5)


def test_a_remote_read_gives_up_after_its_bounded_attempts(remote: None) -> None:
    sleeps: list[float] = []
    request = Mock(side_effect=ConnectionRefusedError("down"))
    with (
        patch.object(ledger, "client", return_value=SimpleNamespace(request=request)),
        patch.object(ledger.time, "sleep", sleeps.append),
        pytest.raises(ConnectionRefusedError, match="^down$"),
    ):
        ledger.resource(SLUG, "metadata")
    assert request.call_count == 3
    assert sleeps == [0.5, 1.0]


def test_a_remote_read_gives_up_on_a_lasting_server_error(remote: None) -> None:
    request = Mock(side_effect=[refusal(500, "broken")[0] for _ in range(3)])
    with (
        patch.object(ledger, "client", return_value=SimpleNamespace(request=request)),
        patch.object(ledger.time, "sleep"),
        pytest.raises(urllib.error.HTTPError) as raised,
    ):
        ledger.resource(SLUG, "metadata")
    assert raised.value.code == 500
    assert request.call_count == 3


def test_a_refused_ledger_credential_is_unauthenticated_and_not_retried(remote: None) -> None:
    before = ledger.ledger_remote_auth_failures_total()
    reason = "the ledger server refused the credential"
    counted = ledger.AUTH_FAILURES[reason]
    for _ in range(2):
        request = Mock(side_effect=refusal(403, "Missing or wrong ledger credential")[0])
        with (
            patch.object(ledger, "client", return_value=SimpleNamespace(request=request)),
            pytest.raises(ledger.Unauthenticated, match=f"^unauthenticated: {reason}$"),
        ):
            ledger.resource(SLUG, "metadata")
        assert request.call_count == 1
    assert ledger.AUTH_FAILURES[reason] == counted + 2
    assert ledger.ledger_remote_auth_failures_total() == before + 2


def test_a_transient_outage_retries_reads_without_starting_a_server(remote: None) -> None:
    request = Mock(side_effect=[ConnectionRefusedError("ledger outage"), {"data": {"title": "T"}}])
    with (
        patch.object(ledger, "client", return_value=SimpleNamespace(request=request)),
        patch.object(ledger.time, "sleep") as sleep,
        patch("subprocess.run") as run,
        patch("subprocess.Popen") as popen,
    ):
        assert ledger.resource(SLUG, "metadata") == {"title": "T"}
    assert request.call_count == 2
    sleep.assert_called_once_with(0.5)
    run.assert_not_called()
    popen.assert_not_called()


def test_a_missing_token_or_foreign_label_is_unauthenticated_and_never_uses_the_local_credential(remote: None) -> None:
    from scripts.swarm_ledger import ledger_authority

    os.environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN")
    os.environ.pop("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", None)
    with (
        patch.object(ledger.repository, "token", side_effect=AssertionError("the local credential was read")),
        pytest.raises(ledger.Unauthenticated, match="^unauthenticated: a remote ledger client needs"),
    ):
        ledger.credentials(SLUG)
    token = ledger_authority.agent_token("admin", SLUG, WORKER)
    with patch.object(ledger_authority, "remote", return_value=False):
        assert ledger_authority.principal("admin", SLUG, token, WORKER) == WORKER
        assert ledger_authority.principal("admin", SLUG, token, "eng-9@another-swarm") is None


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
