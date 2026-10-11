import copy
import json
from pathlib import Path

import fakeredis
import pytest

from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2 import fleet_dashboard as fleet

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-OBS-05"
FX = json.loads((Path(__file__).parent / "fixtures" / "swarm_v2" / "fleet-dashboard.json").read_text())
FOUND = fleet.definition()
ON: dict[str, str] = {}
FIRST, SECOND = FX["traces"]["first"], FX["traces"]["second"]
SWARM = fleet.viewer(FX["grants"]["swarm"])


@pytest.fixture
def store():
    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def fired(sample, found=FOUND):
    return [(alert["rule"], alert["subject"]) for alert in fleet.evaluate(sample, found, ON)["alerts"]]


def test_the_definition_names_every_panel_the_dashboard_fills():
    view = fleet.dashboard(FIRST[0], SWARM, FOUND)
    assert sorted(view["panels"]) == sorted(panel["id"] for panel in FOUND["panels"])
    assert [rule.id for rule in fleet.rules(FOUND)] == [
        "protected_idle_pod",
        "impossible_pending",
        "cross_scope_denial",
    ]


def test_capacity_labels_configured_maxima_apart_from_nodes_and_tasks():
    row = fleet.dashboard(FIRST[5], SWARM, FOUND)["panels"]["capacity"][1]
    assert row["pool"] == "general-spot"
    series = row["series"]
    assert [series[key]["value"] for key in series] == [20, 1, 1, 0]
    assert "configured maximum" in series["configured_maximum"]["label"]
    assert "not live nodes" in series["configured_maximum"]["label"]
    assert len({series[key]["label"] for key in series}) == len(series)


def test_a_pending_attempt_counts_against_its_pool_and_not_as_active():
    view = fleet.dashboard(FIRST[0], SWARM, FOUND)["panels"]
    assert [view["attempts"][key]["value"] for key in ("active", "pending")] == [1, 2]
    spot = view["capacity"][1]["series"]
    assert [spot["active_tasks"]["value"], spot["pending_requests"]["value"]] == [0, 1]


def test_terminal_connectivity_is_reported_apart_from_worker_authority():
    panel = fleet.dashboard(FIRST[0], SWARM, FOUND)["panels"]["connectivity"]
    assert panel["terminal"]["value"] == {"disconnected": 1, "unknown": 2}
    assert panel["authority"]["value"] == {"held": 1, "unknown": 2}
    assert panel["terminal"]["label"] != panel["authority"]["label"]


@pytest.mark.parametrize(
    ("panel", "key", "value"),
    [
        ("quota_wait", "waiting_accounts", 1),
        ("quota_wait", "longest_wait_ms", 30000),
        ("archive_lag", "lagging", 1),
        ("archive_lag", "worst_lag_ms", 400000),
        ("checkpoint_age", "stale", 1),
        ("checkpoint_age", "oldest_age_ms", 2000000),
        ("knowledge", "feed_age_ms", 100000),
    ],
)
def test_pressure_panels_read_their_ages_from_the_sample_clock(panel, key, value):
    assert fleet.dashboard(FIRST[0], SWARM, FOUND)["panels"][panel][key]["value"] == value


def test_ages_at_the_limit_are_not_counted_stale():
    sample = copy.deepcopy(FIRST[0])
    sample["checkpoints"][0]["taken_at_ms"] = sample["at_ms"] - FOUND["limits"]["checkpoint_stale_ms"]
    sample["archive"][0]["advanced_at_ms"] = sample["at_ms"] - FOUND["limits"]["archive_lag_ms"]
    sample["brain"]["feed_at_ms"] = sample["at_ms"] - FOUND["limits"]["knowledge_stale_ms"]
    panels = fleet.dashboard(sample, SWARM, FOUND)["panels"]
    assert panels["checkpoint_age"]["stale"]["value"] == 0
    assert panels["archive_lag"]["lagging"]["value"] == 0
    assert panels["knowledge"]["stale"] is False


def test_an_empty_sample_reads_zero_everywhere():
    sample = {**FIRST[0], "attempts": [], "quota": [], "archive": [], "checkpoints": []}
    panels = fleet.dashboard(sample, SWARM, FOUND)["panels"]
    assert panels["quota_wait"]["longest_wait_ms"]["value"] == 0
    assert panels["archive_lag"]["worst_lag_ms"]["value"] == 0
    assert panels["checkpoint_age"]["oldest_age_ms"]["value"] == 0
    assert panels["connectivity"]["terminal"]["value"] == {}


def test_an_allowed_account_is_not_counted_as_waiting():
    sample = copy.deepcopy(FIRST[0])
    sample["quota"][0]["state"] = "allowed"
    assert fleet.dashboard(sample, SWARM, FOUND)["panels"]["quota_wait"]["waiting_accounts"]["value"] == 0


@pytest.mark.parametrize(
    ("grant", "title"),
    [
        (FX["grants"]["swarm"], fleet.WITHHELD),
        (FX["grants"]["operator"], "Private family medical appointments"),
    ],
)
def test_a_private_session_title_shows_only_to_a_viewer_granted_its_brain(grant, title):
    denials = fleet.dashboard(FIRST[3], fleet.viewer(grant), FOUND)["panels"]["knowledge"]["denials"]
    assert [row["session_title"] for row in denials] == [title]
    assert denials[0]["requester_scope"] == "swarm:canary"


@pytest.mark.parametrize(
    "grant",
    [
        {"actor": "operator", "label": "operator", "role": "operator"},
        {"actor": "operator", "brains": "personal-operator"},
        {"actor": "operator", "brains": [""]},
        {"actor": "operator", "brains": [7]},
    ],
)
def test_a_grant_without_a_brain_list_is_refused(grant):
    with pytest.raises(fleet.ViewRefused, match="names its brains"):
        fleet.viewer(grant)


def test_the_viewer_actor_is_sanitized():
    seen = fleet.viewer({"actor": "Authorization: Bearer abcdefghijklmnop", "brains": []})
    assert "abcdefghijklmnop" not in seen.actor


@pytest.mark.parametrize(
    ("trace", "seq", "expected"),
    [
        (FIRST, 2, []),
        (FIRST, 3, [("impossible_pending", "exe-huge-1")]),
        (FIRST, 4, [("impossible_pending", "exe-huge-1"), ("cross_scope_denial", "deny-1")]),
        (
            FIRST,
            5,
            [
                ("protected_idle_pod", "exe-idle-1"),
                ("impossible_pending", "exe-huge-1"),
                ("cross_scope_denial", "deny-1"),
            ],
        ),
        (SECOND, 1, []),
        (
            SECOND,
            3,
            [("impossible_pending", "exe-full-2")] + [("cross_scope_denial", f"deny-2-{n}") for n in range(1, 6)],
        ),
    ],
)
def test_alert_thresholds_hold_against_the_recorded_canary(trace, seq, expected):
    assert fired(trace[seq - 1]) == expected


def test_a_pending_attempt_that_scales_up_never_alerts():
    assert all(("impossible_pending", "exe-scale-1") not in fired(sample) for sample in FIRST)


def test_impossible_pending_names_its_reason():
    reasons = {
        alert["subject"]: alert["facts"]["reason"]
        for s in (FIRST[5], SECOND[5])
        for alert in fleet.evaluate(s, FOUND, ON)["alerts"]
        if alert["rule"] == "impossible_pending"
    }
    assert reasons == {
        "exe-huge-1": "no_permitted_template_fits",
        "exe-full-2": "permitted_pools_at_configured_maximum",
    }


def test_a_pool_below_its_maximum_keeps_a_fitting_request_possible():
    sample = copy.deepcopy(SECOND[5])
    sample["pools"][0]["nodes"] = 19
    assert ("impossible_pending", "exe-full-2") not in fired(sample)


def test_an_unknown_permitted_pool_cannot_hold_a_request():
    sample = copy.deepcopy(FIRST[0])
    sample["attempts"][0]["permitted_pools"] = ["absent"]
    sample["at_ms"] += 120000
    assert ("impossible_pending", "exe-scale-1") in fired(sample)


def test_a_protected_pod_that_is_busy_or_pending_never_alerts_idle():
    sample = copy.deepcopy(FIRST[5])
    sample["attempts"][2]["idle_since_ms"] = None
    assert ("protected_idle_pod", "exe-idle-1") not in fired(sample)
    sample["attempts"][2].update(idle_since_ms=0, phase="pending")
    assert ("protected_idle_pod", "exe-idle-1") not in fired(sample)
    sample["attempts"][2].update(phase="active", protected=False)
    assert ("protected_idle_pod", "exe-idle-1") not in fired(sample)


def test_alerts_are_bounded_per_rule_and_count_the_rest():
    evaluated = fleet.evaluate(SECOND[5], FOUND, ON)
    denials = [alert["subject"] for alert in evaluated["alerts"] if alert["rule"] == "cross_scope_denial"]
    assert denials == [f"deny-2-{n}" for n in range(1, 6)]
    assert evaluated["suppressed"] == {"protected_idle_pod": 0, "impossible_pending": 0, "cross_scope_denial": 1}


def test_an_alert_never_carries_a_session_title_and_never_acts():
    evaluated = fleet.evaluate(SECOND[5], FOUND, ON)
    assert "Private journal" not in json.dumps(evaluated)
    assert {alert["automatic"] for alert in evaluated["alerts"]} == {False}
    assert all(alert["owner"] and alert["action"] for alert in evaluated["alerts"])


def test_alert_facts_are_sanitized():
    sample = copy.deepcopy(FIRST[3])
    sample["brain"]["denials"][0]["requester_scope"] = "token=abcdefghijklmnopqrstuvwxyz"
    facts = fleet.evaluate(sample, FOUND, ON)["alerts"][-1]["facts"]
    assert "abcdefghijklmnopqrstuvwxyz" not in facts["requester_scope"]


@pytest.mark.parametrize(("value", "enabled"), [("off", False), (" OFF ", False), ("on", True), ("", True)])
def test_alerts_switch_off_only_by_the_setting(value, enabled):
    assert fleet.alerts_enabled({fleet.ALERTS_ENV: value}) is enabled
    assert fleet.alerts_enabled({}) is True


def test_disabled_alerts_leave_the_dashboard_and_the_incidents_alone(store):
    incidents = fleet.IncidentStore(store, "fixture")
    incidents.record(fleet.evaluate(FIRST[4], FOUND, ON))
    before = incidents.incidents()
    off = {fleet.ALERTS_ENV: "off"}
    evaluated = fleet.evaluate(FIRST[5], FOUND, off)
    assert (evaluated["state"], evaluated["alerts"]) == ("disabled", [])
    assert incidents.record(evaluated)["disabled"] is True
    assert incidents.deliver(lambda notice: pytest.fail("delivered while disabled"), off) == 0
    assert incidents.incidents() == before
    assert fleet.dashboard(FIRST[5], SWARM, FOUND)["panels"]["attempts"]["active"]["value"] == 2


def test_actionability_review_counts_owner_action_and_subject():
    evaluated = fleet.evaluate(FIRST[5], FOUND, ON)
    review = fleet.alert_actionability_review(evaluated)
    assert review == {
        "state": "enabled",
        "alerts": 3,
        "actionable": 3,
        "by_rule": {"cross_scope_denial": 1, "impossible_pending": 1, "protected_idle_pod": 1},
        "suppressed": {"cross_scope_denial": 0, "impossible_pending": 0, "protected_idle_pod": 0},
    }
    evaluated["alerts"][0]["owner"] = ""
    evaluated["alerts"][1]["facts"] = {}
    evaluated["alerts"][2]["automatic"] = True
    assert fleet.alert_actionability_review(evaluated)["actionable"] == 0


def test_an_incident_opens_once_and_resolves_when_its_condition_clears(store):
    incidents = fleet.IncidentStore(store, "fixture")
    idle = fleet.fingerprint("protected_idle_pod", "exe-idle-1")
    assert incidents.record(fleet.evaluate(FIRST[4], FOUND, ON))["opened"] == sorted(
        fleet.fingerprint(rule, subject) for rule, subject in fired(FIRST[4])
    )
    assert incidents.record(fleet.evaluate(FIRST[5], FOUND, ON))["opened"] == []
    cleared = copy.deepcopy(FIRST[5])
    cleared.update(seq=7, at_ms=cleared["at_ms"] + 60000)
    cleared["attempts"][2]["idle_since_ms"] = None
    assert incidents.record(fleet.evaluate(cleared, FOUND, ON))["resolved"] == [idle]
    row = incidents.incidents()[idle]
    assert (row["state"], row["opened_seq"], row["resolved_seq"]) == ("resolved", 5, 7)
    again = copy.deepcopy(FIRST[5])
    again.update(seq=8, at_ms=cleared["at_ms"] + 60000)
    assert incidents.record(fleet.evaluate(again, FOUND, ON))["opened"] == [idle]
    assert incidents.incidents()[idle]["opened_seq"] == 8


def test_a_replayed_or_older_evaluation_changes_nothing(store):
    incidents = fleet.IncidentStore(store, "fixture")
    incidents.record(fleet.evaluate(FIRST[5], FOUND, ON))
    before = incidents.incidents()
    for sample in (FIRST[5], FIRST[2]):
        assert incidents.record(fleet.evaluate(sample, FOUND, ON))["stale"] is True
    assert incidents.incidents() == before


def test_each_incident_is_delivered_once_with_its_action(store):
    incidents = fleet.IncidentStore(store, "fixture")
    incidents.record(fleet.evaluate(FIRST[5], FOUND, ON))
    seen: list[dict] = []
    assert incidents.deliver(seen.append, ON) == 3
    assert incidents.deliver(seen.append, ON) == 0
    assert sorted(notice["rule"] for notice in seen) == [
        "cross_scope_denial",
        "impossible_pending",
        "protected_idle_pod",
    ]
    assert all(notice["incident_id"].endswith(":6") and notice["action"] for notice in seen)


def test_a_failing_sink_leaves_the_incident_for_the_next_delivery(store):
    incidents = fleet.IncidentStore(store, "fixture")
    incidents.record(fleet.evaluate(FIRST[2], FOUND, ON))

    def refuse(notice):
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        incidents.deliver(refuse, ON)
    seen: list[dict] = []
    assert incidents.deliver(seen.append, ON) == 1


def test_a_notification_mark_skips_a_reopened_incident(store):
    incidents = fleet.IncidentStore(store, "fixture")
    incidents.record(fleet.evaluate(FIRST[2], FOUND, ON))
    huge = fleet.fingerprint("impossible_pending", "exe-huge-1")
    incidents._notified(huge, 99)
    incidents._notified("absent", 3)
    assert incidents.incidents()[huge]["notified"] is False
    assert "absent" not in incidents.incidents()


def test_a_contended_store_retries_then_refuses(store, monkeypatch):
    import redis

    incidents = fleet.IncidentStore(store, "fixture")
    calls = []

    def contended(seq, alerts):
        calls.append(seq)
        raise redis.WatchError("changed")

    monkeypatch.setattr(incidents, "_record", contended)
    with pytest.raises(SwarmError, match="kept changing"):
        incidents.record(fleet.evaluate(FIRST[2], FOUND, ON))
    assert calls == [3] * fleet.WRITE_ATTEMPTS


def test_incidents_are_kept_per_swarm(store):
    fleet.IncidentStore(store, "one").record(fleet.evaluate(FIRST[5], FOUND, ON))
    assert fleet.IncidentStore(store, "two").incidents() == {}


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    from tests import sv2_obs05_cases as cases

    first = cases.run_case(case)
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
