import inspect

import pytest

from scripts.swarm_ledger import ledger_server, load_gate
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository


def test_p95_is_the_nearest_rank_sample():
    samples = [float(n) for n in range(1, 21)]
    assert load_gate.p95(samples) == 19.0
    assert load_gate.p95([0.5]) == 0.5


def test_writes_and_cpu_within_budget_pass():
    assert load_gate.verdict([0.1] * 100, 100, load_gate.CPU_CORES, []) == []


@pytest.mark.parametrize(
    "writes, cores, errors, problem",
    [
        ([0.1] * 94 + [load_gate.WRITE_P95_S + 0.01] * 6, 0.2, [], "write p95"),
        ([0.1] * 100, load_gate.CPU_CORES + 0.01, [], "server CPU"),
        ([0.1] * 100, 0.2, ["ci@ab0000-0001: timed out"], "requests failed"),
        ([], 0.2, [], "writes completed"),
        ([0.1] * 89, 0.2, [], "writes completed"),
    ],
)
def test_a_broken_budget_a_failed_request_or_a_shortfall_is_red(writes, cores, errors, problem):
    problems = load_gate.verdict(writes, 100, cores, errors)
    assert any(problem in line for line in problems)


def test_cpu_is_judged_on_its_busiest_window_not_the_whole_run():
    calm = [(float(t), 0.2 * t) for t in range(60)]
    burst = [(60.0 + t, calm[-1][1] + 1.5 * (t + 1)) for t in range(load_gate.CPU_WINDOW_S + 1)]
    assert load_gate.busiest_window(calm) == pytest.approx(0.2)
    assert load_gate.busiest_window(calm + burst) > load_gate.CPU_CORES


def test_a_run_too_short_for_one_cpu_window_is_red():
    assert load_gate.busiest_window([(0.0, 0.0), (1.0, 0.1)]) is None
    assert any("no 10s window" in line for line in load_gate.verdict([0.1] * 100, 100, None, []))


def test_alerts_past_their_quiet_hour_left_open_after_the_load_are_named(tmp_path, monkeypatch):
    monkeypatch.setattr(load_gate, "TASKS", 20)
    monkeypatch.setattr(load_gate, "LEDGER_BYTES", 0)
    at = load_gate.ALERT_QUIET_MS * 10
    load_gate.store(tmp_path, load_gate.full_size(at))
    stale = load_gate.unexpired(tmp_path, at)
    assert stale and all(alert_id.startswith("al-") for alert_id in stale)


def test_the_load_covers_every_sweep_of_the_watch_loop_more_than_once():
    interval = inspect.signature(ledger_server.watch_ledgers).parameters["interval"].default
    period = ledger_server.BIN_SWEEP_EVERY * interval
    assert load_gate.duration() >= load_gate.SWEEPS * period + period / 2


def test_the_gate_refuses_to_run_outside_ci(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit, match="CI only"):
        load_gate.main(["--folder", str(tmp_path)])


def test_some_open_alerts_are_past_their_quiet_hour_so_the_expiry_sweep_has_work():
    at = 10 * load_gate.ALERT_QUIET_MS
    alerts = [load_gate.alert(n, at) for n in range(load_gate.ALERTS)]
    assert any(ledger_server.ledger_alerts.expired(alert, at) for alert in alerts)
    assert not all(ledger_server.ledger_alerts.expired(alert, at) for alert in alerts if alert["state"] == "open")


def test_the_generated_ledger_reaches_the_target_size(monkeypatch):
    monkeypatch.setattr(load_gate, "TASKS", 40)
    target = load_gate.size(load_gate.document(1)) + 200_000
    monkeypatch.setattr(load_gate, "LEDGER_BYTES", target)
    doc = load_gate.full_size(1)
    assert len(doc["tasks"]) == 40
    assert target <= load_gate.size(doc) < target + 40 * 100


def test_the_scale_and_budgets_hold_the_operator_order():
    assert load_gate.TASKS >= 1000
    assert load_gate.LEDGER_BYTES >= 5_000_000
    assert load_gate.CLIENTS >= 10
    assert load_gate.WRITE_P95_S <= 2.0
    assert load_gate.CPU_CORES <= 1.0


def test_the_stored_ledger_exports_as_generated_with_a_token(tmp_path, monkeypatch):
    monkeypatch.setattr(load_gate, "TASKS", 20)
    monkeypatch.setattr(load_gate, "LEDGER_BYTES", 0)
    doc = load_gate.full_size(1)
    token = load_gate.store(tmp_path, doc)
    exported = SQLiteLedgerRepository(tmp_path / DATABASE).export_document(load_gate.SLUG)
    assert token
    assert len(exported["tasks"]) == 20
    assert exported["alerts"] == doc["alerts"]


def test_the_clients_together_write_headroom_times_the_live_peak_minute():
    per_minute = load_gate.CLIENTS * 60 / load_gate.write_every()
    assert per_minute == pytest.approx(load_gate.LIVE_PEAK_WRITES_PER_MINUTE * load_gate.HEADROOM)
    assert load_gate.HEADROOM >= 2
