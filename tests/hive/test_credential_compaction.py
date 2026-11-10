import hashlib
from typing import Any

import pytest

from scripts.hive import auth, hive_state


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "on")
    monkeypatch.setattr(auth, "_LEDGER_OF", {})
    redis = fakeredis.FakeRedis(decode_responses=True)
    store = hive_state.credentials(redis, auth.PREFIX)
    hive_state.amend(store, "member.m1", lambda _: {"name": "hive", "ledger": "d", "agents": {}})
    hive_state.amend(store, "ledger.d", lambda _: "m1")
    hive_state.amend(store, "holder.m1", lambda _: "d")
    hive_state.indexed(redis, "m1", True)
    return redis


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.mark.feature("hive-credential-compaction")
def test_scoped_credential_keys_preserve_existing_record_addresses() -> None:
    assert hive_state.agent_key("m1", "alpha", "engineer@1") == (
        "agent.m1.d540e79213ffc4316c706b9576c85f31dad274814e278a86a4d8392eeb0362f5"
    )


@pytest.mark.feature("hive-credential-compaction")
def test_compaction_preserves_legacy_tokens_and_removes_member_maps(client: Any) -> None:
    from scripts.hive import agent_credentials

    store = hive_state.credentials(client, auth.PREFIX)
    token = "hive.m1.legacy"
    held = _digest(token)
    hive_state.amend(
        store, "member.m1", lambda _: {"name": "hive", "ledger": "d", "agents": {held: ["alpha", "engineer@1"]}}
    )
    agent_credentials.compact(client, ["m1"], readers_compatible=True)

    assert auth.member(client, "m1") == {"name": "hive", "ledger": "d"}
    assert auth.agent_member(client, token, "alpha", "engineer@1") == "m1"
    assert auth.agent_member(client, token, "other", "engineer@1") is None
    assert hive_state.read(store, f"agent.m1.{held}") == {"ledger": "d", "scope": ["alpha", "engineer@1"]}
    assert agent_credentials.compact(client, ["m1"], readers_compatible=True) == {"m1": 31}


@pytest.mark.feature("hive-credential-compaction")
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
    assert (
        hive_state.read(hive_state.credentials(client, auth.PREFIX), hive_state.agent_key("m1", "alpha", name)) is None
    )
    assert store.execution("alpha", agent.execution_id).state == "ended"
    assert store.drop_agent("alpha", name)


@pytest.mark.feature("hive-credential-compaction")
def test_recording_an_ended_execution_removes_its_token(client: Any) -> None:
    from dataclasses import replace

    import scripts.swarm.execution as execution
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(client)
    store.create(SwarmConfig("alpha", "/repo", 1, 0))
    name = store.next_name("alpha", "eng")
    agent = store.start_execution("alpha", AgentRecord(name, "eng", "task", seat="eng-1@alpha", hive="m1"))
    token = auth.issue_agent(client, "m1", "alpha", name)

    execution.ExecutionRegistry(store).update("alpha", replace(agent, state="ended"))

    assert auth.agent_member(client, token, "alpha", name) is None
    assert (
        hive_state.read(hive_state.credentials(client, auth.PREFIX), hive_state.agent_key("m1", "alpha", name)) is None
    )


@pytest.mark.feature("hive-credential-compaction")
def test_ending_an_orphan_execution_removes_its_token(client: Any) -> None:
    import scripts.swarm.execution as execution
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(client)
    store.create(SwarmConfig("alpha", "/repo", 1, 0))
    name = store.next_name("alpha", "eng")
    agent = store.start_execution("alpha", AgentRecord(name, "eng", "task", seat="eng-1@alpha", hive="m1"))
    token = auth.issue_agent(client, "m1", "alpha", name)
    client.hdel(store.key("alpha", "agents"), name)

    assert execution.ExecutionRegistry(store).end_orphan("alpha", agent, 100)
    assert auth.agent_member(client, token, "alpha", name) is None
    assert store.execution("alpha", agent.execution_id).state == "ended"


@pytest.mark.feature("hive-credential-compaction")
def test_retiring_a_compact_member_never_scans_the_credential_store(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import agent_credentials

    credentials = hive_state.credentials(client, auth.PREFIX)
    hive_state.amend(credentials, "member.m1", lambda _: {"name": "hive", "ledger": "d"})
    token = auth.issue_agent(client, "m1", "alpha", "engineer@1")

    def refused_scan(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("compact member retirement scanned the credential store")

    monkeypatch.setattr(client, "hscan_iter", refused_scan)
    agent_credentials.retire(client, "m1", "alpha", "engineer@1")

    assert auth.agent_member(client, token, "alpha", "engineer@1") is None


@pytest.mark.feature("hive-credential-compaction")
def test_retirement_indexes_uncompacted_independent_tokens(client: Any) -> None:
    from scripts.hive import agent_credentials

    credentials = hive_state.credentials(client, auth.PREFIX)
    token = "hive.m1.unindexed"
    key = f"agent.m1.{_digest(token)}"
    hive_state.amend(credentials, key, lambda _: {"ledger": "d", "scope": ["alpha", "engineer@1"]})

    agent_credentials.retire(client, "m1", "alpha", "engineer@1")

    assert hive_state.read(credentials, key) is None
    assert auth.agent_member(client, token, "alpha", "engineer@1") is None


@pytest.mark.feature("hive-credential-compaction")
def test_legacy_indexes_keep_separate_scopes_and_exclude_scoped_records(client: Any) -> None:
    from scripts.hive import agent_credentials

    credentials = hive_state.credentials(client, auth.PREFIX)
    tokens = [
        ("hive.m1.first", "alpha", "engineer@1"),
        ("hive.m1.peer", "alpha", "engineer@peer"),
        ("hive.m1.other", "beta", "engineer@1"),
    ]
    for token, slug, name in tokens:
        hive_state.amend(credentials, f"agent.m1.{_digest(token)}", lambda _: {"ledger": "d", "scope": [slug, name]})
    scoped = auth.issue_agent(client, "m1", "alpha", "engineer@1")
    agent_credentials.compact(client, ["m1"], readers_compatible=True)
    index = "agent-index.m1.d540e79213ffc4316c706b9576c85f31dad274814e278a86a4d8392eeb0362f5"

    assert hive_state.read(credentials, index) == [f"agent.m1.{_digest(tokens[0][0])}"]
    agent_credentials.retire(client, "m1", "alpha", "engineer@1")

    assert hive_state.read(credentials, index) is None
    assert auth.agent_member(client, scoped, "alpha", "engineer@1") is None
    assert auth.agent_member(client, tokens[0][0], "alpha", "engineer@1") is None
    assert auth.agent_member(client, tokens[1][0], "alpha", "engineer@peer") == "m1"
    assert auth.agent_member(client, tokens[2][0], "beta", "engineer@1") == "m1"


@pytest.mark.feature("hive-credential-compaction")
def test_legacy_indexing_bounds_reads_and_skips_tombstones(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Iterator

    from scripts.hive import agent_credentials

    credentials = hive_state.credentials(client, auth.PREFIX)
    tokens = [f"hive.m1.batch-{number}" for number in range(129)]
    for token in tokens:
        hive_state.amend(
            credentials, f"agent.m1.{_digest(token)}", lambda _: {"ledger": "d", "scope": ["alpha", "engineer@1"]}
        )
    empty = "agent.m1.empty"
    hive_state.amend(credentials, empty, lambda _: {"ledger": "d", "scope": ["alpha", "engineer@1"]})
    hive_state.amend(credentials, empty, lambda _: None)
    original_scan, original_many = client.hscan_iter, client.hmget
    batches: list[int] = []

    def scan(name: str, **kwargs: Any) -> Iterator[tuple[str, str]]:
        assert kwargs.get("count") is not None and 0 < kwargs["count"] <= 128
        rows: list[tuple[str, str]] = list(original_scan(name, **kwargs))
        rows.sort(key=lambda row: row[0] != empty)
        return iter(rows)

    def many(name: str, keys: list[str], *args: Any) -> Any:
        assert 0 < len(keys) <= 128
        batches.append(len(keys))
        return original_many(name, keys, *args)

    monkeypatch.setattr(client, "hscan_iter", scan)
    monkeypatch.setattr(client, "hmget", many)
    agent_credentials.compact(client, ["m1"], readers_compatible=True)
    assert len(batches) >= 2
    agent_credentials.retire(client, "m1", "alpha", "engineer@1")
    assert all(auth.agent_member(client, token, "alpha", "engineer@1") is None for token in tokens)


@pytest.mark.feature("hive-credential-compaction")
def test_retiring_a_legacy_projection_removes_its_hive_token(client: Any) -> None:
    import json
    from dataclasses import asdict

    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(client)
    store.create(SwarmConfig("alpha", "/repo", 1, 0))
    name = store.next_name("alpha", "eng")
    client.hset(store.key("alpha", "agents"), name, json.dumps(asdict(AgentRecord(name, "eng", "task", hive="m1"))))
    token = auth.issue_agent(client, "m1", "alpha", name)

    assert store.drop_agent("alpha", name)
    assert auth.agent_member(client, token, "alpha", name) is None


@pytest.mark.feature("hive-credential-compaction")
def test_named_retirement_covers_recorded_and_previous_hives_only(client: Any) -> None:
    import json

    from scripts.hive import agent_credentials
    from scripts.swarm.store import AgentRecord

    credentials = hive_state.credentials(client, auth.PREFIX)
    for member_id in ("m2", "m3", "m4"):
        digest = f"d-{member_id}"
        hive_state.amend(credentials, f"member.{member_id}", lambda _: {"name": member_id, "ledger": digest})
        hive_state.amend(credentials, f"ledger.{digest}", lambda _: member_id)
        hive_state.amend(credentials, f"holder.{member_id}", lambda _: digest)
    tokens = {
        member_id: auth.issue_agent(client, member_id, "alpha", "engineer@1") for member_id in ("m1", "m2", "m3", "m4")
    }
    peer = auth.issue_agent(client, "m1", "alpha", "engineer@peer")
    lives = [
        AgentRecord("engineer@1", "eng", "task", hive="m1"),
        AgentRecord("engineer@1", "eng", "task", hive="m2"),
        AgentRecord("engineer@peer", "eng", "peer", hive="m3"),
        AgentRecord("engineer@1", "eng", "task"),
    ]

    agent_credentials.retire_named(client, "alpha", "engineer@1", json.dumps({"hive": "m4"}), lives)

    assert all(
        auth.agent_member(client, tokens[member_id], "alpha", "engineer@1") is None for member_id in ("m1", "m2", "m4")
    )
    assert auth.agent_member(client, tokens["m3"], "alpha", "engineer@1") == "m3"
    assert auth.agent_member(client, peer, "alpha", "engineer@peer") == "m1"


@pytest.mark.feature("hive-credential-compaction")
def test_compacted_independent_tokens_are_removed_when_the_agent_ends(client: Any) -> None:
    from scripts.hive import agent_credentials
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(client)
    store.create(SwarmConfig("alpha", "/repo", 1, 0))
    name = store.next_name("alpha", "eng")
    store.start_execution("alpha", AgentRecord(name, "eng", "task", seat="eng-1@alpha", hive="m1"))
    credentials = hive_state.credentials(client, auth.PREFIX)
    token = "hive.m1.older"
    key = f"agent.m1.{_digest(token)}"
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


@pytest.mark.feature("hive-credential-compaction")
def test_ending_an_agent_removes_retained_map_tokens_and_keeps_active_peers(client: Any) -> None:
    from scripts.hive import agent_credentials

    credentials = hive_state.credentials(client, auth.PREFIX)
    token = "hive.m1.retained"
    peer = "hive.m1.retained-peer"
    hive_state.amend(
        credentials,
        "member.m1",
        lambda _: {
            "name": "hive",
            "ledger": "d",
            "agents": {
                _digest(token): ["alpha", "engineer@1"],
                _digest(peer): ["alpha", "engineer@peer"],
            },
        },
    )

    agent_credentials.retire(client, "m1", "alpha", "engineer@1")

    assert auth.agent_member(client, token, "alpha", "engineer@1") is None
    assert auth.agent_member(client, peer, "alpha", "engineer@peer") == "m1"
    assert auth.member(client, "m1") == {
        "name": "hive",
        "ledger": "d",
        "agents": {_digest(peer): ["alpha", "engineer@peer"]},
    }
    agent_credentials.retire(client, "m1", "alpha", "engineer@1")
    assert auth.agent_member(client, peer, "alpha", "engineer@peer") == "m1"


@pytest.mark.feature("hive-credential-compaction")
def test_compaction_and_retirement_share_a_fence(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Generator
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event

    from scripts.hive import agent_credentials

    token = "hive.m1.interleaved"
    hive_state.amend(
        hive_state.credentials(client, auth.PREFIX),
        "member.m1",
        lambda _: {"name": "hive", "ledger": "d", "agents": {_digest(token): ["alpha", "engineer@1"]}},
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
    def observed_lock(name: str, **kwargs: Any) -> Generator[None]:
        assert kwargs.get("timeout") is not None and 0 < kwargs["timeout"] <= 600
        assert kwargs.get("blocking_timeout") is not None and 0 < kwargs["blocking_timeout"] <= 10
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


@pytest.mark.feature("hive-credential-compaction")
def test_minted_members_omit_the_obsolete_agents_map(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_set_user", lambda *_: None)
    monkeypatch.setattr(auth.redis_acl, "save", lambda *_: None)

    grant = auth.mint(client, "new hive", "redis://example/0")

    assert auth.member(client, grant["id"]) == {"name": "new hive", "ledger": _digest(grant["ledger_credential"])}


@pytest.mark.parametrize(("members", "verified"), [(["m1"], False), ([], True)])
@pytest.mark.feature("hive-credential-compaction")
def test_compaction_requires_explicit_members_and_verified_readers(
    client: Any, members: list[str], verified: bool
) -> None:
    from scripts.hive import agent_credentials

    with pytest.raises(auth.HiveError) as raised:
        agent_credentials.compact(client, members, readers_compatible=verified)
    assert str(raised.value) == "compaction requires verified readers and an explicit member list"
    assert auth.member(client, "m1") == {"name": "hive", "ledger": "d", "agents": {}}


@pytest.mark.feature("hive-credential-compaction")
def test_compaction_defaults_to_unverified_readers(client: Any) -> None:
    from scripts.hive import agent_credentials

    with pytest.raises(auth.HiveError) as raised:
        agent_credentials.compact(client, ["m1"])
    assert str(raised.value) == "compaction requires verified readers and an explicit member list"


@pytest.mark.feature("hive-credential-compaction")
def test_compaction_refuses_other_backends(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import agent_credentials

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    with pytest.raises(auth.HiveError) as raised:
        agent_credentials.compact(client, ["m1"], readers_compatible=True)
    assert str(raised.value) == "credential compaction requires the Redis state backend"


@pytest.mark.feature("hive-credential-compaction")
def test_compaction_refuses_unknown_members(client: Any) -> None:
    from scripts.hive import agent_credentials

    with pytest.raises(auth.HiveError) as raised:
        agent_credentials.compact(client, ["missing"], readers_compatible=True)
    assert str(raised.value) == "unknown hive member missing"


@pytest.mark.feature("hive-credential-compaction")
def test_cli_compacts_only_the_selected_members(
    client: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from scripts.hive import cli

    monkeypatch.setattr(cli, "redis_client", lambda: client)
    hive_state.amend(
        hive_state.credentials(client, auth.PREFIX),
        "member.m2",
        lambda _: {"name": "peer", "ledger": "peer", "agents": {}},
    )

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


@pytest.mark.feature("hive-credential-compaction")
def test_compaction_replaces_large_history_and_preserves_existing_independent_records(client: Any) -> None:
    import base64
    import json

    from scripts.hive import agent_credentials

    store = hive_state.credentials(client, auth.PREFIX)
    held = _digest("hive.m1.existing")
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


@pytest.mark.feature("hive-credential-compaction")
def test_cli_explains_the_reader_precondition(capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.hive import cli

    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])
    assert raised.value.code == 0
    assert "compact-credentials Compact member documents after verifying deployed readers refresh" in (
        " ".join(capsys.readouterr().out.split())
    )
    with pytest.raises(SystemExit) as raised:
        cli.main(["compact-credentials", "--help"])
    assert raised.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert help_text.rsplit("options:", 1)[1].strip() == (
        "-h, --help show this help message and exit --member MEMBER member whose deployed readers were verified "
        "--readers-compatible confirm independent token readers are deployed"
    )
