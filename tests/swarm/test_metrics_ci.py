import json

import pytest

from scripts.swarm import metrics_ci, metrics_outbox, test_signatures

LOG = "\n".join(
    (
        "2026-10-09T07:05:01.1Z [gw1] [ 40%] FAILED tests/a_test.py::test_spool",
        "2026-10-09T07:05:02.1Z [gw2] [ 41%] PASSED tests/a_test.py::test_other",
        "2026-10-09T07:09:00.1Z FAILED tests/a_test.py::test_spool - AssertionError: assert '/tmp/pytest-3/x' == 'y'",
        "2026-10-09T07:09:00.2Z FAILED tests/b_test.py::test_key - KeyError: 'missing'",
    )
)
RUN = {
    "id": 7,
    "event": "pull_request",
    "status": "completed",
    "conclusion": "failure",
    "head_branch": "eng-1",
    "head_sha": "abc123",
    "created_at": "2026-10-09T07:00:00Z",
    "run_started_at": "2026-10-09T07:00:30Z",
    "updated_at": "2026-10-09T07:12:00Z",
    "run_attempt": 2,
}


def _job(job_id, name, created, started, completed, conclusion="success"):
    return {
        "id": job_id,
        "name": name,
        "created_at": f"2026-10-09T07:{created}Z",
        "started_at": f"2026-10-09T07:{started}Z",
        "completed_at": f"2026-10-09T07:{completed}Z",
        "conclusion": conclusion,
        "html_url": "https://github.com/o/r/actions/runs/7/job/1",
    }


JOBS = [
    _job(11, "unit (3.12, 1)", "00:40", "01:10", "09:30", "failure"),
    _job(12, "unit (3.12, 2)", "00:40", "00:50", "08:00"),
    _job(13, "Gate — Required", "10:00", "10:05", "10:30", "failure"),
]


def test_a_run_yields_one_gate_row_with_queue_and_run_seconds():
    rows = metrics_ci.rows("sw", RUN, JOBS, {11: LOG})
    assert rows[metrics_ci.RUNS] == [
        {
            "event_id": "ci-run:7:2",
            "ledger": "sw",
            "ts_ms": 1_791_529_830_000,
            "plan": "",
            "phase": "",
            "slice": "",
            "task": "",
            "workflow": "test.yml",
            "job": "Gate — Required",
            "event": "pull_request",
            "branch": "eng-1",
            "head_sha": "abc123",
            "run_id": 7,
            "attempt": 2,
            "conclusion": "failure",
            "queue_s": 30.0,
            "run_s": 600.0,
        }
    ]


def test_each_stage_carries_its_wall_time_budget_and_longest_pickup():
    rows = metrics_ci.rows("sw", RUN, JOBS, {})
    stages = {row["stage"]: row for row in rows[metrics_ci.STAGES]}
    assert set(stages) == {"unit", "gate-required"}
    assert (stages["unit"]["seconds"], stages["unit"]["budget"], stages["unit"]["pickup_s"]) == (530.0, 300, 30.0)
    assert stages["unit"]["event_id"] == "ci-stage:7:2:unit"


def test_failed_tests_are_rows_with_their_message_and_signature_once_each():
    failures = metrics_ci.rows("sw", RUN, JOBS, {11: LOG})[metrics_ci.FAILURES]
    found = {(row["test_id"], row["message"], row["job"]) for row in failures}
    assert found == {
        ("tests/a_test.py::test_spool", "AssertionError: assert '/tmp/pytest-3/x' == 'y'", "unit (3.12, 1)"),
        ("tests/b_test.py::test_key", "KeyError: 'missing'", "unit (3.12, 1)"),
    }
    spool = next(row for row in failures if row["test_id"].endswith("test_spool"))
    assert spool["event_id"] == "ci-fail:7:2:11:tests/a_test.py::test_spool"
    assert spool["signature"] == test_signatures.signature(spool["test_id"], spool["message"])


def test_a_failure_without_a_summary_line_still_has_a_signature():
    log = "2026-10-09T07:05:01.1Z [gw1] [ 40%] FAILED tests/a_test.py::test_spool"
    (row,) = metrics_ci.rows("sw", RUN, JOBS, {11: log})[metrics_ci.FAILURES]
    assert (row["message"], len(row["signature"])) == ("", 16)


def test_a_run_whose_gate_never_finished_measures_to_its_last_update():
    jobs = [j for j in JOBS if j["name"] != "Gate — Required"]
    (row,) = metrics_ci.rows("sw", RUN, jobs, {})[metrics_ci.RUNS]
    assert (row["job"], row["run_s"], row["ts_ms"]) == ("", 690.0, 1_791_529_920_000)


def test_every_row_passes_its_table_check():
    for table, found in metrics_ci.rows("sw", RUN, JOBS, {11: LOG}).items():
        for row in found:
            table.check(row)


def test_metered_runs_are_finished_pull_requests_and_dev_pushes():
    push = {**RUN, "id": 8, "event": "push", "head_branch": "dev"}
    other_push = {**RUN, "id": 9, "event": "push", "head_branch": "eng-2"}
    running = {**RUN, "id": 10, "status": "in_progress"}
    release = {**RUN, "id": 11, "head_branch": "dev"}
    push_running = {**push, "id": 12, "status": "in_progress"}
    push_cancelled = {**push, "id": 13, "conclusion": "cancelled"}
    runs = [RUN, push, other_push, running, release, push_running, push_cancelled]
    assert [r["id"] for r in metrics_ci.metered(runs)] == [7, 8]


def test_a_summary_message_survives_a_later_bare_line_for_the_same_test():
    log = "\n".join(
        (
            "2026-10-09T07:09:00.1Z FAILED tests/a_test.py::test_spool - KeyError: 'k'",
            "2026-10-09T07:09:01.1Z [gw1] [ 40%] FAILED tests/a_test.py::test_spool",
        )
    )
    (row,) = metrics_ci.rows("sw", RUN, JOBS, {11: log})[metrics_ci.FAILURES]
    assert row["message"] == "KeyError: 'k'"


def test_a_stage_without_a_budget_or_a_started_job_records_zero():
    jobs = [
        {**_job(14, "custom", "00:40", "00:41", "01:00"), "started_at": None},
        _job(15, "custom (2)", "00:40", "02:00", "03:00", "skipped"),
    ]
    (stage,) = metrics_ci.rows("sw", RUN, jobs, {})[metrics_ci.STAGES]
    assert (stage["stage"], stage["budget"], stage["pickup_s"]) == ("custom", 0, 0.0)


@pytest.fixture
def spool(tmp_path, monkeypatch):
    path = tmp_path / "outbox.sqlite"
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: path)
    return path


def test_ship_appends_rows_to_the_outbox_and_flushes(spool, monkeypatch):
    sent = []
    monkeypatch.setattr(metrics_outbox, "post", lambda sink, query, body: sent.append((query, body)) or True)
    rows = metrics_ci.rows("sw", RUN, JOBS, {11: LOG})
    env = {metrics_outbox.URL_ENV: "http://ch", metrics_outbox.USER_ENV: "ins"}
    assert metrics_ci.ship(1_791_530_600_000, rows, env) == []
    inserts = [body for query, body in sent if query.startswith("INSERT INTO swarm.ci_failures")]
    assert [json.loads(line)["test_id"] for line in inserts[0].decode().splitlines()] == [
        "tests/a_test.py::test_spool",
        "tests/b_test.py::test_key",
    ]


def test_ship_reports_an_outbox_it_cannot_open(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: blocker / "outbox.sqlite")
    env = {metrics_outbox.URL_ENV: "http://ch", metrics_outbox.USER_ENV: "ins"}
    (error,) = metrics_ci.ship(1, {}, env)
    assert error.startswith("metrics outbox failed:")


@pytest.mark.parametrize("env", [{}, {metrics_outbox.URL_ENV: "http://ch"}])
def test_ship_does_nothing_while_the_sink_is_off(spool, env):
    assert metrics_ci.ship(1, {metrics_ci.RUNS: [{"bad": 1}]}, env) == []
    assert not spool.exists()


def test_a_run_whose_rows_break_their_table_is_refused():
    with pytest.raises(ValueError, match="ci_runs.branch"):
        metrics_ci.rows("sw", {**RUN, "head_branch": None}, JOBS, {})
