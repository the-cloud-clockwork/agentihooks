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
        source["ledger_events"].append({"kind": "joined", "ts_ms": start, "by": task, "target": ""})
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
        assert found["total"] == 2700.0
        assert [(row["stage"], row["seconds"]) for row in found["top"]] == [
            ("review", 1200.0),
            ("merge_queue", 900.0),
            ("intent_fix", 600.0),
        ]
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


def test_alert_and_detector_updates_do_not_lose_an_interleaved_episode(store, monkeypatch):
    source, tasks = replay()
    report = stage_detector.report(source, tasks, NOW)
    real_pipeline = store.redis.pipeline
    interleaved = []

    def pipeline(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)
        real_multi = pipe.multi

        def multi():
            if not interleaved:
                interleaved.append(True)
                standing.fire(
                    store.redis, "scratch", "ci_overrun", standing.Reading(20, 15, "minutes", "4", "new run"), NOW
                )
            return real_multi()

        pipe.multi = multi
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    stage_detector.update_findings(store, "scratch", report, NOW)
    row = standing.read(store.redis, "scratch")["ci_overrun"]
    assert (row["fired"], row["value"], row["threshold"], row["unit"], row["detail"]) == (
        4,
        20,
        15,
        "minutes",
        "new run",
    )
    assert row["detector"]["seconds"] == 2289.0
    standing.fire(store.redis, "scratch", "ci_overrun", standing.Reading(21, 15, "minutes", "1", "same run"), NOW)
    assert standing.read(store.redis, "scratch")["ci_overrun"]["fired"] == 4
    assert standing.read(store.redis, "scratch")["ci_overrun"]["detector"] == row["detector"]


def test_a_stage_that_leaves_the_window_falls_to_zero_and_updates_displayed_hours(store):
    source, tasks = replay()
    initial = stage_detector.report(source, tasks, NOW, 4 * H)
    stage_detector.update_findings(store, "scratch", initial, NOW)
    later = stage_detector.report(source, tasks, NOW + 4 * H, 4 * H)
    row = next(row for row in later["stages"] if row["stage"] == "ci_overrun")
    assert (row["seconds"], row["previous_seconds"], row["trend"]) == (0.0, 2289.0, "falling")
    stage_detector.update_findings(store, "scratch", later, NOW + 4 * H)
    updated = standing.read(store.redis, "scratch")["ci_overrun"]
    assert updated["value"] == 0.0
    assert updated["fired"] == 3
    evidence = next(
        finding.evidence
        for finding in standing.findings(store.redis, "scratch", NOW + 4 * H)
        if finding.subject == "ci_overrun"
    )
    assert evidence[-1] == "Current 0 task hours, falling, previously 0.635833 task hours"


def test_unrelated_overruns_do_not_inflate_the_standing_repeat_count(store):
    source, tasks = replay()
    source["ci_stages"].append({**stage(99, "orphan", 2000, 1, 0, "ubuntu"), "ts_ms": NOW - H})
    source["ci_runs"].append(run(99, "unrelated", NOW - H, NOW, "success"))
    found = stage_detector.report(source, tasks, NOW)
    stage_detector.update_findings(store, "scratch", found, NOW)
    assert standing.read(store.redis, "scratch")["ci_overrun"]["fired"] == 3


def test_separate_task_observations_count_once_each_for_non_ci_stages(store):
    from scripts.swarm import stages

    source, tasks = replay()
    for row in source["ci_stages"]:
        row["budget"] = 0
    found = stage_detector.report(source, tasks, NOW)
    stage_detector.update_findings(store, "scratch", found, NOW)
    row = standing.read(store.redis, "scratch")["ci_run"]
    assert row["fired"] == 3
    assert row["value"] == 1.0
    assert stages.line(found, NOW) == (
        "top stages in the last 4 hours: ci run 1.0 task hours (rising); 1.0 task hours in all, measured 0 minutes ago"
    )
    stage_detector.update_findings(store, "scratch", found, NOW + 60_000)
    assert standing.read(store.redis, "scratch")["ci_run"] == row


def test_standing_detector_retains_episode_counts_when_recent_history_rolls_over(store):
    reading = {
        "stage": "ci_overrun",
        "seconds": 3600.0,
        "labels": ["mutation-stats"],
        "label": "mutation-stats",
        "previous_seconds": 0.0,
        "trend": "rising",
        "episodes": [str(n) for n in range(30)],
    }
    row = standing.update_detector(store.redis, "scratch", reading, NOW)
    assert row == {
        "stage": "ci_overrun",
        "value": 1.0,
        "threshold": 0.0,
        "unit": "task hours",
        "detail": "",
        "first_at": NOW,
        "fired": 30,
        "episodes": [str(n) for n in range(10, 30)],
        "detector": reading,
        "updated_at": NOW,
    }
    same = standing.fire(store.redis, "scratch", "ci_overrun", standing.Reading(19, 15, "minutes", "0", "old run"), NOW)
    assert same["fired"] == 30
    changed = {**reading, "seconds": 7200.0, "episodes": [*reading["episodes"], "30"]}
    row = standing.update_detector(store.redis, "scratch", changed, NOW + 60_000)
    assert row["fired"] == 31
    assert row["episodes"] == [str(n) for n in range(11, 31)]
    assert row["first_at"] == NOW
    assert row["updated_at"] == NOW + 60_000
    assert (row["value"], row["threshold"], row["unit"], row["detail"]) == (19, 15, "minutes", "old run")
    row = standing.update_detector(store.redis, "scratch", {**changed, "seconds": 10800}, NOW + 120_000)
    assert row["fired"] == 31


@pytest.mark.parametrize("ticks,hours", [("0", "4"), ("1", "0"), ("1", "12.5"), ("-1", "13")])
def test_detector_refuses_cadence_or_windows_outside_retained_history(ticks, hours):
    with pytest.raises(ValueError) as error:
        stage_detector.settings(
            {"AGENTIHOOKS_STAGE_DETECTOR_TICKS": ticks, "AGENTIHOOKS_STAGE_DETECTOR_WINDOW_HOURS": hours}
        )
    assert str(error.value) == "stage detector needs positive ticks and a window of at most twelve hours"


def test_detector_defaults_and_the_longest_equal_windows_are_supported():
    assert stage_detector.settings({}) == stage_detector.Settings(5, 4 * H)
    assert stage_detector.settings({"AGENTIHOOKS_STAGE_DETECTOR_TICKS": "1"}) == stage_detector.Settings(1, 4 * H)
    assert stage_detector.settings({"AGENTIHOOKS_STAGE_DETECTOR_WINDOW_HOURS": "12"}) == stage_detector.Settings(
        5, 12 * H
    )


def test_each_ledger_has_its_own_default_detector_cadence(tmp_path, store):
    from scripts.swarm.metrics_outbox import Outbox, Settings

    box = Outbox(tmp_path / "empty.db", Settings("http://sink", "writer", ""))
    try:
        initial = stage_detector.record(box, store, "scratch", NOW, [], {})
        other = stage_detector.record(box, store, "another", NOW + 60_000, [], {})
        assert initial["at"] == NOW
        assert other["at"] == NOW + 60_000
        for tick in range(1, 5):
            assert stage_detector.record(box, store, "scratch", NOW + tick * 60_000, [], {}) == initial
        assert stage_detector.record(box, store, "scratch", NOW + 300_000, [], {})["at"] == NOW + 300_000
    finally:
        box.close()


def test_labels_of_the_same_stage_share_one_rank_and_loss_total():
    source, tasks = replay()
    source["ci_stages"][1]["stage"] = "unit"
    source["ci_stages"][2]["stage"] = "unit"
    report = stage_detector.report(source, tasks, NOW)
    first = report["top"][0]
    assert (first["stage"], first["seconds"], first["label"], first["labels"]) == (
        "ci_overrun",
        2289.0,
        "mutation-stats, unit",
        ["mutation-stats", "unit"],
    )


@pytest.mark.parametrize("previous,current,trend", [(3600, 3600, "unchanged"), (0, 1, "rising")])
def test_identical_windows_are_unchanged_and_one_second_of_new_loss_is_rising(previous, current, trend):
    source = rows()
    tasks = []
    for number, (end, duration) in enumerate([(NOW - H, previous), (NOW, current)], 1):
        if not duration:
            continue
        task, branch = f"t{number}", f"b{number}"
        tasks.append({"id": task, "state": "done", "branch": branch})
        start = end - duration * 1000
        source["agent_events"].append(agent("claim", task, start))
        source["delivery_events"].append(delivery("merge", task, end))
        source["ci_runs"].append(run(number, branch, start, end, "success"))
    first = stage_detector.report(source, tasks, NOW, H)["top"][0]
    assert (first["stage"], first["seconds"], first["previous_seconds"], first["trend"]) == (
        "ci_run",
        float(current),
        float(previous),
        trend,
    )
