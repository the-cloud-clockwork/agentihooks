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
    assert (
        SNAPSHOT["intent"]
        == "Scale and ship in priority order to gradually unblock more and more load: first what stops agents running, then what lets more run at once, then the build that rests on it. Every hive live, every hive agent joins and writes the ledger, launches start, the hosted ledger answers, the workstation stays under its root cap, merge queue groups move and releases stay fresh."
    )


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
    assert SNAPSHOT["hives"] == {
        "root": {"ui": "yes", "ephemeral": "no", "excluded": [], "max_agents": 6},
        "6ebe8950387c4e6a": {
            "ui": "no",
            "ephemeral": "yes",
            "excluded": ["master"],
            "max_agents": 24,
            "pod_limit_mb": 16800,
        },
        "be7b18c10eb79574": {
            "ui": "no",
            "ephemeral": "yes",
            "excluded": ["master"],
            "max_agents": 24,
            "pod_limit_mb": 16800,
        },
    }
    assert {"ui", "ephemeral", "excluded", "max_agents"} <= set(registry._default("hive"))


def test_the_probe_command_reports_the_snapshot_invariants(monkeypatch):
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
    from types import SimpleNamespace

    monkeypatch.setattr(probe, "guarded", lambda name, read: [probe.Probe(name, True, "")])
    store = SimpleNamespace(config=lambda slug: None)
    assert [item.name for item in probe.run(store, SNAPSHOT["swarm"], None, 0)] == PROBES
    assert [item.name for item in found] == PROBES
    assert list(SNAPSHOT["probes"]["invariants"]) == PROBES


def test_the_window_baseline_is_seven_samples_with_each_invariant_pass_count():
    assert SNAPSHOT["probes"] == {
        "window": {
            "start": "2026-10-27T16:33:06Z",
            "end": "2026-10-27T17:45:38Z",
            "samples": 7,
            "host": "be7b18c10eb79574",
            "taken": "after the worker one memory upgrade, Ready at 14:39 UTC",
        },
        "invariants": {
            "every hive live": {"passed": 7, "measured": "3 of 3 live in every sample"},
            "every hive agent joins and writes the ledger": {
                "passed": 0,
                "measured": "12 to 15 of 14 to 16 passed; one ci agent failed in every sample, a second in the last",
            },
            "launch success": {
                "passed": 0,
                "measured": "every hive and root group 100%; workstation claude 100%, "
                "workstation codex 83 to 85%, workstation unknown harness 0 "
                "of 4",
            },
            "full task list reads per minute": {"passed": 0, "measured": "1.1 to 5.3 per minute over 3 reads"},
            "workstation agents at or under the root cap": {
                "passed": 0,
                "measured": "unread: counted only on the root hive, the window ran on a worker hive",
            },
            "merge queue groups moving": {
                "passed": 7,
                "measured": "5 to 10 queued, last merge at most 20 minutes before",
            },
            "release age": {"passed": 7, "measured": "0.1 to 0.8 hours"},
        },
    }
