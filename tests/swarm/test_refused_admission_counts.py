import json
import time
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REFUSED = "No permissions to access a key"
NO_READING = "missing telemetry: no fresh headroom reading"


def _member_store(readable: str) -> Any:
    """A store whose hive admission counts run under a Redis ACL user that may read only one hive's admission key,
    while every other key reads as the member's read only grant on the swarm keyspace would."""
    import fakeredis

    from scripts.hive import admission
    from scripts.swarm.store import RedisStore

    server = fakeredis.FakeServer()
    admin = fakeredis.FakeRedis(server=server, decode_responses=True)
    admin.acl_setuser(
        "hive-near", enabled=True, nopass=True, categories=["+@all"], keys=[f"~{admission.key(readable)}"]
    )
    member = fakeredis.FakeRedis(server=server, decode_responses=True, username="hive-near")
    member.auth("", username="hive-near")

    class Counted(fakeredis.FakeRedis):
        def zcount(self, name: Any, min: Any, max: Any) -> Any:
            if str(name).startswith(admission.key("")):
                return member.zcount(name, min, max)
            return super().zcount(name, min, max)

    return RedisStore(Counted(server=server, decode_responses=True))


def _beat(store: Any, now: int, *hives: str) -> None:
    from scripts.hive import registry

    for hive in hives:
        registry.register(store.redis, hive, hive, [])
        registry.beat(store.redis, hive, {"heartbeat_at": now, "beat_at": now})


@pytest.mark.feature("autoscale-and-ledger-hives-name-each-refused-admission-count")
def test_the_autoscale_command_names_the_hive_admission_count_the_redis_login_refuses_and_prints_the_rest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from scripts.swarm import capacity, cli
    from scripts.swarm.store import SwarmConfig
    from tests.swarm.test_tick import FakeLedger

    store = _member_store("near")
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0))
    host = {"room": 6, "reason": "worker quota leaves 6 Pods", "limit": "quota pods", "held": False, "granted_at": 1}
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps({"host": host}))
    _beat(store, int(time.time() * 1000), "near", "far")
    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: [])
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    binding = f"binding limit hive admission, room 0: hive near {NO_READING}"
    cli.cmd_autoscale(store, argparse.Namespace(slug="sw", fixture=None, json=False))
    assert capsys.readouterr().out.splitlines()[-3:] == [
        f"hive near  admission room 0  {NO_READING}",
        f"hive far  admission count unreadable: {REFUSED}",
        binding,
    ]
    cli.cmd_autoscale(store, argparse.Namespace(slug="sw", fixture=None, json=True))
    shown = json.loads(capsys.readouterr().out)
    assert shown["hive_admission"] == [{"hive": "near", "room": 0, "limit": NO_READING}]
    assert shown["admission_unreadable"] == {"far": REFUSED}
    assert shown["binding"] == binding


@pytest.mark.feature("autoscale-and-ledger-hives-name-each-refused-admission-count")
def test_the_autoscale_command_names_every_refused_count_when_no_hive_count_is_readable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from scripts.swarm import capacity, cli
    from scripts.swarm.store import SwarmConfig
    from tests.swarm.test_tick import FakeLedger

    store = _member_store("near")
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, max_plan=0))
    host = {"room": 6, "reason": "worker quota leaves 6 Pods", "limit": "quota pods", "held": False, "granted_at": 1}
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps({"host": host}))
    _beat(store, int(time.time() * 1000), "far")
    monkeypatch.setattr(capacity, "accounts", lambda env, now, refresh=True: [])
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    cli.cmd_autoscale(store, argparse.Namespace(slug="sw", fixture=None, json=False))
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == f"hive far  admission count unreadable: {REFUSED}"
    assert not [line for line in out if line.startswith(("hive near", "binding limit"))]
    cli.cmd_autoscale(store, argparse.Namespace(slug="sw", fixture=None, json=True))
    shown = json.loads(capsys.readouterr().out)
    assert shown["admission_unreadable"] == {"far": REFUSED}
    assert "hive_admission" not in shown
    assert "binding" not in shown
