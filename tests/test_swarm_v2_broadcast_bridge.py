import io
import json
import socket
import stat
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from hooks import config, hook_manager
from hooks.context import broadcast as hb
from scripts.swarm_v2 import broadcast_bridge, broadcasts, worker_home
from tests.test_swarm_v2_broadcasts import CHANNELS, LOCAL, REMOTE, SLUG, Epoch
from tests.test_swarm_v2_broadcasts_api import EXPIRES_MS, Fleet, foreign, forged

pytestmark = pytest.mark.unit

UNSERVED = "http://127.0.0.1:9"


def no_redis(environ=None):
    raise AssertionError("a remote worker holds no Redis credential")


@pytest.fixture
def world(monkeypatch, tmp_path):
    found = Fleet(monkeypatch)
    monkeypatch.setattr(hb, "_broadcast_path", lambda: tmp_path / "broadcast.json")
    monkeypatch.setattr(hb, "_sessions_path", lambda: tmp_path / "active-sessions.json")
    monkeypatch.setattr(hb, "datetime", Epoch)
    monkeypatch.setattr("scripts.swarm.store.connect", no_redis)
    return found


@pytest.fixture
def served(world):
    from scripts.swarm_v2.api.server import Routes, serve

    server = serve(Routes(world.api, None), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()
    server.server_close()


def grant(monkeypatch, attempt, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    return worker_home.main(["grant", str(attempt)])


def launch(world, tmp_path, seat, monkeypatch, url=UNSERVED):
    agent, token = world.worker(seat)
    attempt = tmp_path / "attempt"
    (attempt / "run").mkdir(parents=True, exist_ok=True)
    assert grant(monkeypatch, attempt, token + "\n") == 0
    path = str(broadcast_bridge.grant_path(attempt))
    return agent, token, {broadcasts.FLAG: "1", broadcast_bridge.GRANT_FILE: path, broadcast_bridge.API_URL: url}


def test_a_worker_launch_writes_its_grant_where_only_the_worker_reads_it(world, tmp_path, monkeypatch):
    _, token, environ = launch(world, tmp_path, REMOTE, monkeypatch)
    path = broadcast_bridge.grant_path(tmp_path / "attempt")
    assert path == tmp_path / "attempt" / "run" / "launch-grant"
    assert path.read_text(encoding="utf-8") == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert broadcast_bridge.read_grant(environ) == token
    assert sorted(p.name for p in path.parent.iterdir()) == ["launch-grant"]


def test_a_rewritten_grant_replaces_the_previous_one(tmp_path):
    path = tmp_path / "launch-grant"
    broadcast_bridge.store_grant(path, "v2.first.grant")
    broadcast_bridge.store_grant(path, "v2.second.grant\n")
    assert broadcast_bridge.read_grant({broadcast_bridge.GRANT_FILE: str(path)}) == "v2.second.grant"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_grant_is_staged_beside_its_file_and_never_in_the_shared_temporary_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "absent"))
    path = tmp_path / "run" / "launch-grant"
    path.parent.mkdir()
    broadcast_bridge.store_grant(path, "v2.staged.grant")
    assert path.read_bytes() == b"v2.staged.grant"
    assert [p.name for p in path.parent.iterdir()] == ["launch-grant"]


def test_a_grant_file_left_open_to_others_is_replaced_privately(tmp_path):
    path = tmp_path / "launch-grant"
    path.write_text("old")
    path.chmod(0o644)
    broadcast_bridge.store_grant(path, "v2.new.grant")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "v2.new.grant"


def test_the_grant_command_refuses_an_empty_grant(tmp_path, capsys, monkeypatch):
    (tmp_path / "run").mkdir()
    assert grant(monkeypatch, tmp_path, " \n") == 1
    assert capsys.readouterr().err == "ERROR: no launch grant on standard input\n"
    assert not broadcast_bridge.grant_path(tmp_path).exists()


def quiet(monkeypatch):
    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    for flag in (
        "AMYGDALA_ENABLED",
        "QUOTA_POLICY_ENABLED",
        "QUOTA_USAGE_INJECTION_ENABLED",
        "CI_MANIFESTO_ENABLED",
        "VOICE_ENABLED",
        "CONTROLS_BYPASS_ENABLED",
    ):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(config, "BROADCAST_ENABLED", True)
    for name in (
        "_confirm_inbox",
        "_swarm_heartbeat",
        "_operator_mode",
        "_request_trace_flush",
        "_inject_refocus",
        "_inject_ledger_decision",
    ):
        monkeypatch.setattr(hook_manager, name, Mock())
    monkeypatch.setattr("hooks.context.rules_refresh.maybe_inject", Mock())
    monkeypatch.setattr("hooks.context.enforcement.get_user_prompt_enforcements", Mock(return_value=""))


def test_a_remote_worker_prompt_receives_the_fleet_broadcast(world, served, tmp_path, monkeypatch):
    agent, _, environ = launch(world, tmp_path, REMOTE, monkeypatch, served)
    world.announce()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        environ["AGENTIHOOKS_SWARM_REDIS_URL"] = f"redis://127.0.0.1:{probe.getsockname()[1]}/0"
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(hb, "_get_session_channels", lambda session_id: list(CHANNELS))
    monkeypatch.setattr(hb.quarantine, "mode", lambda: "off")
    quiet(monkeypatch)
    delivered, logged = Mock(), Mock()
    monkeypatch.setattr("hooks.context.broadcast.check_and_inject_broadcasts", delivered)
    monkeypatch.setattr(hook_manager, "log", logged)

    hook_manager.on_user_prompt_submit({"session_id": "s-remote", "prompt": "", "cwd": ""})

    delivered.assert_called_once_with("s-remote")
    assert [c for c in logged.call_args_list if "broadcast" in c.args[0]] == []
    assert world.publisher.delivery(REMOTE, "fleet-warning")["execution_id"] == agent.execution_id
    assert [m["id"] for m in hb.list_broadcasts()] == [f"{SLUG}:fleet-warning:1:s-remote"]
    assert [m["id"] for m in hb.get_critical_broadcasts("s-remote")] == [f"{SLUG}:fleet-warning:1:s-remote"]
    assert hb.get_critical_broadcasts("s-other") == []


def test_the_prompt_hook_claims_on_the_session_channels_before_it_delivers(monkeypatch):
    order = []
    quiet(monkeypatch)
    monkeypatch.setattr(hb, "_get_session_channels", lambda session_id: [f"brain-{session_id}"])
    monkeypatch.setattr(broadcast_bridge, "claim", lambda *args: order.append(("claim", *args[:2])) or 0)
    monkeypatch.setattr("hooks.context.broadcast.check_and_inject_broadcasts", lambda s: order.append(("deliver", s)))
    hook_manager.on_user_prompt_submit({"session_id": "s1", "prompt": "", "cwd": ""})
    assert order == [("claim", "s1", ["brain-s1"]), ("deliver", "s1")]


def test_a_failing_claim_is_logged_and_local_broadcasts_still_deliver(monkeypatch):
    quiet(monkeypatch)
    log = Mock()
    monkeypatch.setattr(hook_manager, "log", log)
    monkeypatch.setattr(broadcast_bridge, "claim", Mock(side_effect=RuntimeError("boom")))
    delivered = Mock()
    monkeypatch.setattr("hooks.context.broadcast.check_and_inject_broadcasts", delivered)
    hook_manager._prompt_broadcasts("s1")
    delivered.assert_called_once_with("s1")
    assert log.call_args_list == [call("fleet broadcast claim failed", {"error": "boom"})]


def test_a_failing_delivery_is_logged(monkeypatch):
    log = Mock()
    monkeypatch.setattr(hook_manager, "log", log)
    monkeypatch.setattr(broadcast_bridge, "claim", Mock(return_value=0))
    monkeypatch.setattr("hooks.context.broadcast.check_and_inject_broadcasts", Mock(side_effect=OSError("full")))
    hook_manager._prompt_broadcasts("s1")
    assert log.call_args_list == [call("broadcast user_prompt failed", {"error": "full"})]


def test_the_bridge_claims_nothing_while_the_fleet_path_is_off_or_a_setting_is_missing(
    world, served, tmp_path, monkeypatch
):
    _, _, environ = launch(world, tmp_path, LOCAL, monkeypatch, served)
    world.announce()
    assert broadcast_bridge.API_URL == "AGENTIHOOKS_SWARM_API_URL"
    read, post = Mock(), Mock()
    with monkeypatch.context() as stubbed:
        stubbed.setattr(broadcast_bridge, "read_grant", read)
        stubbed.setattr("urllib.request.urlopen", post)
        for missing in (broadcast_bridge.GRANT_FILE, broadcast_bridge.API_URL, broadcasts.FLAG):
            assert broadcast_bridge.claim("s-local", list(CHANNELS), {**environ, missing: ""}) == 0
        assert broadcast_bridge.claim("s-local", list(CHANNELS), {**environ, broadcasts.FLAG: "0"}) == 0
    assert (read.call_count, post.call_count) == (0, 0)
    absent = {**environ, broadcast_bridge.GRANT_FILE: str(tmp_path / "absent")}
    assert broadcast_bridge.claim("s-local", list(CHANNELS), absent) == 0
    assert world.deliveries() == []
    assert broadcast_bridge.claim("s-local", list(CHANNELS), environ) == 1
    assert broadcast_bridge.claim("s-local", list(CHANNELS), environ) == 0


def unregistered(world, agent, token):
    return world.start("eng-9@fixture", "task-unregistered")[1]


def superseded(world, agent, token):
    world.start(REMOTE, agent.task, previous=agent.execution_id)
    return token


def expired(world, agent, token):
    world.clock[0] = EXPIRES_MS
    return token


REFUSED = {
    "forged": lambda world, agent, token: forged(world, token),
    "foreign": lambda world, agent, token: foreign(world, token),
    "unregistered": unregistered,
    "superseded": superseded,
    "expired": expired,
}


@pytest.mark.parametrize("refused", sorted(REFUSED))
def test_a_refused_grant_claims_nothing(world, served, tmp_path, monkeypatch, refused):
    agent, token, environ = launch(world, tmp_path, REMOTE, monkeypatch, served)
    world.announce()
    broadcast_bridge.store_grant(Path(environ[broadcast_bridge.GRANT_FILE]), REFUSED[refused](world, agent, token))
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0
    assert hb.list_broadcasts() == []
    assert world.deliveries() == []


def test_an_unreachable_api_claims_nothing(world, tmp_path, monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
    _, _, environ = launch(world, tmp_path, REMOTE, monkeypatch, closed)
    world.announce()
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0
    assert hb.list_broadcasts() == []


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_the_bridge_posts_its_grant_and_channels_to_the_claim_endpoint(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: sent.append((request, timeout)) or Answer(b'{"deliveries": []}'),
    )
    fleet = broadcast_bridge.RemoteFleet("https://swarm.invalid/apiX/")
    assert fleet.claim("v2.grant.sig", ["amygdala"]) == []
    assert fleet.claim("v2.grant.sig", ["brain"], "claim-1") == []
    first, second = sent
    assert (first[0].full_url, first[0].get_method(), first[1]) == (
        "https://swarm.invalid/apiX/v2/broadcasts/claim",
        "POST",
        5,
    )
    assert dict(first[0].header_items()) == {"Authorization": "Bearer v2.grant.sig", "Content-type": "application/json"}
    assert json.loads(first[0].data) == {"channels": ["amygdala"]}
    assert json.loads(second[0].data) == {"channels": ["brain"], "claim_id": "claim-1"}


def test_an_answer_that_is_not_json_claims_nothing(world, tmp_path, monkeypatch):
    _, _, environ = launch(world, tmp_path, REMOTE, monkeypatch)
    sent = []
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: sent.append(request) or Answer(b"<html>"))
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0
    assert len(sent) == 1
    assert hb.list_broadcasts() == []


def test_a_lost_answer_is_retried_once_with_the_same_claim_id(world, served, tmp_path, monkeypatch):
    import urllib.request
    from urllib.error import URLError

    _, _, environ = launch(world, tmp_path, REMOTE, monkeypatch, served)
    world.announce()
    real = urllib.request.urlopen

    def lost(request, timeout):
        real(request, timeout=timeout).close()
        raise URLError("reset")

    sent = []
    answers = [lost, real]
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: sent.append(json.loads(request.data)) or answers.pop(0)(request, timeout=timeout),
    )
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 1
    first, second = sent
    assert first == second == {"channels": CHANNELS, "claim_id": first["claim_id"]}
    assert len(first["claim_id"]) == 32
    assert [m["id"] for m in hb.list_broadcasts()] == [f"{SLUG}:fleet-warning:1:s-remote"]
    answers[:] = [real]
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0
    assert sent[2]["claim_id"] != first["claim_id"]


def test_a_transport_failure_is_tried_twice_and_a_refusal_once(world, tmp_path, monkeypatch):
    from urllib.error import HTTPError, URLError

    _, _, environ = launch(world, tmp_path, REMOTE, monkeypatch)
    tries = Mock(side_effect=URLError("down"))
    monkeypatch.setattr("urllib.request.urlopen", tries)
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0
    assert tries.call_count == 2
    refused = Mock(side_effect=HTTPError("http://swarm.invalid", 401, "unauthenticated", {}, None))
    monkeypatch.setattr("urllib.request.urlopen", refused)
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0
    assert refused.call_count == 1
