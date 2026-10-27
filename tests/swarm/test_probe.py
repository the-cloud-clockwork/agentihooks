import json
from types import SimpleNamespace

import pytest

from scripts.swarm import probe
from scripts.swarm.probe import HOUR_MS, MINUTE_MS, Probe, Thresholds
from scripts.swarm.store import AgentRecord

NOW = 100 * HOUR_MS
LIMITS = Thresholds()


def test_thresholds_live_in_one_typed_config():
    assert probe.THRESHOLDS == Thresholds(6 * HOUR_MS, 0.9, 3, 12.0, HOUR_MS, 24 * HOUR_MS)


def test_a_probe_line_says_pass_or_fail_then_its_name_and_number():
    assert Probe("release age", True, "0.5 hours").line() == "pass\trelease age\t0.5 hours"
    assert Probe("release age", False, "30.0 hours").line() == "fail\trelease age\t30.0 hours"


def test_every_hive_live_names_the_stale_hives_and_fails_with_none_registered():
    fresh = {"id": "root", "heartbeat_at": NOW - 1000}
    stale = {"id": "pod", "heartbeat_at": NOW - 90_001}
    never = {"id": "new", "heartbeat_at": 0}
    assert probe.hives([fresh], NOW) == Probe("every hive live", True, "1 of 1 live")
    assert probe.hives([fresh, stale, never], NOW) == Probe("every hive live", False, "1 of 3 live: pod, new")
    assert probe.hives([], NOW) == Probe("every hive live", False, "0 of 0 live")


def report(**checks):
    return {"checks": {"ledger_join": True, "ledger_write": True, **checks}}


def test_every_hive_agent_must_pass_ledger_join_and_ledger_write():
    name = "every hive agent joins and writes the ledger"
    assert probe.hive_joins({"a": report(), "b": report()}) == Probe(name, True, "2 of 2 passed")
    found = probe.hive_joins({"a": report(), "b": report(ledger_join=False), "c": report(ledger_write="yes")})
    assert found == Probe(name, False, "1 of 3 passed: b, c")
    assert probe.hive_joins({"d": {"checks": {"ledger_join": True}}, "e": {}}) == Probe(
        name, False, "0 of 2 passed: d, e"
    )
    assert probe.hive_joins({}) == Probe(name, True, "0 of 0 passed")


def test_placements_name_each_agent_hive_and_harness():
    agents = [
        {"name": "a", "hive": "pod", "harness": "codex", "runtime_target": {"host_id": "other"}},
        {"name": "b", "hive": "", "harness": "claude", "runtime_target": {"host_id": "root"}},
        {"name": "c", "hive": "", "harness": "", "runtime_target": {}},
        {"name": "d"},
    ]
    assert probe.placements(agents) == {
        "a": ("pod", "codex"),
        "b": ("root", "claude"),
        "c": ("workstation", "unknown"),
        "d": ("workstation", "unknown"),
    }


def launch(agent, state, age_ms=0):
    return {"agent": agent, "state": state, "at": NOW - age_ms}


def test_launch_success_rate_is_graded_per_hive_and_harness_inside_the_window():
    placed = {"a": ("pod", "codex"), "b": ("pod", "codex"), "c": ("root", "claude")}
    rows = [
        *(launch("a", "started") for _ in range(9)),
        launch("b", "failed"),
        launch("c", "started"),
        launch("c", "failed"),
        launch("x", "failed", LIMITS.launch_window_ms),
        launch("c", "failed", LIMITS.launch_window_ms + 1),
        launch("c", "waiting"),
    ]
    assert probe.launches(rows, placed, NOW, LIMITS) == [
        Probe("launch success hive pod harness codex", True, "90%, 9 of 10 started in the last 6 hours"),
        Probe("launch success hive root harness claude", False, "50%, 1 of 2 started in the last 6 hours"),
        Probe("launch success hive unknown harness unknown", False, "0%, 0 of 1 started in the last 6 hours"),
    ]


def test_no_launches_in_the_window_pass_and_say_so():
    rows = [launch("a", "waiting"), launch("a", "started", 2 * HOUR_MS + 1)]
    limits = Thresholds(launch_window_ms=2 * HOUR_MS)
    assert probe.launches(rows, {}, NOW, limits) == [Probe("launch success", True, "no launches in the last 2 hours")]


def test_task_list_reads_per_minute_time_the_configured_number_of_full_reads():
    reads, ticks = [], iter([10.0, 25.0])
    found = probe.task_list_reads(lambda: reads.append(1), Thresholds(task_list_samples=4), lambda: next(ticks))
    assert reads == [1, 1, 1, 1]
    assert found == Probe("full task list reads per minute", True, "16.0 per minute over 4 reads")
    ticks = iter([0.0, 20.0])
    assert probe.task_list_reads(lambda: None, LIMITS, lambda: next(ticks)) == Probe(
        "full task list reads per minute", False, "9.0 per minute over 3 reads"
    )
    ticks = iter([0.0, 15.0])
    assert probe.task_list_reads(lambda: None, LIMITS, lambda: next(ticks)).passed is True


def test_workstation_agents_pass_at_or_under_the_root_cap():
    name = "workstation agents at or under the root cap"
    assert probe.workstation(6, 6, 20) == Probe(name, True, "6 running, root cap 6, swarm local limit 20")
    assert probe.workstation(7, 6, 4) == Probe(name, False, "7 running, root cap 6, swarm local limit 4")


def test_merge_queue_groups_move_when_dev_merged_inside_the_stall_window():
    name = "merge queue groups moving"
    assert probe.merge_queue({"queue": 0, "merged": 0, "at": NOW}, LIMITS) == Probe(name, True, "queue empty")
    moving = {"queue": 3, "merged": NOW - HOUR_MS + 1, "at": NOW}
    assert probe.merge_queue(moving, LIMITS) == Probe(name, True, "3 queued, last merge 59 minutes ago")
    stuck = {"queue": 2, "merged": NOW - HOUR_MS, "at": NOW}
    assert probe.merge_queue(stuck, LIMITS) == Probe(name, False, "2 queued, last merge 60 minutes ago")
    unseen = {"queue": 4, "merged": 0, "at": NOW}
    assert probe.merge_queue(unseen, LIMITS) == Probe(name, False, "4 queued, no merge observed")


def test_release_age_passes_at_or_under_the_maximum():
    assert probe.release(NOW - 24 * HOUR_MS, NOW, LIMITS) == Probe("release age", True, "24.0 hours")
    assert probe.release(NOW - 24 * HOUR_MS - 1, NOW, LIMITS).passed is False
    assert probe.release(NOW - HOUR_MS // 2, NOW, LIMITS) == Probe("release age", True, "0.5 hours")


def test_a_reader_that_fails_turns_its_probe_into_a_named_fail():
    def broken():
        raise OSError("no route")

    assert probe.guarded("release age", broken) == [Probe("release age", False, "unread: OSError: no route")]
    assert probe.guarded("release age", lambda: [Probe("x", True, "1")]) == [Probe("x", True, "1")]


class Store:
    def __init__(self):
        history = [{"name": "old", "hive": "pod", "harness": "codex"}]
        self.redis = SimpleNamespace(
            lrange=lambda key, start, end: (
                [json.dumps(row) for row in history] if (key, start, end) == ("s:history", 0, -1) else []
            )
        )

    def config(self, slug):
        assert slug == "s"
        return SimpleNamespace(repo="/far/agentihooks", local_max_agents=20)

    def key(self, slug, name):
        return f"{slug}:{name}"

    def agents(self, slug):
        rows = [
            AgentRecord("new", "eng", "t", harness="claude", hive="root"),
            AgentRecord("here", "eng", "t"),
            AgentRecord("h1", "eng", "t", hive="pod"),
            AgentRecord("h2", "eng", "t", hive="pod"),
            AgentRecord("h3", "ci", "t", hive="pod"),
            AgentRecord("h4", "eng", "t", hive="pod", state="starting"),
            AgentRecord("h5", "eng", "t", hive="pod", state="ended"),
            AgentRecord("h6", "eng", "t", hive="pod", state="finished"),
        ]
        return rows if slug == "s" else []

    def launches(self, slug):
        return [launch("old", "failed"), launch("new", "started")] if slug == "s" else []


STORE = Store()


@pytest.fixture
def live(monkeypatch):
    from scripts.hive import join_report, registry
    from scripts.swarm import commands, local_slots, publish
    from scripts.swarm.health import merge_stall

    records = [
        {"id": "root", "heartbeat_at": NOW},
        {"id": "pod", "heartbeat_at": NOW},
        {"id": "gone", "heartbeat_at": 0},
    ]
    reports = {"h1": report(), "h2": report(ledger_join=False), "new": report(), "here": report()}
    ticks = iter([0.0, 10.0])
    monkeypatch.setattr(registry, "hives", lambda redis: records if redis is STORE.redis else [])
    monkeypatch.setattr(
        registry, "show", lambda redis, hive: {"max_agents": 6} if (redis, hive) == (STORE.redis, "root") else None
    )
    monkeypatch.setattr(
        join_report, "read", lambda redis, agent: reports.get(agent.name, {}) if redis is STORE.redis else {}
    )
    monkeypatch.setattr(commands, "hive_id", lambda: "root")
    monkeypatch.setattr(
        local_slots, "occupancy", lambda store, machine: ({"root": 5}[machine] if store is STORE else 0, 0)
    )
    monkeypatch.setattr(publish, "local_repository", lambda value, repo: f"/here/{value == repo}")
    monkeypatch.setattr(
        merge_stall,
        "request",
        lambda repo, args: (
            {"published_at": "1970-01-05T02:00:00+00:00"} if (repo, args) == ("/here/True", probe.RELEASE) else {}
        ),
    )
    monkeypatch.setattr(
        merge_stall,
        "observe",
        lambda repo, at: {"queue": 2, "merged": at - 5 * MINUTE_MS, "at": at} if repo == "/here/True" else {},
    )
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    return SimpleNamespace(tasks=lambda slug: {"s": []}[slug])


def test_run_reads_the_live_swarm_into_one_line_per_invariant(live):
    assert [found.line() for found in probe.run(STORE, "s", live, NOW)] == [
        "fail\tevery hive live\t2 of 3 live: gone",
        "fail\tevery hive agent joins and writes the ledger\t1 of 3 passed: h2, h3",
        "fail\tlaunch success hive pod harness codex\t0%, 0 of 1 started in the last 6 hours",
        "pass\tlaunch success hive root harness claude\t100%, 1 of 1 started in the last 6 hours",
        "pass\tfull task list reads per minute\t18.0 per minute over 3 reads",
        "pass\tworkstation agents at or under the root cap\t5 running, root cap 6, swarm local limit 20",
        "pass\tmerge queue groups moving\t2 queued, last merge 5 minutes ago",
        "pass\trelease age\t2.0 hours",
    ]


def test_run_counts_root_agents_only_on_the_root_hive(live, monkeypatch):
    from scripts.swarm import commands, local_slots

    monkeypatch.setattr(
        local_slots, "occupancy", lambda store, machine: ({"root": 7}[machine] if store is STORE else 0, 0)
    )
    found = probe.run(STORE, "s", live, NOW, Thresholds(task_list_reads_min=20.0))
    assert found[4] == Probe("full task list reads per minute", False, "18.0 per minute over 3 reads")
    assert found[5] == Probe(
        "workstation agents at or under the root cap", False, "7 running, root cap 6, swarm local limit 20"
    )
    monkeypatch.setattr(commands, "hive_id", lambda: "pod")
    assert probe.run(STORE, "s", live, NOW)[5] == Probe(
        "workstation agents at or under the root cap",
        False,
        "unread: SwarmError: counted only on the root hive, this host is pod",
    )


def test_run_reports_each_unreadable_invariant_as_a_fail(monkeypatch):
    from scripts.hive import registry
    from scripts.swarm import commands, publish

    def refused(*_args):
        raise RuntimeError("down")

    monkeypatch.setattr(registry, "hives", refused)
    monkeypatch.setattr(registry, "show", refused)
    monkeypatch.setattr(publish, "local_repository", refused)
    monkeypatch.setattr(commands, "hive_id", lambda: "root")
    store = SimpleNamespace(
        config=lambda slug: SimpleNamespace(repo="r", local_max_agents=1),
        redis=SimpleNamespace(lrange=refused),
        key=lambda slug, name: name,
        agents=refused,
    )
    ledger = SimpleNamespace(tasks=refused)
    assert [found.line() for found in probe.run(store, "s", ledger, NOW)] == [
        f"fail\t{name}\tunread: RuntimeError: down"
        for name in (
            "every hive live",
            "every hive agent joins and writes the ledger",
            "launch success",
            "full task list reads per minute",
            "workstation agents at or under the root cap",
            "merge queue groups moving",
            "release age",
        )
    ]


def test_the_probe_command_prints_each_probe_line(monkeypatch, capsys):
    from scripts.swarm import cli

    seen = []

    def run(store, slug, ledger, now_ms):
        seen.append((store, slug, type(ledger).__name__, now_ms))
        return [Probe("a", True, "1"), Probe("b", False, "2")]

    monkeypatch.setattr(probe, "run", run)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    cli.probe_command("store", SimpleNamespace(slug="s"))
    assert capsys.readouterr().out == "pass\ta\t1\nfail\tb\t2\n"
    assert seen == [("store", "s", "LedgerClient", NOW)]
    assert cli.build_parser().parse_args(["s", "probe"]).command == "probe"
