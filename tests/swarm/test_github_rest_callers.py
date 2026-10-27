import json
import subprocess

import pytest

from scripts.swarm import github_reads, github_sampler, ledger_events, merge_queue, queue_priority

pytestmark = pytest.mark.unit
PR = "https://github.com/org/repo/pull/7"
PULL = "repos/org/repo/pulls/7"
RULES = "repos/org/repo/rules/branches/dev"
CHECKS = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "Gate"}]}}]


@pytest.fixture
def github(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    asked = []

    def api(args, **kwargs):
        asked.append(args[2])
        if args[2] == RULES:
            body = CHECKS
        elif args[2] == PULL:
            body = {"head": {"sha": "first"}}
        else:
            body = {"workflow_runs": []}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    return api, asked


def charged():
    return [json.loads(line) for line in (github_reads.home() / "charged.jsonl").read_text().splitlines()]


def test_every_rest_read_charges_the_module_that_asked(github):
    api, _ = github
    merge_queue.rest(PULL, api)
    queue_priority.merge_queue.rest(PULL, api, "swarm.dispatcher")
    assert [(row["caller"], row["endpoint"]) for row in charged()] == [
        ("tests.swarm.test_github_rest_callers", PULL),
        ("swarm.dispatcher", PULL),
    ]


def test_pull_request_heads_are_read_once_per_interval_across_readers(github):
    api, asked = github
    assert ledger_events.head(PR, api) == "first"
    assert ledger_events.head(PR, api) == "first"
    assert asked == [PULL]
    assert [row["caller"] for row in charged()] == ["scripts.swarm.ledger_events"]


def test_required_check_rules_are_read_once_per_interval_across_pull_requests(github, monkeypatch):
    api, asked = github
    resource = {"repository": {"nameWithOwner": "org/repo"}, "baseRefName": "dev", "baseRef": None}
    monkeypatch.setattr(merge_queue, "graphql", lambda query, variables, run: {"resource": resource})
    seen = []
    monkeypatch.setattr(ledger_events, "view", lambda url, read, required_checks: seen.append(required_checks))
    merge_queue.required_view(PR, api)
    merge_queue.required_view("https://github.com/org/repo/pull/8", api)
    assert asked == [RULES]
    assert seen == [{"Gate": None}, {"Gate": None}]
    assert [row["caller"] for row in charged()] == ["scripts.swarm.merge_queue"]


def test_the_sampler_counts_used_calls_across_a_window_reset():
    assert github_sampler.spent([(100, 1), (160, 1), (40, 2), (90, 2)]) == 60 + 40 + 50


def test_the_sampler_reports_calls_an_hour_and_the_spend_outside_the_doctor(github, monkeypatch):
    used = iter([1000, 1600, 2200])
    clock = iter([0.0, 7200.0])

    def rate_limit(args, **kwargs):
        assert args == ["gh", "api", "rate_limit"]
        body = {"resources": {"core": {"used": next(used), "reset": 5}}}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    for caller in ("doctor.ci", "doctor.ci", "swarm.head"):
        github_reads.charge(caller, PULL, 10.0)
    found = github_sampler.sample(120, 3600, run=rate_limit, sleep=lambda pause: None, clock=lambda: next(clock))
    assert found["calls"] == 1200
    assert found["per_hour"] == 600
    assert found["outside_doctor_per_hour"] == 599
    assert found["callers"] == {"doctor.ci": 2, "swarm.head": 1}
