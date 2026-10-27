import json
import subprocess

import fakeredis
import pytest

from scripts.doctor import slot_census, spawn_read
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
    claims = [line("sw: local slot claim new for t9 on workstation counted 1 of 6: engineer@a-1")]
    lives = [
        life("engineer@a-1"),
        life("engineer@a-2", task="t2"),
        life("ci@b-3", slug="other", hive="workstation"),
    ]
    [found] = slot_census.findings(record(claims, lives))
    assert found.kind == slot_census.KIND
    assert found.subject == f"new@{AT}"
    assert found.measure == 2
    assert "engineer@a-2" in found.summary and "ci@b-3" in found.summary
    assert any("counted 1 of 6" in item for item in found.evidence)
    assert any("ci@b-3 of other on task t1" in item for item in found.evidence)


def test_the_census_skips_masters_remote_hives_paneless_ended_and_unstarted_agents_and_the_claimed_task():
    claims = [line("sw: local slot claim new for t9 on workstation counted 0 of 6: ")]
    lives = [
        life("master@a-1", lane="master"),
        life("remote", hive="anton"),
        life("k8s", runtime_backend="kubernetes"),
        life("paneless", pane_id=""),
        life("finished", state="finished"),
        life("ended", ended=AT - 1),
        life("later", started=AT + 1),
        life("predecessor", task="t9"),
    ]
    assert slot_census.findings(record(claims, lives)) == []


def test_only_the_watched_swarm_claims_are_judged():
    claims = [line("other: local slot claim new for t9 on workstation counted 0 of 6: ")]
    assert slot_census.findings(record(claims, [life("engineer@a-1")])) == []


def test_an_unreadable_journal_fails_the_detector():
    with pytest.raises(SwarmError, match="journalctl exit 1"):
        slot_census.findings({"slug": "sw", "claims": None, "journal_error": "journalctl exit 1: no", "lives": []})


def test_the_reader_joins_claim_lines_with_current_and_ended_lives_of_every_swarm():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    store.create(SwarmConfig("other", "/repo", 1, 0))
    store.put_agent("sw", AgentRecord("engineer@a-1", "eng", "t1", pane_id="w5M", started_at=AT - 10))
    store.put_agent("other", AgentRecord("ci@b-2", "ci", "t2", pane_id="w5G", started_at=AT - 20))
    store.drop_agent("other", "ci@b-2", at=AT + 5)
    message = "sw: local slot claim new for t9 on workstation counted 1 of 6: engineer@a-1"

    def journal(argv, **kwargs):
        assert "-g" in argv and argv[argv.index("-g") + 1] == slot_census.CLAIM
        row = {"__REALTIME_TIMESTAMP": str(AT * 1000), "_PID": "97698", "MESSAGE": message}
        return subprocess.CompletedProcess(argv, 0, json.dumps(row) + "\n", "")

    read = spawn_read.slot_claims(store, "sw", run=journal)
    assert read["claims"] == [line(message)]
    lives = {(row["slug"], row["name"]): row for row in read["lives"]}
    assert lives["sw", "engineer@a-1"]["ended_at"] == 0
    assert lives["other", "ci@b-2"]["ended_at"] == AT + 5
    [found] = slot_census.findings(read)
    assert "ci@b-2" in found.summary
