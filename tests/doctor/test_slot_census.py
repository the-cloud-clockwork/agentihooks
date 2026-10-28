import json
import subprocess

import pytest

from scripts.doctor import slot_census, spawn_read
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError

AT = 1_793_000_000_000


def line(message, at=AT):
    return {"at": at, "pid": "97698", "message": message}


def life(name, slug="sw", task="t1", started=AT - 60_000, ended=0, **fields):
    return {
        "slug": slug,
        "name": name,
        "task": task,
        "lane": "eng",
        "pane_id": "w5M",
        "runtime_backend": "local",
        "hive": "",
        "started_at": started,
        "ended_at": ended,
        **fields,
    }


def record(claims, lives):
    return {"slug": "sw", "claims": claims, "journal_error": "", "lives": lives}


def test_a_claim_that_counted_every_local_pane_raises_nothing():
    claims = [line("sw: local slot claim new for t9 on workstation counted 2 of 6: engineer@a-1, other/ci@b-2")]
    lives = [life("engineer@a-1"), life("ci@b-2", slug="other")]
    assert slot_census.findings(record(claims, lives)) == []


def test_a_local_pane_left_out_of_the_count_is_named():
    message = "sw: local slot claim new for t9 on workstation counted 1 of 6: engineer@a-1"
    taskless = life("ci@b-3", slug="other", hive="workstation")
    del taskless["task"]
    lives = [life("engineer@a-1"), life("engineer@a-2", task="t2", started=AT), taskless]
    claims = [
        line("other: local slot claim new for t9 on workstation counted 0 of 6: "),
        line("sw: local slot claim full for t9 on workstation counted 3 of 6: engineer@a-1, engineer@a-2, ci@b-3"),
        line(message),
    ]
    assert slot_census.findings(record(claims, lives)) == [
        Finding(
            slot_census.KIND,
            f"new@{AT}",
            "the local slot claim for new left 2 live local agents out: ci@b-3, engineer@a-2",
            (f"journal at {AT} pid 97698: {message}", "ci@b-3 of other on task ", "engineer@a-2 of sw on task t2"),
            "a live local pane missing from a local slot claim's count",
            2,
        )
    ]


def test_a_life_without_start_or_end_times_counts_as_live_from_the_epoch():
    bare = life("engineer@a-2")
    del bare["started_at"], bare["ended_at"]
    message = "sw: local slot claim new for t9 on workstation counted 0 of 6: "
    found = slot_census.findings(record([line(message, at=0), line(message)], [bare]))
    assert [f.subject for f in found] == ["new@0", f"new@{AT}"]


def test_the_census_skips_masters_remote_hives_paneless_ended_and_unstarted_agents_and_the_claimed_task():
    claims = [line("sw: local slot claim new for t9 on workstation counted 0 of 6: ")]
    lives = [
        life("master@a-1", lane="master"),
        life("remote", hive="anton"),
        life("k8s", runtime_backend="kubernetes"),
        life("paneless", pane_id=""),
        life("finished", state="finished"),
        life("ended state", state="ended"),
        life("ended", ended=AT),
        life("ended early", ended=1),
        life("later", started=AT + 1),
        life("predecessor", task="t9"),
    ]
    assert slot_census.findings(record(claims, lives)) == []


def test_only_the_watched_swarm_claims_are_judged():
    claims = [
        line("other: local slot claim new for t9 on workstation counted 0 of 6: "),
        line("sw: local slot claim late for t9 on workstation counted 0 of 6: "),
    ]
    assert [f.subject for f in slot_census.findings(record(claims, [life("engineer@a-1")]))] == [f"late@{AT}"]


def test_an_unreadable_journal_fails_the_detector():
    with pytest.raises(SwarmError, match="journalctl exit 1"):
        slot_census.findings({"slug": "sw", "claims": None, "journal_error": "journalctl exit 1: no", "lives": []})


def test_the_reader_joins_claim_lines_with_current_and_ended_lives_of_every_swarm():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    store.create(SwarmConfig("other", "/repo", 1, 0))
    store.put_agent("sw", AgentRecord("engineer@a-1", "eng", "t1", pane_id="w5M", started_at=AT - 10))
    for n in range(3):
        store.put_agent("other", AgentRecord(f"ci@b-{n}", "ci", "t2", pane_id="w5G", started_at=AT - 20))
        store.drop_agent("other", f"ci@b-{n}", at=AT + 5)
    message = "sw: local slot claim new for t9 on workstation counted 1 of 6: engineer@a-1"
    calls = []

    def journal(argv, **kwargs):
        calls.append(argv)
        assert "-g" in argv and argv[argv.index("-g") + 1] == slot_census.CLAIM
        row = {"__REALTIME_TIMESTAMP": str(AT * 1000), "_PID": "97698", "MESSAGE": message}
        return subprocess.CompletedProcess(argv, 0, json.dumps(row) + "\n", "")

    read = spawn_read.slot_claims(store, "sw", run=journal)
    assert read["claims"] == [line(message)]
    assert read["journal_error"] == ""
    assert calls[0][calls[0].index("--since") + 1] == "1 hour ago"
    assert "--until" not in calls[0]
    lives = {(row["slug"], row["name"]): row for row in read["lives"]}
    assert lives["sw", "engineer@a-1"]["ended_at"] == 0
    assert [lives["other", f"ci@b-{n}"]["ended_at"] for n in range(3)] == [AT + 5] * 3
    [found] = slot_census.findings(read)
    assert "ci@b-0, ci@b-1, ci@b-2" in found.summary
    spawn_read.slot_claims(store, "sw", "2 hours ago", journal, until="now")
    assert calls[1][calls[1].index("--since") + 1] == "2 hours ago"
    assert calls[1][calls[1].index("--until") + 1] == "now"


def test_the_reader_places_ended_lives_on_the_hive_their_execution_ran_on():
    from dataclasses import asdict, replace

    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    for name in ("ci@remote", "ci@local"):
        store.put_agent("sw", AgentRecord(name, "ci", "t2", pane_id="w2:pD", started_at=AT - 20))
    remote = AgentRecord(
        "ci@remote", "ci", "t2", pane_id="w2:pD", started_at=AT - 20, hive="h6", execution_id="exe-1", state="ended"
    )
    store.redis.hset(store.key("sw", "executions-archive"), "exe-1", json.dumps(asdict(remote)))
    store.redis.hset(
        store.key("sw", "executions"),
        "exe-2",
        json.dumps(asdict(replace(remote, started_at=AT - 90, hive="h7", execution_id="exe-2"))),
    )
    for name in ("ci@remote", "ci@local"):
        store.drop_agent("sw", name, at=AT + 5)
    message = "sw: local slot claim new for t9 on root counted 0 of 6: "

    def journal(argv, **kwargs):
        row = {"__REALTIME_TIMESTAMP": str(AT * 1000), "_PID": "97698", "MESSAGE": message}
        return subprocess.CompletedProcess(argv, 0, json.dumps(row) + "\n", "")

    read = spawn_read.slot_claims(store, "sw", run=journal)
    ended = {row["name"]: row for row in read["lives"] if row["ended_at"]}
    assert (ended["ci@remote"]["hive"], ended["ci@remote"]["runtime_backend"]) == ("h6", "local")
    assert "hive" not in ended["ci@local"]
    [found] = slot_census.findings(read)
    assert found.summary.endswith("left 1 live local agents out: ci@local")
