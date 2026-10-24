import pytest

from scripts.swarm.health import findings as health


@pytest.mark.parametrize("kind", ["subscription", "api", "interactive", "master-login"])
@pytest.mark.parametrize("headless", [False, True])
def test_master_health_reports_actual_auth_and_headless_placement(kind, headless):
    agent = {
        "name": "master-agent",
        "seat": "master@fixture",
        "lane": "master",
        "state": "working",
        "desired_route_kind": "interactive",
        "route_kind": kind,
        "profile_decision": {"master_placement": {"hive": "remote", "headless": headless}},
    }
    found = health.findings({}, [agent], {}, health.Limits())
    assert {f.kind for f in found} == (
        ({"master-fallback-auth"} if kind in ("subscription", "api") else set())
        | ({"master-headless"} if headless else set())
    )
    if headless:
        finding = next(f for f in found if f.kind == "master-headless")
        assert finding.id == "master-headless/master@fixture"
        assert finding.summary == "master runs on a headless hive"
        assert finding.evidence == ("hive: remote", "agent master-agent")
        assert finding.threshold == "the master has no UI hive"
        assert finding.measure == 1
    agent.update(route_kind="interactive", profile_decision={"master_placement": {"hive": "login", "headless": False}})
    assert health.findings({}, [agent], {}, health.Limits()) == []


@pytest.mark.parametrize("state", ["finished", "ended"])
def test_completed_headless_master_does_not_raise_current_health_findings(state):
    agent = {
        "name": "old",
        "lane": "master",
        "state": state,
        "desired_route_kind": "interactive",
        "route_kind": "subscription",
        "profile_decision": {"master_placement": {"hive": "headless", "headless": True}},
    }
    assert health.findings({}, [agent], {}, health.Limits()) == []


def test_headless_health_is_master_only_and_names_an_unseated_master():
    agent = {
        "name": "unseated-master",
        "lane": "eng",
        "state": "working",
        "profile_decision": {"master_placement": {"hive": "remote", "headless": True}},
    }
    assert health.findings({}, [agent], {}, health.Limits()) == []
    worker = {**agent}
    agent["lane"] = "master"
    [found] = health.findings({}, [worker, agent], {}, health.Limits())
    assert found.id == "master-headless/unseated-master"
