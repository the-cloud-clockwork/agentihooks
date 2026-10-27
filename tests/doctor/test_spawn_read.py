import subprocess
from dataclasses import asdict

import pytest

from scripts.doctor import spawn_read, spawns
from tests.doctor.recorded import load


def test_spawn_reader_is_read_only_and_uses_the_swarm_target(monkeypatch):
    import fakeredis

    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    fixture = load("spawns")
    slug = fixture["slug"]
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(slug, "/repo", 1, 0))
    for harness, count in fixture["spawns"].items():
        for _ in range(count):
            store.count_spawn(slug, harness)
    agent = AgentRecord("agent", "eng", "dt2", account="a", harness="codex", placement="overflow")
    store.put_agent(slug, agent)
    before = store.export(slug)

    def journal(argv, **kwargs):
        assert argv[:7] == [
            "journalctl",
            "--user",
            "_SYSTEMD_USER_UNIT=agentihooks-swarm.service",
            "+",
            "SYSLOG_IDENTIFIER=agentihooks-swarm",
            "--since",
            "1 hour ago",
        ]
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(
            argv,
            0,
            f"{slug}: spawn failed for dt2, task dt2 reopened: timeout\nother: master spawn failed: unavailable\n",
            "",
        )

    for at, name in enumerate(("first", "second", "ended"), start=7):
        store.put_agent(slug, AgentRecord(name, "eng", "dt1", harness="claude", choice="share"))
        store.drop_agent(slug, name, at=at)
    store.put_agent(slug, agent)
    before = store.export(slug)
    record = spawn_read.records(store, slug, 5, run=journal)
    assert record["now"] == 5
    assert [(row["name"], row["choice"], row["ended_at"]) for row in record["history"]] == [
        ("first", "share", 7),
        ("second", "share", 8),
        ("ended", "share", 9),
    ]
    assert record["agents"] == [asdict(agent)]
    assert record["spawns"] == {"codex": 3, "claude": 5}
    assert record["restored"] == []
    assert {f.kind for f in spawns.findings(record)} == {"failed spawn"}
    assert spawns.failed(record)[0].subject == "dt2"
    assert store.export(slug) == before


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError(2, "No such file or directory", "journalctl"), subprocess.CalledProcessError(1, "journalctl")],
)
def test_spawn_reader_refuses_when_the_tick_journal_cannot_be_read(failure):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "/repo", 1, 0))

    def journal(argv, **kwargs):
        raise failure

    with pytest.raises(SwarmError, match=f"^tick journal unavailable: {type(failure).__name__}: "):
        spawn_read.records(store, "fixture", 5, run=journal)


def test_spawn_reader_passes_an_explicit_upper_journal_bound(monkeypatch):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    before = store.export("sw")

    def journal(argv, **kwargs):
        assert argv == [
            "journalctl",
            "--user",
            "_SYSTEMD_USER_UNIT=agentihooks-swarm.service",
            "+",
            "SYSLOG_IDENTIFIER=agentihooks-swarm",
            "--since",
            "2026-10-07T09:58:24Z",
            "--until",
            "2026-10-07T12:58:24Z",
            "-o",
            "cat",
            "--no-pager",
        ]
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 30}
        return subprocess.CompletedProcess(argv, 0, "sw: spawn failed for mu1, task mu1 reopened: timeout\n", "")

    record = spawn_read.records(
        store,
        "sw",
        5,
        since="2026-10-07T09:58:24Z",
        until="2026-10-07T12:58:24Z",
        run=journal,
    )
    assert record["actions"] == ["sw: spawn failed for mu1, task mu1 reopened: timeout"]
    assert spawns.failed(record)[0].measure == 1
    assert store.export("sw") == before


def test_spawn_reader_keeps_capacity_beside_its_refusal():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    capacity = {
        "harness": "claude",
        "accounts": [
            {"harness": "claude", "state": "OPEN", "sessions": 6, "cap": 6},
        ],
    }
    import json

    context = f"sw: spawn capacity for mu1: {json.dumps(capacity)}"
    refusal = "sw: spawn failed for mu1: account reservation is full"

    def journal(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            "\n".join(
                [
                    "other: spawn capacity for mu1: {}",
                    context,
                    refusal,
                    "sw: unrelated",
                ]
            ),
            "",
        )

    record = spawn_read.records(store, "sw", 5, run=journal)
    assert record["actions"] == [context, refusal]
    assert spawns.failed(record) == []
