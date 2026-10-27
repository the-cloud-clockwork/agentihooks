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
        if "/rules/branches/" in args[2]:
            body = CHECKS
        elif "/pulls/" in args[2]:
            body = {"head": {"sha": f"head of {args[2]}"}}
        else:
            body = {"workflow_runs": []}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    return api, asked


def charged():
    return [json.loads(line) for line in (github_reads.home() / "charged.jsonl").read_text().splitlines()]


def test_every_rest_read_charges_the_caller_it_runs_under(github, monkeypatch):
    api, _ = github
    monkeypatch.setattr(merge_queue.time, "time", lambda: 50.0)
    merge_queue.rest(PULL, api)
    with github_reads.calling("swarm.dispatcher"):
        merge_queue.rest(PULL, api)
    merge_queue.rest(PULL, api)
    assert [(row["caller"], row["endpoint"], row["at"]) for row in charged()] == [
        ("swarm.rest", PULL, 50.0),
        ("swarm.dispatcher", PULL, 50.0),
        ("swarm.rest", PULL, 50.0),
    ]


def test_pull_request_heads_are_read_once_per_interval_across_readers(github):
    api, asked = github
    assert ledger_events.head(PR, api) == f"head of {PULL}"
    assert ledger_events.head(PR, api) == f"head of {PULL}"
    assert ledger_events.head("https://github.com/org/repo/pull/8", api) == "head of repos/org/repo/pulls/8"
    assert asked == [PULL, "repos/org/repo/pulls/8"]
    assert [row["caller"] for row in charged()] == ["swarm.head", "swarm.head"]


def test_required_check_rules_are_read_once_per_interval_across_pull_requests(github, monkeypatch):
    api, asked = github
    bases = iter(["dev", "dev", "main"])
    monkeypatch.setattr(
        merge_queue,
        "graphql",
        lambda query, variables, run: {
            "resource": {"repository": {"nameWithOwner": "org/repo"}, "baseRefName": next(bases), "baseRef": None}
        },
    )
    seen = []
    monkeypatch.setattr(ledger_events, "view", lambda url, read, required_checks: seen.append(required_checks))
    merge_queue.required_view(PR, api)
    merge_queue.required_view("https://github.com/org/repo/pull/8", api)
    merge_queue.required_view("https://github.com/org/repo/pull/9", api)
    assert asked == [RULES, "repos/org/repo/rules/branches/main"]
    assert seen == [{"Gate": None}] * 3
    assert [row["caller"] for row in charged()] == ["swarm.checks.rules"] * 2


def test_the_sampler_counts_used_calls_across_a_window_reset():
    assert github_sampler.spent([(100, 1), (160, 1), (40, 2), (90, 2)]) == 60 + 40 + 50
    assert github_sampler.spent([(5, 1), (5, 1)]) == 0


def rate_limit(used):
    def read(args, **kwargs):
        assert args == ["gh", "api", "rate_limit"]
        assert kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 30}
        body = {"resources": {"core": {"used": next(used), "reset": 5}}}
        return subprocess.CompletedProcess(args, 0, json.dumps(body), "")

    return read


def test_the_sampler_reports_calls_an_hour_and_the_spend_outside_the_doctor(github, monkeypatch):
    clock = iter([100.0, 7300.0])
    for caller in ("doctor.ci", "doctor.ci", "swarm.head"):
        github_reads.charge(caller, PULL, 100.0)
    for caller in ("a.first", "z.last", "z.last", "z.last"):
        github_reads.charge(caller, PULL, 200.0)
    github_reads.charge("swarm.checks", "graphql", 200.0)
    github_reads.charge("swarm.late", PULL, 7300.0)
    (github_reads.home() / "charged.jsonl").open("a").write("torn\n")
    github_reads.charge("swarm.after", PULL, 300.0)
    monkeypatch.setenv("GH_TOKEN", "another_login")
    github_reads.charge("swarm.head", PULL, 200.0)
    monkeypatch.delenv("GH_TOKEN")
    found = github_sampler.sample(
        120, 3600, run=rate_limit(iter([1000, 1600, 2200])), sleep=lambda pause: None, clock=lambda: next(clock)
    )
    assert found == {
        "since": 100.0,
        "until": 7300.0,
        "calls": 1200,
        "per_hour": 600,
        "outside_doctor_per_hour": 599,
        "unattributed_per_hour": 596,
        "callers": {"z.last": 3, "doctor.ci": 2, "swarm.head": 1, "a.first": 1, "swarm.after": 1},
    }
    assert list(found["callers"]) == ["z.last", "doctor.ci", "swarm.head", "a.first", "swarm.after"]


def test_the_sampler_reads_once_a_minute_and_reports_an_idle_window_as_zero(github):
    pauses, clock = [], iter([0.0, 3600.0])
    found = github_sampler.sample(60, run=rate_limit(iter([7] * 61)), sleep=pauses.append, clock=lambda: next(clock))
    assert pauses == [60] * 60
    assert found["calls"] == found["per_hour"] == 0
    assert found["outside_doctor_per_hour"] == found["unattributed_per_hour"] == 0


def test_a_window_too_short_to_time_counts_its_calls_as_one_hour(github):
    found = github_sampler.sample(1, run=rate_limit(iter([7, 9])), sleep=lambda pause: None, clock=lambda: 500.0)
    assert found["calls"] == found["per_hour"] == 2


def test_the_sampler_command_samples_the_window_it_is_given(monkeypatch, capsys):
    monkeypatch.setattr(github_sampler, "sample", lambda minutes, interval: {"minutes": minutes, "interval": interval})
    assert github_sampler.main(["--minutes", "5", "--interval", "30"]) == 0
    assert json.loads(capsys.readouterr().out) == {"minutes": 5, "interval": 30}
    assert github_sampler.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"minutes": 60, "interval": 60}
    with pytest.raises(SystemExit):
        github_sampler.main(["--help"])
    assert capsys.readouterr().out.startswith("usage: python -m scripts.swarm.github_sampler ")
