import json
import subprocess

import pytest

from scripts.swarm import github_reads, github_sampler, ledger_events, merge_queue

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


def test_every_rest_read_charges_the_caller_it_runs_under(github):
    api, _ = github
    merge_queue.rest(PULL, api)
    with github_reads.calling("swarm.dispatcher"):
        merge_queue.rest(PULL, api)
    merge_queue.rest(PULL, api)
    assert [(row["caller"], row["endpoint"]) for row in charged()] == [
        ("swarm.rest", PULL),
        ("swarm.dispatcher", PULL),
        ("swarm.rest", PULL),
    ]


def test_pull_request_heads_are_read_once_per_interval_across_readers(github):
    api, asked = github
    assert ledger_events.head(PR, api) == "first"
    assert ledger_events.head(PR, api) == "first"
    assert asked == [PULL]
    assert [row["caller"] for row in charged()] == ["swarm.head"]


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
    assert [row["caller"] for row in charged()] == ["swarm.checks.rules"]


def test_the_sampler_counts_used_calls_across_a_window_reset():
    assert github_sampler.spent([(100, 1), (160, 1), (40, 2), (90, 2)]) == 60 + 40 + 50
    assert github_sampler.spent([(5, 1), (5, 1)]) == 0


def test_the_sampler_reports_calls_an_hour_and_the_spend_outside_the_doctor(github, monkeypatch):
    used = iter([1000, 1600, 2200])
    clock = iter([0.0, 7200.0])

    def rate_limit(args, **kwargs):
        assert args == ["gh", "api", "rate_limit"]
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 30}
        body = {"resources": {"core": {"used": next(used), "reset": 5}}}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    for caller in ("doctor.ci", "doctor.ci", "swarm.head"):
        github_reads.charge(caller, PULL, 10.0)
    github_reads.charge("swarm.checks", "graphql", 10.0)
    monkeypatch.setenv("GH_TOKEN", "another_login")
    github_reads.charge("swarm.head", PULL, 10.0)
    monkeypatch.delenv("GH_TOKEN")
    found = github_sampler.sample(120, 3600, run=rate_limit, sleep=lambda pause: None, clock=lambda: next(clock))
    assert found["calls"] == 1200
    assert found["per_hour"] == 600
    assert found["outside_doctor_per_hour"] == 599
    assert found["unattributed_per_hour"] == 598
    assert list(found["callers"].items()) == [("doctor.ci", 2), ("swarm.head", 1)]


def test_the_sampler_command_samples_the_window_it_is_given(monkeypatch, capsys):
    monkeypatch.setattr(github_sampler, "sample", lambda minutes, interval: {"minutes": minutes, "interval": interval})
    assert github_sampler.main(["--minutes", "5", "--interval", "30"]) == 0
    assert json.loads(capsys.readouterr().out) == {"minutes": 5, "interval": 30}
    assert github_sampler.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"minutes": 60, "interval": 60}
    with pytest.raises(SystemExit):
        github_sampler.main(["--help"])
    assert capsys.readouterr().out.startswith("usage: python -m scripts.swarm.github_sampler ")
