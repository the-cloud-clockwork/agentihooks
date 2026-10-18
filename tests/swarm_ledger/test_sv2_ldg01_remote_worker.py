import io
import json
import os
import re
import urllib.error
from unittest.mock import Mock, patch

import pytest

from scripts.swarm_ledger import ledger
from tests import sv2_ldg01_cases as cases
from tests.swarm_ledger.test_ledger_authority import SLUG, WORKER
from tests.swarm_ledger.test_ledger_authority import live as _authority_live
from tests.swarm_ledger.test_remote_ledger_client import hive as _remote_hive

pytestmark = pytest.mark.xdist_group("fakeredis")

live = _authority_live
hive = _remote_hive
REMOTE = {
    "AGENTIHOOKS_DEPLOYMENT": "distributed",
    "AGENTIHOOKS_SWARM": "rig-grade-swarm",
    "AGENTIHOOKS_AGENT_NAME": WORKER,
    "LEDGER_URL": "https://hub.example",
    "AGENTIHOOKS_LEDGER_AGENT_TOKEN": "launch-token",
}


def test_worker_with_a_read_only_home_reads_its_task_and_posts_an_update(live, hive, tmp_path):
    token = cases.grant()
    first = cases.positive(live, tmp_path / "first", "first", token)
    second = cases.positive(live, tmp_path / "second", "second", token)
    assert first == second
    assert first == {
        "task_read": True,
        "update_committed": True,
        "home_changed": False,
        "ledger_directory_created": False,
        cases.METRIC: 0,
    }


def test_a_missing_token_or_foreign_label_is_unauthenticated_and_never_uses_the_local_credential(live, hive, tmp_path):
    token = cases.grant()
    first = cases.negative(live, tmp_path / "first", "first", token)
    second = cases.negative(live, tmp_path / "second", "second", token)
    assert first == second
    assert first == {
        "missing_token": "unauthenticated",
        "foreign_label": "unauthenticated",
        "protected_state_changed": False,
        "credential_disclosed": False,
        "new_valid_request_posted": True,
        cases.METRIC: 2,
    }


def test_a_transient_outage_retries_reads_without_starting_a_server(live, hive):
    token = cases.grant()
    first = cases.recovery(live, "first", token)
    second = cases.recovery(live, "second", token)
    assert first == second
    assert first == {
        "read_attempts": 3,
        "write_attempts": 3,
        "servers_started": 0,
        "lost_response_applied": True,
        "replayed_effects": 1,
        cases.METRIC: 0,
    }


@pytest.fixture
def remote():
    with patch.dict(os.environ, REMOTE), patch.object(ledger, "BASE", ""):
        yield


def refusal(code, message):
    body = json.dumps({"error": {"code": "forbidden", "message": message}}).encode()
    return urllib.error.HTTPError("u", code, "Refused", {}, io.BytesIO(body)), body.decode()


def test_a_remote_read_gives_up_after_its_bounded_attempts(remote):
    sleeps = []
    with (
        patch.object(ledger, "request", side_effect=ConnectionRefusedError("down")) as request,
        patch.object(ledger.time, "sleep", sleeps.append),
        patch.object(ledger.repository, "exists", side_effect=AssertionError("local repository read")),
        patch.object(ledger.subprocess, "run") as run,
        pytest.raises(SystemExit, match="^ledger server not answering on https://hub.example: down$"),
    ):
        ledger.call(SLUG)
    assert request.call_count == 3
    assert sleeps == [0.5, 1.0]
    run.assert_not_called()


def test_a_remote_read_retries_a_server_error_and_returns_the_next_answer(remote):
    error, _ = refusal(503, "unavailable")
    with (
        patch.object(ledger, "request", side_effect=[error, {"tasks": []}]) as request,
        patch.object(ledger.time, "sleep") as sleep,
    ):
        assert ledger.call(SLUG) == {"tasks": []}
    assert request.call_count == 2
    sleep.assert_called_once_with(0.5)


def test_a_remote_read_gives_up_on_a_lasting_server_error(remote):
    errors = [refusal(500, "broken")[0] for _ in range(3)]
    with (
        patch.object(ledger, "request", side_effect=errors) as request,
        patch.object(ledger.time, "sleep"),
        pytest.raises(SystemExit, match="^server refused: 500 "),
    ):
        ledger.call(SLUG)
    assert request.call_count == 3


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


def test_a_request_uses_the_bounded_timeout_by_default(remote):
    client = Mock()
    client.return_value.snapshot.return_value = {"tasks": []}
    with (
        patch("scripts.swarm_ledger.api.client.ResourceClient", client),
        patch.object(ledger, "credentials", return_value={"X-Ledger-Token": "t"}),
    ):
        assert ledger.request(SLUG) == {"tasks": []}
    client.assert_called_once_with("https://hub.example", {"X-Ledger-Token": "t"}, 10)


@pytest.mark.parametrize("ops", [[{"op": "ack"}], []])
@pytest.mark.parametrize("failure", ["outage", "server error"])
def test_a_remote_write_runs_once_through_an_outage_or_a_server_error(remote, ops, failure):
    error = ConnectionRefusedError("down") if failure == "outage" else refusal(503, "unavailable")[0]
    with (
        patch.object(ledger, "request", side_effect=error) as request,
        patch.object(ledger.time, "sleep") as sleep,
        patch.object(ledger.repository, "exists", side_effect=AssertionError("local repository read")),
        pytest.raises(SystemExit, match="^(ledger server not answering|server refused: 503)"),
    ):
        ledger.call(SLUG, ops)
    request.assert_called_once_with(SLUG, ops, False)
    sleep.assert_not_called()


def test_a_refused_ledger_credential_is_unauthenticated_and_not_retried(remote):
    before = ledger.ledger_remote_auth_failures_total()
    reason = "the ledger server refused the credential"
    counted = ledger.AUTH_FAILURES[reason]
    for _ in range(2):
        error, _ = refusal(403, "Missing or wrong ledger credential")
        with (
            patch.object(ledger, "request", side_effect=error) as request,
            pytest.raises(ledger.Unauthenticated, match=f"^unauthenticated: {reason}$"),
        ):
            ledger.call(SLUG)
        assert request.call_count == 1
    assert ledger.AUTH_FAILURES[reason] == counted + 2
    assert ledger.ledger_remote_auth_failures_total() == before + 2


def test_a_remote_permission_refusal_is_not_an_auth_failure(remote):
    error, body = refusal(403, "Checkbox changes need the operator")
    before = ledger.ledger_remote_auth_failures_total()
    with patch.object(ledger, "request", side_effect=error):
        with pytest.raises(SystemExit, match=f"^server refused: 403 {re.escape(body)}$") as refused:
            ledger.call(SLUG, [{"op": "ack"}])
    assert not isinstance(refused.value, ledger.Unauthenticated)
    assert ledger.ledger_remote_auth_failures_total() == before


class Client:
    built = []
    replies = []
    requests = 0

    def __init__(self, base, credentials, timeout):
        self.built.append((base, credentials, timeout))

    def request(self, slug, path, payload=None):
        Client.requests += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_remote_resource_and_export_reads_retry_with_fresh_credentials_and_bounded_timeouts(remote):
    Client.built[:] = []
    Client.replies[:] = [ConnectionResetError("reset"), {"data": {"title": "T"}}, OSError("gone"), {"data": {}}]
    tokens = iter(["first", "second", "third", "fourth"])
    with (
        patch("scripts.swarm_ledger.api.client.ResourceClient", Client),
        patch.object(ledger, "credentials", side_effect=lambda slug, service: {"X-Ledger-Token": next(tokens)}),
        patch.object(ledger.time, "sleep") as sleep,
    ):
        assert ledger.resource(SLUG, "metadata") == {"title": "T"}
        assert ledger.export(SLUG) == {}
    names = ("first", "second", "third", "fourth")
    assert Client.built == [("https://hub.example", {"X-Ledger-Token": name}, 10) for name in names]
    assert sleep.call_count == 2


def test_a_refused_credential_on_a_remote_resource_read_is_unauthenticated(remote):
    Client.replies[:] = [refusal(403, "Missing or wrong ledger credential")[0]]
    with (
        patch("scripts.swarm_ledger.api.client.ResourceClient", Client),
        patch.object(ledger, "credentials", return_value={}),
        pytest.raises(ledger.Unauthenticated, match="^unauthenticated: the ledger server refused the credential$"),
    ):
        ledger.resource(SLUG, "metadata")


def test_local_resource_and_export_reads_run_once(remote):
    Client.built[:] = []
    Client.requests = 0
    with (
        patch("scripts.swarm_ledger.api.client.ResourceClient", Client),
        patch.object(ledger, "credentials", return_value={}),
        patch.object(ledger.ledger_link, "remote", return_value=False),
    ):
        for read in (lambda: ledger.resource(SLUG, "metadata"), lambda: ledger.export(SLUG)):
            Client.replies[:] = [ConnectionResetError("reset"), {"data": {}}]
            with pytest.raises(ConnectionResetError):
                read()
    assert (len(Client.built), Client.requests) == (2, 2)


def test_the_launch_token_fetch_has_a_bounded_timeout(remote):
    client = Mock()
    client.return_value.request.return_value = {"data": {"token": "hive.issued"}}
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": "hive-credential"}),
        patch("scripts.swarm_ledger.api.client.ResourceClient", client),
    ):
        assert ledger.launch_token(SLUG, WORKER) == "hive.issued"
    headers = {"X-Hive-Credential": "hive-credential", "X-Ledger-Agent": WORKER}
    client.assert_called_once_with("https://hub.example", headers, 10)


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_hive_credential_is_unauthenticated(remote, code):
    client = Mock()
    client.return_value.request.side_effect = refusal(code, "Missing or wrong hive credential")[0]
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": "hive-credential"}),
        patch("scripts.swarm_ledger.api.client.ResourceClient", client),
        pytest.raises(
            ledger.Unauthenticated, match=f"^unauthenticated: the ledger server refused the hive credential: {code}$"
        ),
    ):
        ledger.launch_token(SLUG, WORKER)


def test_a_launch_token_server_error_is_not_an_auth_failure(remote):
    client = Mock()
    client.return_value.request.side_effect = refusal(503, "unavailable")[0]
    before = ledger.ledger_remote_auth_failures_total()
    with (
        patch.dict(os.environ, {"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": "hive-credential"}),
        patch("scripts.swarm_ledger.api.client.ResourceClient", client),
        pytest.raises(urllib.error.HTTPError),
    ):
        ledger.launch_token(SLUG, WORKER)
    assert ledger.ledger_remote_auth_failures_total() == before


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
