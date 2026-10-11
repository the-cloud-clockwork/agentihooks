import pytest

from scripts.swarm import stage_detector
from scripts.swarm.health import standing
from tests.swarm.test_stages import agent, delivery, rows, run, stage
from tests.swarm.test_stages import store as store

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

H = 3_600_000
NOW = 1_791_744_000_000


def replay():
    ended = [1_791_736_920_350, 1_791_737_889_784, 1_791_742_500_838]
    elapsed = [708, 1104, 1107]
    tasks = []
    found = rows()
    for number, (end, seconds) in enumerate(zip(ended, elapsed), 1):
        task, branch = f"t{number}", f"b{number}"
        tasks.append({"id": task, "state": "done", "branch": branch})
        start = end - 1_200_000
        found["agent_events"].append(agent("claim", task, start))
        found["delivery_events"].append(delivery("merge", task, end))
        found["ci_runs"].append(run(number, branch, start, end, "success"))
        found["ci_stages"].append({**stage(number, "mutation-stats", seconds, 210, 0, "ubuntu"), "ts_ms": end})
    return found, tasks


def test_eleventh_replay_counts_three_overruns_in_one_standing_finding(store):
    source, tasks = replay()
    report = stage_detector.report(source, tasks, NOW, 4 * H)
    stage_detector.update_findings(store, "scratch", report, NOW)
    found = standing.read(store.redis, "scratch")
    overrun = found["ci_overrun"]
    assert overrun["fired"] == 3
    assert overrun["detector"]["seconds"] == 2289.0
    assert overrun["detector"]["labels"] == ["mutation-stats"]
    assert len([row for row in found.values() if row["stage"] == "ci_overrun"]) == 1
    stage_detector.update_findings(store, "scratch", report, NOW + 60_000)
    assert standing.read(store.redis, "scratch") == found
    assert report["top"][0]["stage"] == "ci_overrun"
    assert report["top"][0]["trend"] == "rising"
    assert len(report["top"]) <= 3


def test_two_equal_windows_show_rising_and_falling_stages():
    source = rows()
    tasks = []
    for number, (start, end) in enumerate([(NOW - 2 * H, NOW - H // 2), (NOW - H // 2, NOW)], 1):
        task = f"w{number}"
        tasks.append({"id": task, "state": "done", "branch": f"w{number}"})
        source["agent_events"].append(agent("claim", task, start))
        source["agent_events"].append(agent("spawn", task, start, task, "codex"))
        source["ledger_events"].append({"kind": "joined", "ts_ms": start, "by": task})
        source["delivery_events"].append(delivery("merge", task, end))
    source["ci_runs"].append(run(5, "w2", NOW - H // 2, NOW, "success"))
    found = stage_detector.report(source, tasks, NOW, H)
    assert [(row["stage"], row["seconds"], row["previous_seconds"], row["trend"]) for row in found["top"]] == [
        ("build", 1800.0, 3600.0, "falling"),
        ("ci_run", 1800.0, 0.0, "rising"),
    ]


def test_record_keeps_the_shared_report_and_findings_stable_between_cadence_ticks(tmp_path, store):
    from scripts.swarm import metrics_ci, metrics_ledger, metrics_swarm, stages
    from scripts.swarm.metrics_outbox import Outbox, Settings
    from tests.swarm.test_stages import every_stage, stored

    box = Outbox(tmp_path / "events.db", Settings("http://sink", "writer", ""))
    tables = {
        "agent_events": metrics_swarm.AGENTS,
        "delivery_events": metrics_swarm.DELIVERY,
        "ci_runs": metrics_ci.RUNS,
        "ci_stages": metrics_ci.STAGES,
        "host_samples": metrics_swarm.HOST,
        "ledger_events": metrics_ledger.EVENTS,
    }
    try:
        for name, table in tables.items():
            box.append(table, [stored(table, row, n) for n, row in enumerate(every_stage()[name])])
        task = {"id": "t1", "state": "done", "branch": "b1", "depends_on": ["t0"]}
        environ = {"AGENTIHOOKS_STAGE_DETECTOR_TICKS": "2", "AGENTIHOOKS_STAGE_DETECTOR_WINDOW_HOURS": "1"}
        found = stage_detector.record(box, store, "scratch", 10 * H, [task], environ)
        assert stages.read(store, "scratch") == found
        assert found["window_hours"] == 1.0
        assert found["top"] == found["stages"][:3]
        before = standing.read(store.redis, "scratch")
        assert stage_detector.record(box, store, "scratch", 10 * H + 60_000, [task], environ) == found
        assert standing.read(store.redis, "scratch") == before
        assert stage_detector.record(box, store, "scratch", 10 * H + 120_000, [task], environ)["at"] == 10 * H + 120_000
    finally:
        box.close()


def test_detector_update_keeps_existing_alert_units_threshold_and_detail(store):
    standing.fire(store.redis, "scratch", "ci_overrun", standing.Reading(19.0, 15.0, "minutes", "1", "slow run"), NOW)
    source, tasks = replay()
    stage_detector.update_findings(store, "scratch", stage_detector.report(source, tasks, NOW), NOW)
    row = standing.read(store.redis, "scratch")["ci_overrun"]
    assert (row["value"], row["threshold"], row["unit"], row["detail"], row["fired"]) == (
        19.0,
        15.0,
        "minutes",
        "slow run",
        3,
    )
