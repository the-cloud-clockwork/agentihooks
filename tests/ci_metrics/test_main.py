import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_metrics import __main__ as entry
from scripts.ci_metrics import report, runs
from scripts.swarm import metrics_outbox

pytestmark = pytest.mark.unit

REPO = "the-cloud-clockwork/agentihooks"
PROMETHEUS = "http://prometheus:9090"
SINCE = datetime(2026, 11, 4, 13, tzinfo=UTC)
UNTIL = datetime(2026, 11, 4, 14, tzinfo=UTC)
ARGS = ["--repo", REPO, "--prometheus", PROMETHEUS, "--until", "2026-11-04T14:00:00Z"]
SETTINGS = metrics_outbox.Settings("http://clickhouse:8123", "swarm", "secret")


def window() -> runs.Pull:
    listed = [
        {
            "id": 1,
            "event": "pull_request",
            "head_branch": "feature",
            "conclusion": "success",
            "created_at": "2026-11-04T13:10:00Z",
            "head_sha": "a",
            "path": ".github/workflows/test.yml",
        }
    ]
    jobs = [
        {
            "id": 10,
            "run_id": 1,
            "conclusion": "success",
            "started_at": "2026-11-04T13:11:00Z",
            "completed_at": "2026-11-04T13:14:00Z",
        }
    ]
    merged = [
        {"number": 7, "branch": "feature", "created_at": "2026-11-04T13:05:00Z", "merged_at": "2026-11-04T13:40:00Z"}
    ]
    return runs.Pull(listed, jobs, merged, listed)


def wired(monkeypatch: pytest.MonkeyPatch, accept: bool) -> tuple[dict[str, bytes], list[tuple[Any, ...]]]:
    sent: dict[str, bytes] = {}
    reads: list[tuple[Any, ...]] = []

    def post(settings: metrics_outbox.Settings, query: str, body: bytes) -> bool:
        assert settings == SETTINGS
        sent[query] = body
        return accept

    def read(repo: str, since: datetime, until: datetime) -> runs.Pull:
        reads.append(("read", repo, since, until))
        return window()

    def pending(url: str, until: datetime) -> dict[str, float]:
        reads.append(("pending", url, until))
        return {"anton-oss": 2.5}

    monkeypatch.setattr(metrics_outbox, "post", post)
    monkeypatch.setattr(runs, "read", read)
    monkeypatch.setattr(runs, "pending", pending)
    monkeypatch.setenv(metrics_outbox.URL_ENV, SETTINGS.url)
    monkeypatch.setenv(metrics_outbox.USER_ENV, SETTINGS.user)
    monkeypatch.setenv(metrics_outbox.PASSWORD_ENV, SETTINGS.password)
    return sent, reads


def body(sent: dict[str, bytes], table: metrics_outbox.Table) -> list[dict[str, Any]]:
    insert = f"INSERT INTO {metrics_outbox.DATABASE}.{table.name} FORMAT JSONEachRow"
    return [json.loads(line) for line in sent[insert].decode().splitlines()]


def test_a_run_refuses_without_the_metrics_sink_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(metrics_outbox.URL_ENV, raising=False)
    monkeypatch.delenv(metrics_outbox.USER_ENV, raising=False)
    with pytest.raises(SystemExit) as refused:
        entry.main(ARGS)
    assert refused.value.code == "ci metrics refused: the metrics sink settings are not set"


def test_a_run_ships_its_rows_to_both_tables_and_prints_the_headline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent, reads = wired(monkeypatch, True)
    assert entry.main(ARGS) == 0
    assert reads == [("read", REPO, SINCE, UNTIL), ("pending", PROMETHEUS, UNTIL)]
    assert set(sent) == {
        report.COST.ddl(),
        report.COST.widen(),
        f"INSERT INTO {metrics_outbox.DATABASE}.ci_cost FORMAT JSONEachRow",
        report.LEAD.ddl(),
        report.LEAD.widen(),
        f"INSERT INTO {metrics_outbox.DATABASE}.ci_lead_time FORMAT JSONEachRow",
    }
    assert sorted((row["scope"], row["name"], row["runner_minutes"]) for row in body(sent, report.COST)) == [
        ("pool", "anton-oss", 0.0),
        ("tier", "pull request", 3.0),
        ("window", "all", 3.0),
    ]
    assert [(row["pr"], row["minutes"], row["ledger"]) for row in body(sent, report.LEAD)] == [(7, 30.0, REPO)]
    printed = capsys.readouterr().out.splitlines()
    assert json.loads(printed[0]) == {
        "since": "2026-11-04T13:00:00+00:00",
        "until": "2026-11-04T14:00:00+00:00",
        "rows": 4,
        "shipped": 4,
    }
    assert len(printed) == 2
    headline = json.loads(printed[1])
    assert (headline["merged"], headline["per_merged"], headline["lead_time"]["p50_minutes"]) == (1, 3.0, 30.0)
    assert headline["pending_runners"] == {"anton-oss": 2.5}


def test_a_run_fails_when_the_sink_takes_fewer_rows_than_it_built(monkeypatch: pytest.MonkeyPatch) -> None:
    wired(monkeypatch, False)
    with pytest.raises(SystemExit) as failed:
        entry.main(ARGS)
    assert failed.value.code == "ci metrics failed: shipped 0 of 4 rows to the metrics sink"


def test_without_an_end_the_run_measures_the_hour_two_hours_before_the_current_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ends: list[tuple[Any, ...]] = []
    _, reads = wired(monkeypatch, True)

    def end(until: str | None, lag_hours: int, now: datetime) -> datetime:
        ends.append((until, lag_hours, now.tzinfo))
        return UNTIL

    monkeypatch.setattr(entry, "end_of_window", end)
    assert entry.main(["--repo", REPO, "--prometheus", PROMETHEUS]) == 0
    assert ends == [(None, 2, UTC)]
    assert reads[0] == ("read", REPO, SINCE, UNTIL)


def test_the_end_of_the_window_is_the_given_time_or_the_lagged_hour() -> None:
    now = datetime(2026, 11, 4, 16, 20, tzinfo=UTC)
    assert entry.end_of_window(None, 2, now) == UNTIL
    assert entry.end_of_window("2026-11-04T09:00:00Z", 2, now) == datetime(2026, 11, 4, 9, tzinfo=UTC)


def test_lead_times_read_the_whole_branch_history() -> None:
    assert entry.OPENED == datetime.min.replace(tzinfo=UTC)


@pytest.mark.parametrize("missing", ["--repo", "--prometheus"])
def test_a_run_needs_the_repository_and_the_prometheus_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    wired(monkeypatch, True)
    given = {"--repo": REPO, "--prometheus": PROMETHEUS}
    argv = [part for flag, value in given.items() if flag != missing for part in (flag, value)]
    with pytest.raises(SystemExit) as refused:
        entry.main(argv)
    assert refused.value.code == 2
    assert f"the following arguments are required: {missing}" in capsys.readouterr().err


def test_the_lag_is_read_as_whole_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    lags: list[object] = []
    wired(monkeypatch, True)

    def end(until: str | None, lag_hours: int, now: datetime) -> datetime:
        lags.append(lag_hours)
        return UNTIL

    monkeypatch.setattr(entry, "end_of_window", end)
    assert entry.main(["--repo", REPO, "--prometheus", PROMETHEUS, "--lag-hours", "3"]) == 0
    assert lags == [3]


def test_the_outbox_is_flushed_at_the_current_time_in_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    flushed: list[int] = []
    wired(monkeypatch, True)

    def ship(
        settings: metrics_outbox.Settings, batches: dict[metrics_outbox.Table, list[dict[str, Any]]], now_ms: int
    ) -> int:
        flushed.append(now_ms)
        return sum(len(found) for found in batches.values())

    monkeypatch.setattr(entry, "ship", ship)
    monkeypatch.setattr(entry.time, "time", lambda: 1794000000.2504)
    assert entry.main(ARGS) == 0
    assert flushed == [1794000000250]


def test_the_temporary_outbox_takes_the_spool_file_name(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []
    original = metrics_outbox.Outbox

    def outbox(path: Path, sink: metrics_outbox.Settings) -> metrics_outbox.Outbox:
        paths.append(Path(path).name)
        return original(path, sink)

    wired(monkeypatch, True)
    monkeypatch.setattr(metrics_outbox, "Outbox", outbox)
    assert entry.main(ARGS) == 0
    assert paths == [metrics_outbox.spool_path().name]
