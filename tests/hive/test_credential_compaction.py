from typing import Any

import pytest

from scripts.hive import auth, hive_state

pytestmark = pytest.mark.feature("hive-credential-compaction")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "on")
    monkeypatch.setattr(auth, "_LEDGER_OF", {})
    redis = fakeredis.FakeRedis(decode_responses=True)
    store = auth._store(redis)
    hive_state.amend(store, "member.m1", lambda _: {"name": "hive", "ledger": "d", "agents": {}})
    hive_state.amend(store, "ledger.d", lambda _: "m1")
    hive_state.amend(store, "holder.m1", lambda _: "d")
    hive_state.indexed(redis, "m1", True)
    return redis


def test_compaction_preserves_legacy_tokens_and_removes_member_maps(client: Any) -> None:
    from scripts.hive import agent_credentials

    store = auth._store(client)
    token = "hive.m1.legacy"
    held = auth._digest(token)
    hive_state.amend(
        store, "member.m1", lambda _: {"name": "hive", "ledger": "d", "agents": {held: ["alpha", "engineer@1"]}}
    )
    agent_credentials.compact(client, ["m1"], readers_compatible=True)

    assert auth.member(client, "m1") == {"name": "hive", "ledger": "d"}
    assert auth.agent_member(client, token, "alpha", "engineer@1") == "m1"
    assert auth.agent_member(client, token, "other", "engineer@1") is None
    assert hive_state.read(store, f"agent.m1.{held}") == {"ledger": "d", "scope": ["alpha", "engineer@1"]}
    assert agent_credentials.compact(client, ["m1"], readers_compatible=True) == {"m1": 31}


def test_ended_agent_tokens_are_removed_and_active_peers_keep_access(client: Any) -> None:
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(client)
    store.create(SwarmConfig("alpha", "/repo", 2, 0))
    name = store.next_name("alpha", "eng")
    agent = store.start_execution("alpha", AgentRecord(name, "eng", "task", seat="eng-1@alpha", hive="m1"))
    token = auth.issue_agent(client, "m1", "alpha", name)
    peer = auth.issue_agent(client, "m1", "alpha", "engineer@peer")

    assert store.drop_agent("alpha", name)
    assert auth.agent_member(client, token, "alpha", name) is None
    assert auth.agent_member(client, peer, "alpha", "engineer@peer") == "m1"
    assert hive_state.read(auth._store(client), auth._agent_key("m1", "alpha", name)) is None
    assert store.execution("alpha", agent.execution_id).state == "ended"
    assert store.drop_agent("alpha", name)


def test_recording_an_ended_execution_removes_its_token(client: Any) -> None:
    from dataclasses import replace

    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(client)
    store.create(SwarmConfig("alpha", "/repo", 1, 0))
    name = store.next_name("alpha", "eng")
    agent = store.start_execution("alpha", AgentRecord(name, "eng", "task", seat="eng-1@alpha", hive="m1"))
    token = auth.issue_agent(client, "m1", "alpha", name)

    store.put_agent("alpha", replace(agent, state="ended"))

    assert auth.agent_member(client, token, "alpha", name) is None
    assert hive_state.read(auth._store(client), auth._agent_key("m1", "alpha", name)) is None


def test_compacted_independent_tokens_are_removed_when_the_agent_ends(client: Any) -> None:
    from scripts.hive import agent_credentials
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(client)
    store.create(SwarmConfig("alpha", "/repo", 1, 0))
    name = store.next_name("alpha", "eng")
    store.start_execution("alpha", AgentRecord(name, "eng", "task", seat="eng-1@alpha", hive="m1"))
    credentials = auth._store(client)
    token = "hive.m1.older"
    key = f"agent.m1.{auth._digest(token)}"
    hive_state.amend(credentials, key, lambda _: {"ledger": "d", "scope": ["alpha", name]})
    scoped = auth.issue_agent(client, "m1", "alpha", name)
    peer = auth.issue_agent(client, "m1", "alpha", "engineer@peer")
    agent_credentials.compact(client, ["m1"], readers_compatible=True)

    assert auth.agent_member(client, token, "alpha", name) == "m1"
    assert store.drop_agent("alpha", name)
    assert hive_state.read(credentials, key) is None
    assert auth.agent_member(client, token, "alpha", name) is None
    assert auth.agent_member(client, scoped, "alpha", name) is None
    assert auth.agent_member(client, peer, "alpha", "engineer@peer") == "m1"


def test_ending_an_agent_removes_retained_map_tokens_and_keeps_active_peers(client: Any) -> None:
    from scripts.hive import agent_credentials

    credentials = auth._store(client)
    token = "hive.m1.retained"
    peer = "hive.m1.retained-peer"
    hive_state.amend(
        credentials,
        "member.m1",
        lambda _: {
            "name": "hive",
            "ledger": "d",
            "agents": {
                auth._digest(token): ["alpha", "engineer@1"],
                auth._digest(peer): ["alpha", "engineer@peer"],
            },
        },
    )

    agent_credentials.retire(client, "m1", "alpha", "engineer@1")

    assert auth.agent_member(client, token, "alpha", "engineer@1") is None
    assert auth.agent_member(client, peer, "alpha", "engineer@peer") == "m1"
    assert auth.member(client, "m1") == {
        "name": "hive",
        "ledger": "d",
        "agents": {auth._digest(peer): ["alpha", "engineer@peer"]},
    }
    agent_credentials.retire(client, "m1", "alpha", "engineer@1")
    assert auth.agent_member(client, peer, "alpha", "engineer@peer") == "m1"


def test_compaction_and_retirement_share_a_fence(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Iterator
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event

    from scripts.hive import agent_credentials

    token = "hive.m1.interleaved"
    hive_state.amend(
        auth._store(client),
        "member.m1",
        lambda _: {"name": "hive", "ledger": "d", "agents": {auth._digest(token): ["alpha", "engineer@1"]}},
    )
    snapshot, release, retirement_attempted = Event(), Event(), Event()
    original_member, original_lock = auth.member, client.lock

    def read_member(redis: Any, member_id: str) -> Any:
        found = original_member(redis, member_id)
        if not snapshot.is_set():
            snapshot.set()
            assert release.wait(5)
        return found

    @contextmanager
    def observed_lock(name: str, **kwargs: Any) -> Iterator[None]:
        if snapshot.is_set():
            retirement_attempted.set()
        with original_lock(name, **kwargs):
            yield

    monkeypatch.setattr(auth, "member", read_member)
    monkeypatch.setattr(client, "lock", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        compacting = pool.submit(agent_credentials.compact, client, ["m1"], True)
        assert snapshot.wait(5)
        retiring = pool.submit(agent_credentials.retire, client, "m1", "alpha", "engineer@1")
        try:
            assert retirement_attempted.wait(5)
        finally:
            release.set()
        assert compacting.result(timeout=5) == {"m1": 31}
        assert retiring.result(timeout=5) is None
    assert auth.agent_member(client, token, "alpha", "engineer@1") is None


def test_minted_members_omit_the_obsolete_agents_map(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_set_user", lambda *_: None)
    monkeypatch.setattr(auth.redis_acl, "save", lambda *_: None)

    grant = auth.mint(client, "new hive", "redis://example/0")

    assert auth.member(client, grant["id"]) == {"name": "new hive", "ledger": auth._digest(grant["ledger_credential"])}


@pytest.mark.parametrize(("members", "verified"), [(["m1"], False), ([], True)])
def test_compaction_requires_explicit_members_and_verified_readers(
    client: Any, members: list[str], verified: bool
) -> None:
    from scripts.hive import agent_credentials

    with pytest.raises(auth.HiveError) as raised:
        agent_credentials.compact(client, members, readers_compatible=verified)
    assert str(raised.value) == "compaction requires verified readers and an explicit member list"
    assert auth.member(client, "m1") == {"name": "hive", "ledger": "d", "agents": {}}


def test_compaction_defaults_to_unverified_readers(client: Any) -> None:
    from scripts.hive import agent_credentials

    with pytest.raises(auth.HiveError) as raised:
        agent_credentials.compact(client, ["m1"])
    assert str(raised.value) == "compaction requires verified readers and an explicit member list"


def test_compaction_refuses_other_backends(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import agent_credentials

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "nats")
    with pytest.raises(auth.HiveError) as raised:
        agent_credentials.compact(client, ["m1"], readers_compatible=True)
    assert str(raised.value) == "credential compaction requires the Redis state backend"


def test_compaction_refuses_unknown_members(client: Any) -> None:
    from scripts.hive import agent_credentials

    with pytest.raises(auth.HiveError) as raised:
        agent_credentials.compact(client, ["missing"], readers_compatible=True)
    assert str(raised.value) == "unknown hive member missing"


def test_cli_compacts_only_the_selected_members(
    client: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from scripts.hive import cli

    monkeypatch.setattr(cli, "redis_client", lambda: client)
    hive_state.amend(auth._store(client), "member.m2", lambda _: {"name": "peer", "ledger": "peer", "agents": {}})

    assert cli.main(["compact-credentials", "--member", "m1", "--readers-compatible"]) == 0
    assert json.loads(capsys.readouterr().out) == {"m1": 31}
    assert auth.member(client, "m2") == {"name": "peer", "ledger": "peer", "agents": {}}
    assert cli.main(["compact-credentials", "--member", "m2"]) == 1
    assert capsys.readouterr().err == (
        "hive compact-credentials refused: compaction requires verified readers and an explicit member list\n"
    )
    assert cli.main(["compact-credentials", "--member", "m1", "--member", "m2", "--readers-compatible"]) == 0
    assert json.loads(capsys.readouterr().out) == {"m1": 31, "m2": 34}
    with pytest.raises(SystemExit) as raised:
        cli.main(["compact-credentials", "--readers-compatible"])
    assert raised.value.code == 2


def test_compaction_replaces_large_history_and_preserves_existing_independent_records(client: Any) -> None:
    import base64
    import json

    from scripts.hive import agent_credentials

    store = auth._store(client)
    held = auth._digest("hive.m1.existing")
    record = {"ledger": "d", "scope": ["alpha", "engineer@peer"]}
    hive_state.amend(store, f"agent.m1.{held}", lambda _: record)
    hive_state.amend(
        store, "member.m1", lambda _: {"name": "hive", "ledger": "d", "agents": {held: ["alpha", "engineer@1"]}}
    )

    assert agent_credentials.compact(client, ["m1"], readers_compatible=True) == {"m1": 31}
    assert hive_state.read(store, f"agent.m1.{held}") == record
    history = client.lrange(f"{auth.PREFIX}:state:credentials:history:member.m1", 0, -1)
    assert len(history) == 2
    assert [json.loads(base64.b64decode(json.loads(raw)["value"])) for raw in history] == [
        {"name": "hive", "ledger": "d"},
        {"name": "hive", "ledger": "d"},
    ]


def test_cli_explains_the_reader_precondition(capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.hive import cli

    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    assert "Compact member documents after verifying deployed readers" in capsys.readouterr().out
    with pytest.raises(SystemExit) as raised:
        cli.main(["compact-credentials", "--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "member whose deployed readers were verified" in help_text
    assert "confirm independent token readers are deployed" in help_text
