"""A ledger watch on a remote host, such as a hive worker, reads the ledger in process over the shared database as every
other non browser read does, so it fetches no agent token and sends the hive member's Redis login no command its ACL
refuses."""

import sys
import types
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch

import pytest

from scripts.hive import auth
from scripts.swarm.keyspace import ROOT
from scripts.swarm_ledger import storage, watch_ledger

NAME = "engineer@323133-0901"
CONTENT = {
    "title": "Hive watch",
    "overview": "o",
    "phases": [{"title": "Watch", "description": "d"}, {"title": "Read", "description": "d"}],
}
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


def comment(slug: str, comment_id: str, text: str, phase: str = "p1") -> None:
    """An operator comment on one of the ledger's phases, written to the shared ledger store; a later comment by the
    same author on the same phase amends this one, so each new event goes on a phase of its own."""
    _, rejected = storage.default().apply_ops(
        slug,
        ops=[{"op": "add", "id": comment_id, "by": "operator", "thread": f"phases/{phase}/comments", "text": text}],
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

    monkeypatch.setattr(watch_ledger, "time", types.SimpleNamespace(sleep=pause))
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

    out, pauses = watched(
        monkeypatch, capsys, hive["slug"], lambda: comment(hive["slug"], "c-second", "second word", "p2")
    )

    assert (out.count("[c-first]"), out.count("[c-second]")) == (1, 1), (pauses, out)
    assert out.index("[c-first]") < out.index("[c-second]")


@pytest.mark.feature("hive-watch-in-process")
def test_a_remote_watch_takes_no_credentials_and_a_local_watch_follows_the_ledger_server_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[str] = []
    polls: list[str] = []
    opened: list[tuple[str, float]] = []

    class Refused(Exception):
        """The ledger server refusing a stream, so the test sees the request without a server."""

    def fetch(slug: str) -> dict[str, str]:
        fetched.append(slug)
        return {"X-Ledger-Token": f"for {slug}"}

    def polled(slug: str) -> Iterator[tuple[str, Any, None]]:
        polls.append(slug)
        yield "snapshot", {"ledger": {"slug": slug}}, None

    def urlopen(request: Any, timeout: float) -> Any:
        opened.append((request.full_url, timeout))
        raise Refused

    monkeypatch.setitem(sys.modules, "ledger", types.SimpleNamespace(credentials=fetch))
    monkeypatch.setattr(watch_ledger, "polled", polled)
    monkeypatch.setattr(watch_ledger.ledger_link, "remote", lambda environ=None, slug="": slug == "far")
    monkeypatch.setattr(watch_ledger.ledger_link, "base", lambda: "http://h:1")
    monkeypatch.setattr(watch_ledger.ledger_link, "urlopen", urlopen)

    assert watch_ledger.credentials("far") == {}
    assert fetched == []
    assert watch_ledger.credentials("near") == {"X-Ledger-Token": "for near"}
    assert fetched == ["near"]

    assert list(watch_ledger.stream("far", None, {})) == [("snapshot", {"ledger": {"slug": "far"}}, None)]
    assert polls == ["far"]
    assert opened == []

    with pytest.raises(Refused):
        next(watch_ledger.stream("near", "c2", {"X-Ledger-Token": "kept"}))
    assert opened == [("http://h:1/api/v1/ledgers/near/events", watch_ledger.STREAM_TIMEOUT_S)]
    assert polls == ["far"]
    assert fetched == ["near"]


@pytest.mark.feature("hive-watch-in-process")
def test_a_remote_poll_reads_the_ledger_in_process_with_the_kept_exports(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.tick.adapters import database_ledger

    built: list[tuple[str, float]] = []
    reads: list[tuple[str, dict[Any, Any]]] = []

    class Client:
        def export(self, slug: str, kept: dict[Any, Any]) -> dict[str, Any]:
            reads.append((slug, kept))
            return {"read": len(reads)}

    def client(base: str, timeout: float) -> Client:
        built.append((base, timeout))
        return Client()

    monkeypatch.setattr(database_ledger, "client", client)

    assert list(watch_ledger.polled("far")) == [("snapshot", {"ledger": {"read": 1}}, None)]
    assert list(watch_ledger.polled("far")) == [("snapshot", {"ledger": {"read": 2}}, None)]
    assert built == [(watch_ledger.IN_PROCESS, watch_ledger.STREAM_TIMEOUT_S)] * 2
    assert reads == [("far", watch_ledger.EXPORTS)] * 2
    assert all(kept is watch_ledger.EXPORTS for _, kept in reads)
