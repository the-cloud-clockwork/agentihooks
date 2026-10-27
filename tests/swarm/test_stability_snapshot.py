import json
from dataclasses import asdict, fields
from pathlib import Path

from scripts import quota_state, session_bands
from scripts.hive import registry
from scripts.swarm import probe
from scripts.swarm.health import findings
from scripts.swarm.store import SwarmConfig

SNAPSHOT = json.loads((Path(__file__).parent / "stability_snapshot_v1.json").read_text())
PROBES = [
    "every hive live",
    "every hive agent joins and writes the ledger",
    "launch success",
    "full task list reads per minute",
    "workstation agents at or under the root cap",
    "merge queue groups moving",
    "release age",
]


def test_the_snapshot_is_version_one_of_rig_grade_swarm():
    assert SNAPSHOT["version"] == 1
    assert SNAPSHOT["swarm"] == "rig-grade-swarm"
    assert list(SNAPSHOT) == ["version", "swarm", "taken", "intent", "caps", "hives", "thresholds", "probes"]
    assert SNAPSHOT["intent"].startswith("Scale and ship in priority order to gradually unblock more and more load")


def test_probe_thresholds_match_the_snapshot():
    assert asdict(probe.THRESHOLDS) == {
        "launch_window_ms": 21_600_000,
        "launch_success_min": 0.9,
        "task_list_samples": 3,
        "task_list_reads_min": 12.0,
        "merge_stall_ms": 3_600_000,
        "release_age_max_ms": 86_400_000,
    }
    assert SNAPSHOT["thresholds"]["probe"] == asdict(probe.THRESHOLDS)


def test_health_limits_match_the_snapshot():
    assert asdict(findings.Limits()) == {
        "ceremony_min": 20,
        "ceremony_ratio": 12,
        "self_queued": 3,
        "reruns": 2,
        "review_rounds": 3,
        "idle_ticks": 3,
        "stale_minutes": 30,
        "stalled_minutes": 10,
        "watch_min": 20,
        "watch_ratio": 5,
        "master_watch_min": 60,
        "master_watch_ratio": 15,
        "cooldown_minutes": 60,
        "drain_minutes": 10,
        "drain_left": 10,
    }
    assert SNAPSHOT["thresholds"]["health"] == asdict(findings.Limits())


def test_session_caps_and_hive_liveness_match_the_snapshot():
    assert session_bands.FIVE_HOUR_BANDS == ((60.0, 6), (40.0, 4), (10.0, 3), (5.0, 2))
    assert session_bands.WEEK_FLOOR == 5.0
    assert session_bands.HIVE_CEILING == 12
    assert registry.LIVE_MS == 90_000
    assert quota_state.DRAIN_LEFT == 10
    assert SNAPSHOT["thresholds"]["sessions"] == {
        "five_hour_bands": [list(band) for band in session_bands.FIVE_HOUR_BANDS],
        "week_floor": session_bands.WEEK_FLOOR,
        "hive_ceiling": session_bands.HIVE_CEILING,
        "hive_live_ms": registry.LIVE_MS,
    }


def test_swarm_caps_are_the_desired_state_and_name_swarm_settings():
    assert SNAPSHOT["caps"] == {
        "max_eng": 16,
        "max_ci": 13,
        "max_plan": 2,
        "local_max_agents": 20,
        "memory_per_agent_mb": 700,
        "load_high": 1.5,
        "load_low": 1.0,
        "swap_full_share": 0.9,
        "swap_floor_mb": 4096,
        "autonomy": "full",
        "effort_min": "medium",
        "effort_max": "xhigh",
        "scaling": "auto",
        "caps_pinned": True,
        "hive_placement": True,
    }
    assert set(SNAPSHOT["caps"]) <= {field.name for field in fields(SwarmConfig)}


def test_hive_settings_are_the_desired_state_and_name_hive_settings():
    worker = {"ui": "no", "ephemeral": "yes", "excluded": ["master"], "max_agents": 24, "pod_limit_mb": 16800}
    assert SNAPSHOT["hives"] == {
        "root": {"ui": "yes", "ephemeral": "no", "excluded": [], "max_agents": 6},
        "6ebe8950387c4e6a": worker,
        "be7b18c10eb79574": worker,
    }
    assert {"ui", "ephemeral", "excluded", "max_agents"} <= set(registry._default("hive"))


def test_the_probe_command_reports_the_snapshot_invariants():
    limits = probe.THRESHOLDS
    ticks = iter([0.0, 60.0])
    found = [
        probe.hives([], 0),
        probe.hive_joins({}),
        *probe.launches([], {}, 0, limits),
        probe.task_list_reads(lambda: None, limits, lambda: next(ticks)),
        probe.workstation(0, 0, 0),
        probe.merge_queue({"queue": 0}, limits),
        probe.release(0, 0, limits),
    ]
    assert [item.name for item in found] == PROBES
    assert list(SNAPSHOT["probes"]["invariants"]) == PROBES


def test_the_window_baseline_is_seven_samples_with_each_invariant_pass_count():
    window = SNAPSHOT["probes"]["window"]
    assert (window["start"], window["end"], window["samples"]) == ("2026-10-27T16:33:06Z", "2026-10-27T17:45:38Z", 7)
    assert {name: found["passed"] for name, found in SNAPSHOT["probes"]["invariants"].items()} == {
        "every hive live": 7,
        "every hive agent joins and writes the ledger": 0,
        "launch success": 0,
        "full task list reads per minute": 0,
        "workstation agents at or under the root cap": 0,
        "merge queue groups moving": 7,
        "release age": 7,
    }
