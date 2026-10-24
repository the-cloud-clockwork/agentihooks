import json

import pytest

from scripts.swarm import stage_detector
from scripts.swarm.health import standing
from tests.swarm.test_stages import agent, delivery, rows, run, stage
from tests.swarm.test_stages import store as store

pytestmark = pytest.mark.unit

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


def test_the_detector_measures_claim_wait_against_claimable_tasks_only():
    from scripts.swarm import stages
    from tests.swarm.test_stages import ledger

    tasks = [{"id": "t1", "state": "open"}, {"id": "t2", "state": "open"}]
    source = rows(ledger_events=[ledger("added", NOW - 5 * H, f"tasks/{task['id']}") for task in tasks])
    ready = stages.claimable({"tasks": tasks, "freezes": [{"verb": "freeze", "target": "tasks/t2"}]}, "")
    found = stage_detector.report(source, tasks, NOW, 4 * H, ready)
    assert [(row["stage"], row["seconds"], row["previous_seconds"], row["episodes"]) for row in found["stages"]] == [
        ("seat_wait", 14400.0, 3600.0, [f"seat_wait:t1:{NOW - 5 * H}"])
    ]


def test_record_measures_with_the_readiness_it_is_given(tmp_path, store, monkeypatch):
    from scripts.swarm.metrics_outbox import Outbox, Settings

    seen = []
    measured = {"at": NOW, "window_hours": 1.0, "stages": [], "total": 0.0}
    monkeypatch.setattr(
        stage_detector, "report", lambda rows, tasks, now_ms, window_ms, ready: seen.append(ready) or measured
    )
    box = Outbox(tmp_path / "events.db", Settings("http://sink", "writer", ""))
    try:
        assert stage_detector.record(box, store, "scratch", NOW, [], {}, ready=len) == measured
    finally:
        box.close()
    assert seen == [len]


FIRST_LIVE_PASS = {
    "seat_wait": 579440.0,
    "quota_hold": 303960.0,
    "build": 66013.0,
    "review": 47969.0,
    "merge_queue": 30517.0,
    "ci_run": 19734.0,
    "intent_fix": 19097.0,
    "ci_overrun": 14341.0,
    "ci_queue": 9262.0,
    "launch": 1377.0,
    "launch_failure": 425.0,
}


def live_pass():
    return {
        "stages": [
            {
                "stage": name,
                "seconds": seconds,
                "labels": [],
                "label": "",
                "previous_seconds": 0.0,
                "trend": "rising",
                "episodes": [f"{name}:t1:0"],
            }
            for name, seconds in FIRST_LIVE_PASS.items()
        ]
    }


def test_first_live_pass_finds_only_stages_over_a_nonzero_threshold(store):
    stage_detector.update_findings(store, "scratch", live_pass(), NOW)
    found = standing.read(store.redis, "scratch")
    assert sorted(found) == [
        "ci_overrun",
        "ci_queue",
        "intent_fix",
        "merge_queue",
        "quota_hold",
        "review",
        "seat_wait",
    ]
    assert {name: row["threshold"] for name, row in found.items()} == {
        name: stage_detector.THRESHOLD_HOURS[name] for name in found
    }
    assert all(row["value"] > row["threshold"] > 0 for row in found.values())


def test_first_live_pass_clears_the_zero_threshold_findings_it_raised(store):
    for name in ("build", "ci_run", "launch", "launch_failure"):
        store.redis.hset(
            standing.key("scratch"),
            name,
            json.dumps(
                {"stage": name, "value": 1.0, "threshold": 0.0, "unit": "task hours", "fired": 1, "updated_at": NOW}
            ),
        )
    stage_detector.update_findings(store, "scratch", live_pass(), NOW)
    assert not {"build", "ci_run", "launch", "launch_failure"} & set(standing.read(store.redis, "scratch"))


def test_productive_stages_have_no_threshold_and_every_other_threshold_is_above_zero():
    assert not {"build", "ci_run", "launch"} & set(stage_detector.THRESHOLD_HOURS)
    assert stage_detector.THRESHOLD_HOURS == {
        "seat_wait": 4.0,
        "review": 4.0,
        "quota_hold": 2.0,
        "host_hold": 2.0,
        "merge_queue": 1.0,
        "intent_fix": 1.0,
        "ci_queue": 1.0,
        "ci_overrun": 0.25,
        "launch_failure": 0.25,
    }


def test_an_alert_finding_under_the_detector_threshold_keeps_its_alert_numbers(store):
    standing.fire(store.redis, "scratch", "ci_queue", standing.Reading(1.5, 1.0, "task hours in the window", "a"), NOW)
    found = {"stages": [{**live_pass()["stages"][8], "seconds": 60.0}]}
    stage_detector.update_findings(store, "scratch", found, NOW)
    row = standing.read(store.redis, "scratch")["ci_queue"]
    assert (row["value"], row["threshold"], row["unit"], row["detector"]["seconds"]) == (
        1.5,
        1.0,
        "task hours in the window",
        60.0,
    )


def test_an_alert_that_fires_while_the_detector_clears_keeps_its_finding(store, monkeypatch):
    stage_detector.update_findings(store, "scratch", live_pass(), NOW)
    real_pipeline = store.redis.pipeline
    interleaved = []

    def pipeline(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)
        real_multi = pipe.multi

        def multi():
            if not interleaved:
                interleaved.append(True)
                standing.fire(
                    store.redis, "scratch", "ci_queue", standing.Reading(1.5, 1.0, "task hours in the window", "a"), NOW
                )
            return real_multi()

        pipe.multi = multi
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    found = {"stages": [{**live_pass()["stages"][8], "seconds": 60.0}]}
    stage_detector.update_findings(store, "scratch", found, NOW)
    assert interleaved == [True]
    row = standing.read(store.redis, "scratch")["ci_queue"]
    assert (row["value"], row["threshold"], row["unit"]) == (1.5, 1.0, "task hours in the window")


def test_clearing_a_detector_finding_leaves_alert_findings_and_reports_what_it_did(store):
    standing.fire(store.redis, "scratch", "launch", standing.Reading(1.0, 0.5, "task hours in the window", "a"), NOW)
    stage_detector.update_findings(store, "scratch", live_pass(), NOW)
    assert standing.clear_detector(store.redis, "scratch", "launch") is False
    assert standing.clear_detector(store.redis, "scratch", "missing") is False
    assert standing.clear_detector(store.redis, "scratch", "review") is True
    assert sorted(standing.read(store.redis, "scratch")) == [
        "ci_overrun",
        "ci_queue",
        "intent_fix",
        "launch",
        "merge_queue",
        "quota_hold",
        "seat_wait",
    ]


def test_an_unchanged_reading_still_takes_a_new_threshold(store):
    reading = live_pass()["stages"][0]
    stage_detector.update_findings(store, "scratch", {"stages": [reading]}, NOW)
    row = standing.update_detector(store.redis, "scratch", reading, NOW + 60_000, 5.0)
    assert (row["threshold"], row["updated_at"]) == (5.0, NOW + 60_000)
    same = standing.update_detector(store.redis, "scratch", reading, NOW + 120_000, 5.0)
    assert same == row


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
        assert before == {}
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


def test_a_detector_finding_that_falls_under_its_threshold_clears(store):
    source, tasks = replay()
    initial = stage_detector.report(source, tasks, NOW, 4 * H)
    stage_detector.update_findings(store, "scratch", initial, NOW)
    evidence = next(
        finding.evidence
        for finding in standing.findings(store.redis, "scratch", NOW)
        if finding.subject == "ci_overrun"
    )
    assert evidence == ("Fired 3 times", "Current 0.635833 task hours, rising, previously 0 task hours")
    later = stage_detector.report(source, tasks, NOW + 4 * H, 4 * H)
    row = next(row for row in later["stages"] if row["stage"] == "ci_overrun")
    assert (row["seconds"], row["previous_seconds"], row["trend"]) == (0.0, 2289.0, "falling")
    stage_detector.update_findings(store, "scratch", later, NOW + 4 * H)
    assert standing.read(store.redis, "scratch") == {}


def test_unrelated_overruns_do_not_inflate_the_standing_repeat_count(store):
    source, tasks = replay()
    source["ci_stages"].append({**stage(99, "mutation-stats", 2000, 1, 0, "ubuntu"), "ts_ms": NOW - H})
    source["ci_runs"].append(run(99, "unrelated", NOW - H, NOW, "success"))
    found = stage_detector.report(source, tasks, NOW)
    stage_detector.update_findings(store, "scratch", found, NOW)
    assert standing.read(store.redis, "scratch")["ci_overrun"]["fired"] == 3


def test_ci_run_time_is_ranked_but_raises_no_standing_finding(store):
    from scripts.swarm import stages

    source, tasks = replay()
    for row in source["ci_stages"]:
        row["budget"] = 0
    found = stage_detector.report(source, tasks, NOW)
    stage_detector.update_findings(store, "scratch", found, NOW)
    assert standing.read(store.redis, "scratch") == {}
    assert stages.line(found, NOW) == (
        "top stages in the last 4 hours: ci run 1.0 task hours (rising); 1.0 task hours in all, measured 0 minutes ago"
    )


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
    row = standing.update_detector(store.redis, "scratch", reading, NOW, 0.25)
    assert row == {
        "stage": "ci_overrun",
        "value": 1.0,
        "threshold": 0.25,
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
    row = standing.update_detector(store.redis, "scratch", changed, NOW + 60_000, 0.25)
    assert row["fired"] == 31
    assert row["episodes"] == [str(n) for n in range(11, 31)]
    assert row["first_at"] == NOW
    assert row["updated_at"] == NOW
    assert (row["value"], row["threshold"], row["unit"], row["detail"]) == (19, 15, "minutes", "old run")
    row = standing.update_detector(store.redis, "scratch", {**changed, "seconds": 10800}, NOW + 120_000, 0.25)
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
    for number, (end, duration) in enumerate([(NOW - 2 * H, 3600), (NOW - H, previous), (NOW, current)], 1):
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


def test_two_ready_tasks_count_as_distinct_claim_wait_observations(store):
    source = rows(
        ledger_events=[
            {"kind": "added", "target": f"tasks/{task}", "ts_ms": NOW - 3 * H, "by": "operator"} for task in ("a", "b")
        ]
    )
    tasks = [{"id": task, "state": "open"} for task in ("a", "b")]
    found = stage_detector.report(source, tasks, NOW)
    stage_detector.update_findings(store, "scratch", found, NOW)
    row = standing.read(store.redis, "scratch")["seat_wait"]
    assert (row["fired"], row["value"], row["threshold"]) == (2, 6.0, 4.0)


def test_claim_wait_at_its_threshold_raises_no_finding_and_one_second_over_does(store):
    reading = {**live_pass()["stages"][0], "seconds": 4 * 3600.0}
    stage_detector.update_findings(store, "scratch", {"stages": [reading]}, NOW)
    assert standing.read(store.redis, "scratch") == {}
    stage_detector.update_findings(store, "scratch", {"stages": [{**reading, "seconds": 4 * 3600.0 + 1}]}, NOW)
    assert standing.read(store.redis, "scratch")["seat_wait"]["threshold"] == 4.0


def test_queue_wait_raises_no_finding_however_deep_the_queue(store):
    reading = {**live_pass()["stages"][0], "stage": "queue_wait", "seconds": 40 * 3600.0}
    stage_detector.update_findings(store, "scratch", {"stages": [reading]}, NOW)
    assert standing.read(store.redis, "scratch") == {}


def test_a_detector_finding_for_a_stage_no_longer_reported_clears_and_alert_findings_stay(store):
    retired = {**live_pass()["stages"][0], "stage": "claim_wait", "seconds": 9 * 3600.0}
    standing.update_detector(store.redis, "scratch", retired, NOW, 4.0)
    standing.fire(store.redis, "scratch", "review", standing.Reading(5.0, 4.0, "task hours in the window", "a"), NOW)
    stage_detector.update_findings(store, "scratch", {"stages": []}, NOW)
    assert sorted(standing.read(store.redis, "scratch")) == ["review"]


def test_under_budget_runs_do_not_count_as_overruns_but_one_second_does(store):
    source, tasks = replay()
    end = NOW - 60_000
    tasks.append({"id": "t4", "state": "done", "branch": "b4"})
    source["agent_events"].append(agent("claim", "t4", end - 600_000))
    source["delivery_events"].append(delivery("merge", "t4", end))
    source["ci_runs"].append(run(4, "b4", end - 600_000, end, "success"))
    source["ci_stages"].append({**stage(4, "unit", 300, 500, 0, "ubuntu"), "ts_ms": end})
    found = stage_detector.report(source, tasks, NOW)
    stage_detector.update_findings(store, "scratch", found, NOW)
    assert standing.read(store.redis, "scratch")["ci_overrun"]["fired"] == 3
    source["ci_stages"][-1]["seconds"] = 501
    found = stage_detector.report(source, tasks, NOW)
    stage_detector.update_findings(store, "scratch", found, NOW)
    assert standing.read(store.redis, "scratch")["ci_overrun"]["fired"] == 4


def test_restarted_detector_respects_the_saved_cadence_counter(tmp_path, store):
    from scripts.swarm.metrics_outbox import Outbox, Settings

    box = Outbox(tmp_path / "restart.db", Settings("http://sink", "writer", ""))
    store.redis.set(store.key("scratch", "stage-detector-ticks"), 3)
    try:
        assert stage_detector.record(box, store, "scratch", NOW, [], {}) == {}
        assert stage_detector.record(box, store, "scratch", NOW + 60_000, [], {}) == {}
        assert stage_detector.record(box, store, "scratch", NOW + 120_000, [], {})["at"] == NOW + 120_000
    finally:
        box.close()
