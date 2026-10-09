import pytest

from scripts.swarm_ledger import ledger_server, load_gate
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository


def test_p95_is_the_nearest_rank_sample():
    samples = [float(n) for n in range(1, 21)]
    assert load_gate.p95(samples) == 19.0
    assert load_gate.p95([0.5]) == 0.5


def test_writes_and_cpu_within_budget_pass():
    assert load_gate.verdict([0.1] * 100, load_gate.CPU_CORES, []) == []


@pytest.mark.parametrize(
    "writes, cores, errors, problem",
    [
        ([0.1] * 94 + [load_gate.WRITE_P95_S + 0.01] * 6, 0.2, [], "write p95"),
        ([0.1] * 100, load_gate.CPU_CORES + 0.01, [], "server CPU"),
        ([0.1] * 100, 0.2, ["ci@ab0000-0001: timed out"], "requests failed"),
        ([], 0.2, [], "no write completed"),
    ],
)
def test_a_broken_budget_or_a_failed_request_is_red(writes, cores, errors, problem):
    problems = load_gate.verdict(writes, cores, errors)
    assert any(problem in line for line in problems)


def test_the_load_covers_every_sweep_of_the_watch_loop_more_than_once():
    period = ledger_server.BIN_SWEEP_EVERY * 2.0
    assert load_gate.duration() >= load_gate.SWEEPS * period + period / 2


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
