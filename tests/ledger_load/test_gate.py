import inspect

import pytest

from scripts.swarm_ledger import ledger_server
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
from tests.ledger_load import gate as ledger_load


def test_p95_is_the_nearest_rank_sample():
    samples = [float(n) for n in range(1, 21)]
    assert ledger_load.p95(samples) == 19.0
    assert ledger_load.p95([0.5]) == 0.5


def test_writes_and_cpu_within_budget_pass():
    assert ledger_load.verdict([0.1] * 100, 100, ledger_load.CPU_CORES, []) == []


@pytest.mark.parametrize(
    "writes, cores, errors, problem",
    [
        ([0.1] * 94 + [ledger_load.WRITE_P95_S + 0.01] * 6, 0.2, [], "write p95"),
        ([0.1] * 100, ledger_load.CPU_CORES + 0.01, [], "server CPU"),
        ([0.1] * 100, 0.2, ["ci@ab0000-0001: timed out"], "requests failed"),
        ([], 0.2, [], "writes completed"),
        ([0.1] * 89, 0.2, [], "writes completed"),
    ],
)
def test_a_broken_budget_a_failed_request_or_a_shortfall_is_red(writes, cores, errors, problem):
    problems = ledger_load.verdict(writes, 100, cores, errors)
    assert any(problem in line for line in problems)


def test_cpu_is_judged_on_its_busiest_window_not_the_whole_run():
    calm = [(float(t), 0.2 * t) for t in range(60)]
    burst = [(60.0 + t, calm[-1][1] + 1.5 * (t + 1)) for t in range(ledger_load.CPU_WINDOW_S + 1)]
    assert ledger_load.busiest_window(calm) == pytest.approx(0.2)
    assert ledger_load.busiest_window(calm + burst) > ledger_load.CPU_CORES


def test_a_run_too_short_for_one_cpu_window_is_red():
    assert ledger_load.busiest_window([(0.0, 0.0), (1.0, 0.1)]) is None
    assert any("no 10s window" in line for line in ledger_load.verdict([0.1] * 100, 100, None, []))


def test_alerts_past_their_quiet_hour_left_open_after_the_load_are_named(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_load, "TASKS", 20)
    monkeypatch.setattr(ledger_load, "LEDGER_BYTES", 0)
    at = ledger_load.ALERT_QUIET_MS * 10
    ledger_load.store(tmp_path, ledger_load.full_size(at))
    stale = ledger_load.unexpired(tmp_path, at)
    assert stale and all(alert_id.startswith("al-") for alert_id in stale)


def test_the_load_covers_every_sweep_of_the_watch_loop_more_than_once():
    interval = inspect.signature(ledger_server.watch_ledgers).parameters["interval"].default
    period = ledger_server.BIN_SWEEP_EVERY * interval
    assert ledger_load.duration() >= ledger_load.SWEEPS * period + period / 2


def test_the_gate_refuses_to_run_outside_ci(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(ledger_load, "run", lambda folder: ran.append(folder) or [])
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit, match="CI only"):
        ledger_load.main(["--folder", str(tmp_path)])
    assert ran == []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert ledger_load.main(["--folder", str(tmp_path)]) == 0
    assert ran == [tmp_path]


def test_some_open_alerts_are_past_their_quiet_hour_so_the_expiry_sweep_has_work():
    at = 10 * ledger_load.ALERT_QUIET_MS
    alerts = [ledger_load.alert(n, at) for n in range(ledger_load.ALERTS)]
    assert any(ledger_server.ledger_alerts.expired(alert, at) for alert in alerts)
    assert not all(ledger_server.ledger_alerts.expired(alert, at) for alert in alerts if alert["state"] == "open")


def test_the_generated_ledger_reaches_the_target_size(monkeypatch):
    monkeypatch.setattr(ledger_load, "TASKS", 40)
    target = ledger_load.size(ledger_load.document(1)) + 200_000
    monkeypatch.setattr(ledger_load, "LEDGER_BYTES", target)
    doc = ledger_load.full_size(1)
    assert len(doc["tasks"]) == 40
    assert target <= ledger_load.size(doc) < target + 40 * 100


def test_the_scale_and_budgets_hold_the_operator_order():
    assert ledger_load.TASKS >= 1000
    assert ledger_load.LEDGER_BYTES >= 5_000_000
    assert ledger_load.CLIENTS >= 10
    assert ledger_load.WRITE_P95_S <= 2.0
    assert ledger_load.CPU_CORES <= 1.0


def test_the_stored_ledger_exports_as_generated_with_a_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_load, "TASKS", 20)
    monkeypatch.setattr(ledger_load, "LEDGER_BYTES", 0)
    doc = ledger_load.full_size(1)
    token = ledger_load.store(tmp_path, doc)
    exported = SQLiteLedgerRepository(tmp_path / DATABASE).export_document(ledger_load.SLUG)
    assert token
    assert len(exported["tasks"]) == 20
    assert exported["alerts"] == doc["alerts"]


def test_the_clients_together_write_headroom_times_the_live_peak_minute():
    per_minute = ledger_load.CLIENTS * 60 / ledger_load.write_every()
    assert per_minute == pytest.approx(ledger_load.LIVE_PEAK_WRITES_PER_MINUTE * ledger_load.HEADROOM)
    assert ledger_load.HEADROOM >= 2


def test_every_client_reads_one_single_resource_a_second_that_the_ledger_holds():
    doc = ledger_load.document(0)
    held = {f"{name}/{item['id']}" for name in ("tasks", "phases", "followups") for item in doc[name]}
    items = [ledger_load.single_items(n) for n in range(ledger_load.CLIENTS)]
    assert ledger_load.READS_PER_SECOND == 1.0
    assert items[1] == ["tasks/t100", "phases/p1", "followups/f-1"]
    assert all(set(paths) <= held for paths in items)
