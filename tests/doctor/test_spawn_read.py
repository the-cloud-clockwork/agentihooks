import subprocess
from dataclasses import asdict

import pytest

from scripts.doctor import spawn_read, spawns
from tests.doctor.recorded import load

pytestmark = pytest.mark.xdist_group("fakeredis")


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
        assert argv[:6] == ["journalctl", "--user", "-u", "agentihooks-swarm.service", "--since", "1 hour ago"]
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
            "-u",
            "agentihooks-swarm.service",
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
