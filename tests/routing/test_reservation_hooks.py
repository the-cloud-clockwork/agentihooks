import pytest

from scripts import claude_quota_balancer as balancer
from scripts import codex_router
from scripts.codex_quota import CodexQuota
from scripts.routing import reservation
from scripts.routing.master_account import MasterAccount
from scripts.swarm import capacity, quota_view

ONE = reservation.Reservation("claude", "main", 1)
REASON = "reserved for 1 live master: workers stop at 70% of the week"


def _row(name="main", week_left=30.0, harness="claude", **extra):
    return capacity.Account(harness, name, "OPEN", 1, 80.0, week_left, 6, **extra)


def _held_for(harness, slug):
    return lambda asked, environ: reservation.Reservation(harness, slug, 1) if asked == harness else None


def test_capacity_accounts_reserve_the_declared_account_per_harness(monkeypatch):
    claude, codex = _row(), _row(harness="codex")
    monkeypatch.setattr(capacity, "_claude", lambda environ, now: [claude])
    monkeypatch.setattr(capacity, "_codex", lambda environ, now, refresh: [codex])
    monkeypatch.setattr(reservation, "current", _held_for("claude", "main"))
    rows = capacity.accounts({}, 0.0, refresh=False)
    assert [(row.harness, row.cap, row.reserved) for row in rows] == [("claude", 0, REASON), ("codex", 6, "")]


def test_row_marks_carry_the_reservation_reason(monkeypatch):
    from scripts.routing import master_account

    declared = MasterAccount("claude", "main")
    monkeypatch.setattr(master_account, "load", lambda environ: {"claude": declared})
    monkeypatch.setattr(reservation, "current", _held_for("claude", "main"))
    assert master_account.row_marks("now", {}) == balancer.RowMarks("now", declared, REASON)
    monkeypatch.setattr(reservation, "current", lambda harness, environ: None)
    assert master_account.row_marks("now", {}) == balancer.RowMarks("now", declared, "")


def test_capacity_closes_the_master_account_for_workers():
    rows = capacity.reserve([_row(), _row(week_left=30.1), _row("other"), _row(harness="codex")], ONE)
    assert [(row.cap, row.state, row.reserved) for row in rows] == [
        (0, "CLOSED", REASON),
        (6, "OPEN", ""),
        (6, "OPEN", ""),
        (6, "OPEN", ""),
    ]
    assert capacity.reserve([_row(week_left=None)], ONE)[0].cap == 6
    assert capacity.reserve([_row(kind="api")], ONE)[0].cap == 6
    assert capacity.reserve([_row()], None)[0].cap == 6


def test_record_keeps_rows_without_a_reservation_unchanged():
    assert "reserved" not in capacity.record(_row())
    assert capacity.record(_row(reserved=REASON))["reserved"] == REASON
    assert capacity.record(_row(kind="api", reserved=REASON))["reserved"] == REASON
    assert "reserved" not in capacity.record(_row(kind="api"))


def test_quota_view_names_the_reservation():
    row = capacity.record(capacity.reserve([_row()], ONE)[0])
    line = quota_view._account_line(row, [row], 0)
    assert line.endswith(f"routing 30% left, {REASON}  sessions 1")
    assert ", reserved" not in quota_view._account_line(capacity.record(_row()), [row], 0)


def _result(name, week_used):
    return balancer.ProbeResult(
        name, "allowed", "NORMAL", 30.0, balancer.QuotaWindow(used=10.0), balancer.QuotaWindow(used=week_used)
    )


def test_claude_token_source_caps_workers_on_the_master_account():
    source = balancer.ClaudeTokenSource(held=ONE)
    assert source.cap(_result("main", 70.0), 0.0) == 0
    assert source.cap(_result("main", 69.9), 0.0) == 6
    assert source.cap(_result("other", 90.0), 0.0) == 6
    assert balancer.ClaudeTokenSource().cap(_result("main", 70.0), 0.0) == 6


def test_codex_source_caps_workers_on_the_master_account():
    held = reservation.Reservation("codex", "default", 2)
    source = codex_router.CodexAccountSource(held=held)
    week = balancer.QuotaWindow(used=60.0)
    reading = CodexQuota(0.0, "pro", balancer.QuotaWindow(used=10.0), week)
    assert source._cap("default", reading, 0.0) == 0
    assert source._cap("spare", reading, 0.0) == codex_router.account_cap(reading, 0.0)
    assert source._cap("default", None, 0.0) is None
    assert codex_router.CodexAccountSource()._cap("default", reading, 0.0) == codex_router.account_cap(reading, 0.0)


def test_balance_table_names_the_reservation():
    master = MasterAccount("claude", "main")
    marks = balancer.RowMarks("", master, REASON)
    table = balancer.render_table([_result("main", 75.0)], now=0, marks=marks)
    assert table.splitlines()[-2:] == ["", f"main: {REASON}"]
    assert REASON not in balancer.render_table([_result("main", 75.0)], now=0, marks=balancer.RowMarks("", master))
    assert REASON not in balancer.render_table(
        [_result("main", 75.0)], now=0, marks=balancer.RowMarks("", None, REASON)
    )


MASTER_ENV = {"AGENTIHOOKS_AGENT_NAME": "master@1"}
WORKER_ENV = {"AGENTIHOOKS_AGENT_NAME": "engineer@1"}


def test_claude_routing_skips_the_master_account_for_workers_only(monkeypatch, tmp_path):
    monkeypatch.setattr(reservation, "current", _held_for("claude", "MAIN"))
    monkeypatch.setattr(
        balancer, "collect_results", lambda *a, **k: ([_result("MAIN", 75.0), _result("SPARE", 30.0)], "live")
    )
    tokens = {"AH_CC_TOKEN_MAIN": "main-secret", "AH_CC_TOKEN_SPARE": "spare-secret"}

    def chosen(environ, sessions):
        decision = balancer.select_credential(
            {**tokens, **environ}, cache_file=tmp_path / "cache.json", sessions=sessions, now=0
        )
        return decision.credential.env_name

    assert chosen(WORKER_ENV, {"MAIN": 0}) == "AH_CC_TOKEN_SPARE"
    assert chosen(MASTER_ENV, {"SPARE": 6}) == "AH_CC_TOKEN_MAIN"


def test_codex_routing_skips_the_master_account_for_workers_only(monkeypatch):
    monkeypatch.setattr(reservation, "current", _held_for("codex", "default"))
    quotas = {"default": CodexQuota(0.0, "team", balancer.QuotaWindow(20.0), balancer.QuotaWindow(80.0))}
    pool = [codex_router.CodexAccount("default")]
    account, _, _ = codex_router.select(pool, quotas, {}, 0.0, environ=MASTER_ENV)
    assert account.name == "default"
    with pytest.raises(codex_router.RoutingError):
        codex_router.select(pool, quotas, {}, 0.0, environ=WORKER_ENV)
