"""A ledger watch on a remote host, such as a hive worker, reads the ledger in process over the shared database as every
other non browser read does, so it fetches no agent token and sends the hive member's Redis login no command its ACL
refuses."""

import sys
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch

import pytest

from scripts.hive import auth
from scripts.swarm.keyspace import ROOT
from scripts.swarm_ledger import storage, watch_ledger

NAME = "engineer@323133-0901"
CONTENT = {"title": "Hive watch", "overview": "o", "phases": [{"title": "Watch", "description": "d"}]}
READS = frozenset({"GET", "HGET", "HMGET", "HGETALL", "HEXISTS", "EXISTS", "SMEMBERS", "SISMEMBER", "MGET", "TTL"})
CREDENTIAL_STORE = f"{auth.PREFIX}:state:credentials:current"


class Stopped(Exception):
    """Ends the watch from its pause, since a watch runs until it is stopped."""


def member_login(server: Any, admin: Any, member_id: str) -> Any:
    """A Redis login holding the hive member grant; fakeredis drops %R~ selectors, so reads of the swarm keyspace
    the grant allows go through the admin login and every other command meets the member ACL."""
    import fakeredis

    class Member(fakeredis.FakeRedis):
        def execute_command(self, *args: Any, **options: Any) -> Any:
            if str(args[0]).upper() in READS and len(args) > 1 and str(args[1]).startswith(f"{ROOT}:"):
                return admin.execute_command(*args, **options)
            return super().execute_command(*args, **options)

    user = f"hive-{member_id}"
    admin.acl_setuser(
        user,
        enabled=True,
        nopass=True,
        reset=True,
        categories=auth.ACL_CATEGORIES,
        keys=auth._acl_keys(member_id),
        channels=auth.ACL_PATTERNS,
    )
    login = Member(server=server, decode_responses=True, username=user)
    login.auth("", username=user)
    return login


def comment(slug: str, comment_id: str, text: str) -> None:
    """An operator comment on the ledger's first phase, written to the shared ledger store."""
    _, rejected = storage.default().apply_ops(
        slug, ops=[{"op": "add", "id": comment_id, "by": "operator", "thread": "phases/p1/comments", "text": text}]
    )
    assert rejected == []


@pytest.fixture
def hive(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A hive agent with no agent token whose process Redis is the hive member login, on a ledger of its own in the
    shared ledger store, with its ledger server address pointing at no server."""
    import fakeredis

    server = fakeredis.FakeServer()
    admin = fakeredis.FakeRedis(server=server, decode_responses=True)
    grant = auth.mint(admin, "member", "redis://localhost:6379/0")
    member = member_login(server, admin, grant["id"])
    slug = f"hive-watch-{uuid.uuid4().hex[:8]}"
    storage.default().create(slug, CONTENT)
    for name in (
        "AGENTIHOOKS_LEDGER_AGENT_TOKEN",
        "SWARM_SECRET",
        "AGENTIHOOKS_CONTROLLER_CREDENTIAL",
        "AGENTIHOOKS_HOSTED_LEDGER_URL",
        "LEDGER_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", slug)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", NAME)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", grant["ledger_credential"])
    monkeypatch.setenv("SWARM_URL", "http://127.0.0.1:9")
    with (
        patch("hooks._redis.get_redis", return_value=None),
        patch("scripts.swarm.store.redis_client", return_value=member),
    ):
        yield {"slug": slug, "member": member}


def watched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], slug: str, between: Callable[[], None]
) -> tuple[str, list[float]]:
    """What a watch of every event since revision zero prints over two reads, and the pauses it took; between runs
    at the first pause."""
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) > 1:
            raise Stopped
        between()

    monkeypatch.setattr(watch_ledger.time, "sleep", pause)
    monkeypatch.setattr(
        sys, "argv", ["watch_ledger.py", slug, "--as", NAME, "--all", "--since-rev", "0", "--interval", "7"]
    )
    capsys.readouterr()
    with pytest.raises(Stopped):
        watch_ledger.main()
    return capsys.readouterr().out, pauses


@pytest.mark.feature("hive-watch-in-process")
def test_a_hive_watch_prints_operator_events_while_its_member_login_cannot_read_the_credential_store(
    hive: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from redis.exceptions import NoPermissionError

    with pytest.raises(NoPermissionError):
        hive["member"].hget(CREDENTIAL_STORE, "probe")
    comment(hive["slug"], "c-first", "first word")

    out, pauses = watched(monkeypatch, capsys, hive["slug"], lambda: None)

    assert out.startswith(f"WATCHING ledger {hive['slug']} rev ")
    assert out.count("[c-first]") == 1
    assert "OPERATOR rev=" in out and '"first word"' in out
    assert "WARNING ledger stream" not in out
    assert pauses == [7.0, 7.0]


@pytest.mark.feature("hive-watch-in-process")
def test_a_hive_watch_prints_an_event_written_between_its_reads_once(
    hive: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    comment(hive["slug"], "c-first", "first word")

    out, _ = watched(monkeypatch, capsys, hive["slug"], lambda: comment(hive["slug"], "c-second", "second word"))

    assert (out.count("[c-first]"), out.count("[c-second]")) == (1, 1)
    assert out.index("[c-first]") < out.index("[c-second]")


@pytest.mark.feature("hive-watch-in-process")
def test_a_remote_watch_takes_no_credentials_and_a_local_watch_follows_the_ledger_server_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streamed: list[tuple[str, str | None, dict[str, str]]] = []

    def stream(slug: str, cursor: str | None, headers: dict[str, str]) -> Iterator[tuple[str, Any, str | None]]:
        streamed.append((slug, cursor, headers))
        return iter(())

    monkeypatch.setattr(watch_ledger, "stream", stream)
    monkeypatch.setattr(watch_ledger, "credentials", lambda slug: {"X-Ledger-Token": f"for {slug}"})
    monkeypatch.setattr(watch_ledger.ledger_link, "remote", lambda environ=None, slug="": slug == "far")

    _, headers = watch_ledger.follow("far", "c1", None, 7.0, {})
    assert headers is None
    assert streamed == []

    _, headers = watch_ledger.follow("near", "c1", None, 7.0, {})
    assert headers == {"X-Ledger-Token": "for near"}
    assert streamed == [("near", "c1", {"X-Ledger-Token": "for near"})]

    _, held = watch_ledger.follow("near", "c2", {"X-Ledger-Token": "kept"}, 7.0, {})
    assert held == {"X-Ledger-Token": "kept"}
    assert streamed[-1] == ("near", "c2", {"X-Ledger-Token": "kept"})
