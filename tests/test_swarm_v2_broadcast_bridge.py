import base64
import io
import json
import stat
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks import config, hook_manager
from hooks.context import broadcast as hb
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2 import broadcast_bridge, broadcasts, worker_home
from scripts.swarm_v2.auth_context import DEFAULT_TTL_SECONDS as TTL_SECONDS
from tests.test_swarm_v2_broadcasts import CHANNELS, LOCAL, PUBLISH_MS, REMOTE, SLUG, WARNING, Epoch, World

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def world(monkeypatch, tmp_path):
    found = World(monkeypatch)
    found.clock[0] = PUBLISH_MS
    monkeypatch.setattr(hb, "_broadcast_path", lambda: tmp_path / "broadcast.json")
    monkeypatch.setattr(hb, "_sessions_path", lambda: tmp_path / "active-sessions.json")
    monkeypatch.setattr(hb, "datetime", Epoch)
    monkeypatch.setattr("scripts.swarm.store.connect", lambda environ: RedisStore(found.store.redis))
    return found


def grant(monkeypatch, attempt, text):
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    return worker_home.main(["grant", str(attempt)])


def launch(world, tmp_path, seat, monkeypatch):
    token = world.token(seat)
    world.authority.authorize(token)
    attempt = tmp_path / "attempt"
    (attempt / "run").mkdir(parents=True, exist_ok=True)
    assert grant(monkeypatch, attempt, token + "\n") == 0
    return token, {broadcasts.FLAG: "1", broadcast_bridge.GRANT_FILE: str(broadcast_bridge.grant_path(attempt))}


def test_a_worker_launch_writes_its_grant_where_only_the_worker_reads_it(world, tmp_path, monkeypatch):
    token, environ = launch(world, tmp_path, REMOTE, monkeypatch)
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


def test_the_grant_command_refuses_an_empty_grant(tmp_path, capsys, monkeypatch):
    (tmp_path / "run").mkdir()
    assert grant(monkeypatch, tmp_path, " \n") == 1
    assert capsys.readouterr().err == "ERROR: no launch grant on standard input\n"
    assert not broadcast_bridge.grant_path(tmp_path).exists()


def test_a_remote_worker_prompt_receives_the_fleet_broadcast(world, tmp_path, monkeypatch):
    _, environ = launch(world, tmp_path, REMOTE, monkeypatch)
    world.announce(WARNING)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(hb, "_get_session_channels", lambda session_id: list(CHANNELS))
    monkeypatch.setattr(hb.quarantine, "mode", lambda: "off")
    quiet(monkeypatch)
    delivered = Mock()
    monkeypatch.setattr("hooks.context.broadcast.check_and_inject_broadcasts", delivered)

    hook_manager.on_user_prompt_submit({"session_id": "s-remote", "prompt": "", "cwd": ""})

    delivered.assert_called_once_with("s-remote")
    assert [m["id"] for m in hb.get_critical_broadcasts("s-remote")] == [f"{SLUG}:fleet-warning:1:s-remote"]
    assert hb.get_critical_broadcasts("s-other") == []
    assert json.loads(world.store.redis.hget(world.fleet.key("broadcast-claims"), REMOTE))["generation"] == 1


def test_the_prompt_hook_claims_before_it_delivers(monkeypatch):
    order = []
    quiet(monkeypatch)
    monkeypatch.setattr(hb, "_get_session_channels", lambda session_id: ["brain"])
    monkeypatch.setattr(broadcast_bridge, "claim", lambda *args: order.append(("claim", *args[:2])) or 0)
    monkeypatch.setattr("hooks.context.broadcast.check_and_inject_broadcasts", lambda s: order.append(("deliver", s)))
    hook_manager.on_user_prompt_submit({"session_id": "s1", "prompt": "", "cwd": ""})
    assert order == [("claim", "s1", ["brain"]), ("deliver", "s1")]


def test_a_failing_claim_still_delivers_local_broadcasts(monkeypatch):
    quiet(monkeypatch)
    monkeypatch.setattr(broadcast_bridge, "claim", Mock(side_effect=RuntimeError("boom")))
    delivered = Mock()
    monkeypatch.setattr("hooks.context.broadcast.check_and_inject_broadcasts", delivered)
    hook_manager.on_user_prompt_submit({"session_id": "s1", "prompt": "", "cwd": ""})
    delivered.assert_called_once_with("s1")


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


def test_the_bridge_claims_nothing_while_the_fleet_path_is_off_or_the_grant_is_missing(world, tmp_path, monkeypatch):
    _, environ = launch(world, tmp_path, LOCAL, monkeypatch)
    world.announce(WARNING)
    for off in ({}, {broadcasts.FLAG: "1"}, {**environ, broadcasts.FLAG: "0"}):
        assert broadcast_bridge.claim("s-local", list(CHANNELS), off) == 0
    missing = {**environ, broadcast_bridge.GRANT_FILE: str(tmp_path / "absent")}
    assert broadcast_bridge.claim("s-local", list(CHANNELS), missing) == 0
    assert world.store.redis.hget(world.fleet.key("broadcast-claims"), LOCAL) is None
    assert broadcast_bridge.claim("s-local", list(CHANNELS), environ) == 1
    assert broadcast_bridge.claim("s-local", list(CHANNELS), environ) == 0


def grant_with(token, **changes):
    head, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "==")) | changes
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{head}.{encoded}.{signature}"


def test_a_grant_is_authorized_only_by_its_recorded_registration(world, tmp_path):
    token = world.token(REMOTE)
    with pytest.raises(SwarmError, match="unauthenticated"):
        broadcast_bridge.registered(world.store, token)
    registration = world.authority.authorize(token)
    assert broadcast_bridge.registered(world.store, token) == registration
    for forged in (grant_with(token, grant_id="lgr-other"), grant_with(token, swarm_id="other"), "v2.%%%.x", "x"):
        with pytest.raises(SwarmError, match="unauthenticated"):
            broadcast_bridge.registered(world.store, forged)


def test_an_unregistered_grant_claims_nothing(world, tmp_path):
    token = world.token(REMOTE)
    world.announce(WARNING)
    path = tmp_path / "launch-grant"
    broadcast_bridge.store_grant(path, token)
    environ = {broadcasts.FLAG: "1", broadcast_bridge.GRANT_FILE: str(path)}
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0
    assert hb.list_broadcasts() == []


def test_a_superseded_or_foreign_grant_claims_nothing(world, tmp_path, monkeypatch):
    old, environ = launch(world, tmp_path, REMOTE, monkeypatch)
    world.announce(WARNING)
    successor = world.token(REMOTE, previous=world.agents[REMOTE].execution_id)
    world.authority.authorize(successor)
    with pytest.raises(SwarmError, match="stale_generation"):
        broadcast_bridge.registered(world.store, old)
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0
    broadcast_bridge.store_grant(Path(environ[broadcast_bridge.GRANT_FILE]), grant_with(successor, swarm_id="other"))
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0
    assert hb.list_broadcasts() == []


def test_an_expired_grant_claims_nothing(world, tmp_path, monkeypatch):
    token, environ = launch(world, tmp_path, REMOTE, monkeypatch)
    world.announce(WARNING)
    world.clock[0] = PUBLISH_MS + (TTL_SECONDS - 1) * 1000
    assert broadcast_bridge.registered(world.store, token).seat_id == REMOTE
    world.clock[0] = PUBLISH_MS + TTL_SECONDS * 1000
    with pytest.raises(SwarmError, match="unauthenticated"):
        broadcast_bridge.registered(world.store, token)
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0
    assert hb.list_broadcasts() == []


def test_a_grant_whose_claims_are_incomplete_is_unauthenticated(world):
    token = world.token(REMOTE)
    world.authority.authorize(token)
    head, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims.pop("swarm_id")
    short = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    for broken in (f"{head}.{short}.{signature}", f"v1.{payload}.{signature}", grant_with(token, generation="1")):
        with pytest.raises(SwarmError, match="unauthenticated"):
            broadcast_bridge.registered(world.store, broken)


def test_a_grant_file_left_open_to_others_is_replaced_privately(tmp_path):
    path = tmp_path / "launch-grant"
    path.write_text("old")
    path.chmod(0o644)
    broadcast_bridge.store_grant(path, "v2.new.grant")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "v2.new.grant"


def test_an_unreachable_fleet_claims_nothing(world, tmp_path, monkeypatch):
    import redis

    _, environ = launch(world, tmp_path, REMOTE, monkeypatch)

    def down(environ):
        raise redis.ConnectionError("down")

    monkeypatch.setattr("scripts.swarm.store.connect", down)
    assert broadcast_bridge.claim("s-remote", list(CHANNELS), environ) == 0


def test_the_bridge_never_authenticates_an_operator():
    with pytest.raises(SwarmError, match="forbidden_scope"):
        broadcast_bridge.no_operator("anything")
