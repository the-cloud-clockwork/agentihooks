import importlib
import json

import pytest

from scripts.swarm import github_reads, mutation_wait
from tests.swarm.test_run_wait import ENDPOINT, JOBS, RUN, actions  # noqa: F401

READ = ["gh", "api", ENDPOINT]
TOKEN = "ghp_value_that_never_reaches_disk"


@pytest.fixture
def clock(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(mutation_wait, "_now", lambda: now[0])
    return now


def test_waits_on_one_running_run_share_one_read_until_it_ages(actions, clock):  # noqa: F811
    assert mutation_wait.run_resolution(RUN) == ""
    importlib.reload(github_reads)
    clock[0] += github_reads.RUN_TTL_S - 1
    assert mutation_wait.run_resolution(RUN) == ""
    assert mutation_wait.read(RUN) == {"status": "in_progress", "conclusion": None}
    assert actions["asked"] == [READ]
    clock[0] += 1
    actions["run"] = {"status": "completed", "conclusion": "success"}
    assert mutation_wait.run_resolution(RUN) == f"checks on {RUN}, now green"
    assert actions["asked"] == [READ, READ]


def test_a_finished_run_is_read_fresh_for_every_decision(actions, clock):  # noqa: F811
    actions["run"] = {"status": "completed", "conclusion": "success"}
    assert mutation_wait.run_resolution(RUN) == f"checks on {RUN}, now green"
    actions["run"] = {"status": "completed", "conclusion": "failure"}
    assert mutation_wait.run_resolution(RUN) == f"checks on {RUN}, now red; the run concluded failure"
    assert actions["asked"] == [READ, READ, ["gh", "api", JOBS]]


def test_a_cached_run_is_served_only_to_the_credential_source_that_read_it(actions, clock, monkeypatch):  # noqa: F811
    assert mutation_wait.run_resolution(RUN) == ""
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    assert mutation_wait.run_resolution(RUN) == ""
    assert mutation_wait.run_resolution(RUN) == ""
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.setenv("GH_CONFIG_DIR", "/elsewhere")
    assert mutation_wait.run_resolution(RUN) == ""
    assert actions["asked"] == [READ, READ, READ]


def test_an_unread_run_is_not_cached(actions, clock):  # noqa: F811
    actions["code"], actions["stderr"] = 1, b"HTTP 502: Bad Gateway"
    assert mutation_wait.run_resolution(RUN) == ""
    actions["code"] = 0
    assert mutation_wait.run_resolution(RUN) == ""
    assert actions["asked"] == [READ, READ]


def test_each_charged_request_counts_against_its_caller_without_token_values(actions, clock, monkeypatch):  # noqa: F811
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    assert mutation_wait.run_resolution(RUN) == ""
    assert mutation_wait.read(RUN) == {"status": "in_progress", "conclusion": None}
    clock[0] += github_reads.RUN_TTL_S
    assert mutation_wait.read(RUN) == {"status": "in_progress", "conclusion": None}
    clock[0] += github_reads.RUN_TTL_S
    actions["run"] = {"status": "completed", "conclusion": "failure"}
    assert mutation_wait.run_resolution(RUN) == f"checks on {RUN}, now red; the run concluded failure"
    assert github_reads.counts(0, clock[0] + 1) == {"waits.checks": 3, "waits.mutation": 1}
    assert github_reads.counts(1_000.0 + github_reads.RUN_TTL_S, clock[0]) == {"waits.mutation": 1}
    log = github_reads.home() / "charged.jsonl"
    assert TOKEN not in log.read_text()
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert rows[0] == {"at": 1_000.0, "caller": "waits.checks", "source": "GH_TOKEN", "endpoint": ENDPOINT}
    assert [row["endpoint"] for row in rows] == [ENDPOINT, ENDPOINT, ENDPOINT, JOBS]


def test_counts_skip_unreadable_lines(actions, clock):  # noqa: F811
    assert mutation_wait.run_resolution(RUN) == ""
    with (github_reads.home() / "charged.jsonl").open("a") as log:
        log.write("not json\n[]\n")
    assert github_reads.counts(0, 2_000) == {"waits.checks": 1}


def test_counts_are_empty_before_any_charged_request():
    assert github_reads.counts(0, 2_000) == {}
