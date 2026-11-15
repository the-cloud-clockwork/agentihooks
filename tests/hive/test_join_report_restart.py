"""A hive member that restarted ends the join reports its agents wrote before the restart, since they died with it."""

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from tests.hive.test_join_report import passing

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.swarm.store import AgentRecord, RedisStore

RESTARTED = {"exit_reason": "agent ended with its member's restart", "status": "ended"}


@pytest.fixture
def store() -> "RedisStore":
    from fakeredis import FakeRedis

    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("swarm", "repo", 8, 0))
    return store


def _running(store: "RedisStore", seat: str) -> "AgentRecord":
    from scripts.swarm.store import AgentRecord

    agent = AgentRecord(store.next_name("swarm", "eng"), "eng", "task", runtime_target={"host_id": "worker"})
    agent = store.start_execution("swarm", replace(agent, seat=f"{seat}@swarm"))
    store.put_agent("swarm", agent)
    return agent


def _reported(store: "RedisStore", agent: "AgentRecord", recorded: dict[str, str]) -> None:
    from scripts.hive import join_report

    report = join_report.graded(
        {
            "agent": agent.name,
            "slug": "swarm",
            "task": agent.task,
            "execution_id": agent.execution_id,
            "generation": agent.generation,
            "session_id": f"session {agent.name}",
            "started_at": 1000,
            "heartbeat_at": 1000,
            "status": "running",
            "checks": passing(),
            "context": {},
            **recorded,
        }
    )
    store.redis.hset(join_report.key("worker"), agent.execution_id, json.dumps(report))


def _after(store: "RedisStore") -> dict[str, dict[str, Any]]:
    from scripts.hive import join_report

    return {report["agent"]: report for report in join_report.listing(store.redis, "worker")}


@pytest.mark.feature("hive-join-heartbeat-ends-restarted-member-reports")
def test_a_restarted_member_ends_the_reports_its_agents_wrote_before_the_restart(
    store: "RedisStore", monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import join_report, registry

    monkeypatch.setattr(registry, "member_name", lambda: "pod-a")
    monkeypatch.setattr(join_report, "local_epoch", lambda: "boot/2", raising=False)
    monkeypatch.setattr(join_report, "cheap_checks", lambda *args: {})
    before, other, unrecorded, current = (_running(store, f"eng-{seat}") for seat in range(1, 5))
    _reported(store, before, {"member": "pod-a", "epoch": "boot/1"})
    _reported(store, other, {"member": "pod-b", "epoch": "boot/1"})
    _reported(store, unrecorded, {})
    join_report.start(store.redis, "swarm", current, "session current", passing(), {}, 1000)
    join_report.heartbeat(store.redis, "worker", 1001)
    after = _after(store)
    assert {key: after[before.name].get(key) for key in ("ended_at", *RESTARTED)} == {"ended_at": 1001, **RESTARTED}
    for agent in (other, unrecorded, current):
        assert after[agent.name]["status"] == "running"
        assert "ended_at" not in after[agent.name]
    live = {report["agent"] for report in join_report.live(store.redis, "worker")}
    assert live == {other.name, unrecorded.name, current.name}


@pytest.mark.feature("hive-join-heartbeat-ends-restarted-member-reports")
@pytest.mark.parametrize(("current", "recorded"), [("", "boot/1"), ("boot/2", ""), ("boot/2", "boot/2")])
def test_a_report_is_kept_while_either_epoch_is_unknown_or_they_match(
    store: "RedisStore", monkeypatch: pytest.MonkeyPatch, current: str, recorded: str
) -> None:
    from scripts.hive import join_report, registry

    monkeypatch.setattr(registry, "member_name", lambda: "pod-a")
    monkeypatch.setattr(join_report, "local_epoch", lambda: current)
    monkeypatch.setattr(join_report, "cheap_checks", lambda *args: {})
    agent = _running(store, "eng-1")
    _reported(store, agent, {"member": "pod-a", "epoch": recorded})
    join_report.heartbeat(store.redis, "worker", 181000)
    report = _after(store)[agent.name]
    assert (report["status"], report["heartbeat_at"]) == ("running", 181000)
    assert "ended_at" not in report


@pytest.mark.feature("hive-join-heartbeat-ends-restarted-member-reports")
def test_a_join_report_records_its_member_and_that_members_epoch(
    store: "RedisStore", monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import join_report, registry

    monkeypatch.setattr(registry, "member_name", lambda: "pod-a")
    monkeypatch.setattr(join_report, "local_epoch", lambda: "boot/7")
    agent = _running(store, "eng-1")
    report = join_report.start(store.redis, "swarm", agent, "session", passing(), {}, 1000)
    assert (report["member"], report["epoch"]) == ("pod-a", "boot/7")
    assert join_report.read(store.redis, agent) == report


STAT = "1 (ti) ni) S " + " ".join(map(str, range(1, 19))) + " 98765 4096\n"


def _proc(root: "Path", stat: str) -> "Path":
    (root / "boot_id").write_text("boot-a\n")
    (root / "1").mkdir()
    (root / "1" / "stat").write_text(stat)
    return root


@pytest.mark.feature("hive-join-heartbeat-ends-restarted-member-reports")
def test_the_epoch_names_the_boot_and_the_tick_process_one_started_at(tmp_path: "Path") -> None:
    from scripts.hive import join_report

    root = _proc(tmp_path, STAT)
    assert join_report.epoch(root / "boot_id", root) == "boot-a/98765"


@pytest.mark.feature("hive-join-heartbeat-ends-restarted-member-reports")
@pytest.mark.parametrize("broken", ["boot", "stat", "short"])
def test_an_unreadable_epoch_is_empty(tmp_path: "Path", broken: str) -> None:
    from scripts.hive import join_report

    root = _proc(tmp_path, "1 (tini) S 0 1\n" if broken == "short" else STAT)
    removed = {"boot": root / "boot_id", "stat": root / "1" / "stat"}.get(broken)
    if removed is not None:
        removed.unlink()
    assert join_report.epoch(root / "boot_id", root) == ""


@pytest.mark.feature("hive-join-heartbeat-ends-restarted-member-reports")
def test_the_local_epoch_reads_this_hosts_boot_id_and_process_one(
    tmp_path: "Path", monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import join_report
    from scripts.swarm_v2.runtime import process

    root = _proc(tmp_path, STAT)
    monkeypatch.setattr(process, "BOOT_ID", root / "boot_id")
    monkeypatch.setattr(process, "PROC", root)
    assert join_report.local_epoch() == "boot-a/98765"
