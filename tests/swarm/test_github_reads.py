import importlib
import json

import pytest

from scripts.swarm import github_reads, mutation_wait
from tests.swarm.test_run_wait import ENDPOINT, JOBS, RUN, actions  # noqa: F401

READ = ["gh", "api", ENDPOINT]
TOKEN = "ghp_value_that_never_reaches_disk"


@pytest.fixture
def clock(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
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


def test_a_cached_run_is_served_only_to_the_credential_that_read_it(actions, clock, monkeypatch):  # noqa: F811
    assert mutation_wait.run_resolution(RUN) == ""
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    assert mutation_wait.run_resolution(RUN) == ""
    assert mutation_wait.run_resolution(RUN) == ""
    monkeypatch.setenv("GH_TOKEN", "another token")
    assert mutation_wait.run_resolution(RUN) == ""
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    assert mutation_wait.run_resolution(RUN) == ""
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.setenv("GH_CONFIG_DIR", "/elsewhere")
    assert mutation_wait.run_resolution(RUN) == ""
    assert actions["asked"] == [READ] * 4
    assert not any(TOKEN in path.name or TOKEN in path.read_text() for path in github_reads.home().iterdir())


@pytest.mark.parametrize(
    "environ, label",
    [
        ({}, "gh config default"),
        ({"GH_CONFIG_DIR": "/elsewhere"}, "gh config /elsewhere"),
        ({"GITHUB_TOKEN": TOKEN}, "GITHUB_TOKEN"),
        ({"GH_TOKEN": TOKEN, "GITHUB_TOKEN": TOKEN}, "GH_TOKEN"),
        ({"GH_TOKEN": "", "GITHUB_TOKEN": TOKEN}, "GITHUB_TOKEN"),
    ],
)
def test_the_credential_source_is_named_by_variable(clock, monkeypatch, environ, label):
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    assert github_reads.source() == label


def test_the_cache_lives_in_the_agentihooks_home(actions, clock, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    assert github_reads.home() == tmp_path / "github-reads"
    assert mutation_wait.run_resolution(RUN) == ""
    assert sorted(path.suffix for path in github_reads.home().iterdir()) == [".json", ".jsonl"]


def test_a_missing_agentihooks_home_is_created_for_the_cache_and_the_counts(actions, clock, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "missing" / "home"))
    assert mutation_wait.run_resolution(RUN) == ""
    assert mutation_wait.run_resolution(RUN) == ""
    assert actions["asked"] == [READ]
    assert github_reads.counts(0, 2_000) == {"waits.checks": 1}


def test_a_reader_that_charges_nothing_still_creates_the_missing_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "missing" / "home"))
    running = {"status": "in_progress"}
    assert github_reads.run("repos/org/repo/actions/runs/1", 1_000.0, lambda: running) == running
    assert github_reads.run("repos/org/repo/actions/runs/1", 1_001.0, pytest.fail) == running


def test_each_run_has_its_own_entry(actions, clock):  # noqa: F811
    other = RUN.replace("/123", "/456")
    assert mutation_wait.run_resolution(RUN) == ""
    assert mutation_wait.run_resolution(other) == ""
    assert actions["asked"] == [READ, ["gh", "api", ENDPOINT.replace("/123", "/456")]]


def test_a_corrupt_entry_is_read_again(actions, clock):  # noqa: F811
    assert mutation_wait.run_resolution(RUN) == ""
    for body in ("not json", "[]", "{}"):
        (entry,) = github_reads.home().glob("*.json")
        entry.write_text(body)
        assert mutation_wait.run_resolution(RUN) == ""
    assert actions["asked"] == [READ] * 4


def test_an_unwritable_home_still_reads_every_run(actions, clock, monkeypatch, tmp_path):  # noqa: F811
    blocked = tmp_path / "blocked"
    blocked.write_text("")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(blocked))
    assert mutation_wait.run_resolution(RUN) == ""
    actions["run"] = {"status": "completed", "conclusion": "success"}
    assert mutation_wait.run_resolution(RUN) == f"checks on {RUN}, now green"
    assert actions["asked"] == [READ, READ]
    assert github_reads.counts(0, 2_000) == {}


def test_reads_outside_the_waits_count_against_the_wait_reader(actions, clock):  # noqa: F811
    mutation_wait._api(ENDPOINT)
    assert github_reads.counts(0, 2_000) == {"mutation_wait": 1}


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
    github_reads.home().mkdir(parents=True, exist_ok=True)
    with (github_reads.home() / "charged.jsonl").open("a") as log:
        log.write('not json\n[]\n{}\n{"at": 1000.0}\n')
    assert mutation_wait.run_resolution(RUN) == ""
    assert github_reads.counts(0, 2_000) == {"waits.checks": 1}


def test_counts_are_empty_before_any_charged_request():
    assert github_reads.counts(0, 2_000) == {}


def test_nested_cache_home_retains_runs_snapshots_and_caller_counts(monkeypatch, tmp_path):
    for reader in ("run", "snapshot", "charge"):
        monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / reader / "nested" / "state"))
        if reader == "charge":
            github_reads.charge("doctor.ci", "jobs", 1_000)
            assert github_reads.counts(0, 1_001) == {"doctor.ci": 1}
        else:
            calls = []
            payload = {"status": "in_progress", "head": "first"}

            def fetch():
                calls.append(1)
                return payload

            read = getattr(github_reads, reader)
            assert read("first", 1_000, fetch) == payload
            assert read("first", 1_001, fetch) == payload
            assert len(calls) == 1


def test_snapshots_keep_distinct_resources_and_retry_partial_results(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    responses = iter([{"pull": "first"}, {"pull": "second"}, {"pull": None}, {"pull": "recovered"}])

    def fetch():
        return next(responses)

    assert github_reads.snapshot("first", 1_000, fetch) == {"pull": "first"}
    assert github_reads.snapshot("second", 1_000, fetch) == {"pull": "second"}
    assert github_reads.snapshot("first", 1_001, fetch) == {"pull": "first"}
    assert github_reads.snapshot("partial", 1_000, fetch) == {"pull": None}
    assert github_reads.snapshot("partial", 1_001, fetch) == {"pull": "recovered"}


@pytest.mark.parametrize("kind", ["job_log", "snapshot"])
def test_retained_readers_share_the_lock_with_an_existing_process(kind, monkeypatch, tmp_path):
    import fcntl
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "lock-compatibility-test")
    reader = getattr(github_reads, kind)
    payload = "saved" if kind == "job_log" else {"pull": "saved"}

    def read(fetch):
        return reader("resource", fetch) if kind == "job_log" else reader("resource", 1_000, fetch)

    assert read(lambda: payload) == payload
    suffix = "*.log" if kind == "job_log" else "*.json"
    entry = next(github_reads.home().glob(suffix))
    entry.unlink()
    original = fcntl.flock
    contended = []
    attempted = threading.Event()

    def acquire(fd, operation):
        try:
            original(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError:
            contended.append(True)
            attempted.set()
            original(fd, operation)
        else:
            attempted.set()

    fetches = []
    with entry.with_suffix(".lock").open("a") as holder, ThreadPoolExecutor() as pool:
        original(holder, fcntl.LOCK_EX)
        monkeypatch.setattr(fcntl, "flock", acquire)
        future = pool.submit(read, lambda: fetches.append(1) or payload)
        try:
            assert attempted.wait(5)
            if kind == "job_log":
                entry.write_text(payload)
            else:
                entry.write_text(json.dumps({"at": 1_000, "snapshot": payload}))
        finally:
            original(holder, fcntl.LOCK_UN)
        assert future.result(timeout=5) == payload
    assert contended == [True]
    assert fetches == []


def response(status, body="", **headers):
    fields = "\r\n".join(f"{key}: {value}" for key, value in headers.items())
    return f"HTTP/2.0 {status}\r\n{fields}\r\n\r\n{body}"


def test_conditional_pages_share_reads_then_revalidate_without_charging_unchanged_data(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "conditional-test-token")
    now = [1_000.0]
    monkeypatch.setattr(github_reads.time, "time", lambda: now[0])
    calls = []
    outputs = iter(
        [
            response(200, '{"jobs": [{"id": 1}]}', ETag='"one"'),
            response(304),
            response(200, '{"jobs": [{"id": 2}]}', ETag='"two"'),
        ]
    )

    def fetch(*args):
        calls.append(args)
        return next(outputs)

    endpoint = "repos/o/r/actions/runs/7/jobs?per_page=100"
    assert github_reads.conditional_pages("lost_runner.jobs", endpoint, fetch) == [{"jobs": [{"id": 1}]}]
    now[0] += 59
    assert github_reads.conditional_pages("another", endpoint, fetch) == [{"jobs": [{"id": 1}]}]
    assert len(calls) == 1
    now[0] += 1
    assert github_reads.conditional_pages("lost_runner.jobs", endpoint, fetch) == [{"jobs": [{"id": 1}]}]
    assert calls[-1] == (endpoint, "--include", "-H", 'If-None-Match: "one"')
    assert github_reads.counts(1_000, 1_061) == {"lost_runner.jobs": 1}
    now[0] += 60
    assert github_reads.conditional_pages("lost_runner.jobs", endpoint, fetch) == [{"jobs": [{"id": 2}]}]
    assert github_reads.counts(1_000, 1_121) == {"lost_runner.jobs": 2}


def test_conditional_pages_keep_cached_links_on_304_and_drop_removed_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    now = [1_000.0]
    monkeypatch.setattr(github_reads.time, "time", lambda: now[0])
    first = "repos/o/r/jobs?per_page=1"
    second = "https://api.github.com/repos/o/r/jobs?per_page=1&page=2"
    outputs = iter(
        [
            response(200, '{"jobs": [1]}', ETag='"one"', Link=f'<{second}>; rel="next"'),
            response(200, '{"jobs": [2]}', ETag='"two"'),
            response(304),
            response(304),
            response(200, '{"jobs": [3]}', ETag='"three"'),
        ]
    )
    calls = []

    def fetch(*args):
        calls.append(args)
        return next(outputs)

    assert github_reads.conditional_pages("jobs", first, fetch) == [{"jobs": [1]}, {"jobs": [2]}]
    now[0] += 60
    assert github_reads.conditional_pages("jobs", first, fetch) == [{"jobs": [1]}, {"jobs": [2]}]
    assert calls[3] == (second, "--include", "-H", 'If-None-Match: "two"')
    now[0] += 60
    assert github_reads.conditional_pages("jobs", first, fetch) == [{"jobs": [3]}]
    assert len(calls) == 5


def test_conditional_pages_without_etags_or_with_a_new_credential_read_fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    now = [1_000.0]
    monkeypatch.setattr(github_reads.time, "time", lambda: now[0])
    calls = []

    def fetch(*args):
        calls.append(args)
        return response(200, '{"jobs": []}')

    for token in ("first-test-token", "second-test-token", "first-test-token"):
        monkeypatch.setenv("GH_TOKEN", token)
        assert github_reads.conditional_pages("jobs", "resource", fetch) == [{"jobs": []}]
        now[0] += 60
    assert calls == [("resource", "--include")] * 3


def test_conditional_pages_accept_a_304_gh_error_but_propagate_other_errors(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    now = [1_000.0]
    monkeypatch.setattr(github_reads.time, "time", lambda: now[0])
    outputs = iter(
        [
            response(200, '{"jobs": []}', ETag='"one"'),
            subprocess.CalledProcessError(1, ["gh"], output=response(304)),
            subprocess.CalledProcessError(1, ["gh"], output=response(403, '{"message": "refused"}')),
        ]
    )

    def fetch(*args):
        found = next(outputs)
        if isinstance(found, Exception):
            raise found
        return found

    assert github_reads.conditional_pages("jobs", "resource", fetch) == [{"jobs": []}]
    now[0] += 60
    assert github_reads.conditional_pages("jobs", "resource", fetch) == [{"jobs": []}]
    now[0] += 60
    with pytest.raises(subprocess.CalledProcessError):
        github_reads.conditional_pages("jobs", "resource", fetch)
