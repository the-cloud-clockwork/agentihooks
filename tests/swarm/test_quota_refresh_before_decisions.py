from scripts import claude_quota_balancer as balancer
from scripts import codex_router
from scripts.codex_quota import CodexQuota
from scripts.swarm import capacity, drain_watch, quota_handoff, quota_view
from scripts.swarm.health.findings import Limits

NOW = 1_800_000_000.0
STALE_AT = NOW - 20 * 60
WEEK_RESET = int(NOW) + 3 * 86_400
FIVE_RESET = int(NOW) + 3_600


def _decisions(row: capacity.Account) -> tuple:
    stored = capacity.record(row)
    return (
        row.state,
        row.week_left,
        stored["observed_at"],
        drain_watch.draining(stored, Limits(), NOW),
        quota_handoff.trigger(row, quota_handoff.Thresholds()),
        quota_view.lines({**_decision(), "accounts": [stored]}, int(NOW * 1000))[1].split("  ")[2],
    )


def _decision() -> dict:
    lanes = {"eng": 0, "ci": 0, "plan": 0}
    return {"configured": lanes, "effective": lanes, "reason": "test", "at": int(NOW * 1000)}


def test_a_stale_claude_reading_is_read_live_before_the_cap_and_drain_decisions(monkeypatch, tmp_path):
    env = {"AGENTIHOOKS_HOME": str(tmp_path), "AH_CC_TOKEN_alpha": "fake-alpha"}
    drained = balancer.ProbeResult(
        "alpha",
        "allowed_warning",
        "DRAIN",
        5.0,
        balancer.QuotaWindow(1.0, FIVE_RESET),
        balancer.QuotaWindow(95.0, WEEK_RESET),
    )
    live = balancer.ProbeResult(
        "alpha",
        "allowed",
        "NORMAL",
        80.0,
        balancer.QuotaWindow(1.0, FIVE_RESET),
        balancer.QuotaWindow(20.0, WEEK_RESET),
    )
    entry = {"observed_at": STALE_AT, "result": balancer._result_data(drained)}
    cache = {"version": 1, "modes": {"normal": {"accounts": {"AH_CC_TOKEN_alpha": entry}}}}
    balancer._write_cache(tmp_path / "claude-router-cache.json", cache)
    probed = []
    monkeypatch.setattr(balancer, "probe_credentials", lambda found, *args: probed.extend(found) or [live])
    monkeypatch.setattr(capacity.account_sessions, "sessions_by_account", lambda **_: {})
    monkeypatch.setattr(capacity, "_api", lambda *args: [])
    [row] = capacity._claude(env, NOW)
    assert [credential.account for credential in probed] == ["alpha"]
    assert _decisions(row) == ("OPEN", 80.0, NOW, False, "", "routing 80% left, read 0m ago")


def test_a_stale_codex_reading_is_read_live_before_the_cap_and_drain_decisions(monkeypatch):
    drained = CodexQuota(STALE_AT, "pro", seven_day=balancer.QuotaWindow(95.0, WEEK_RESET))
    live = CodexQuota(NOW, "pro", seven_day=balancer.QuotaWindow(20.0, WEEK_RESET))
    pool = [codex_router.CodexAccount("alpha", "AH_CX_TOKEN_alpha")]
    probed = []
    monkeypatch.setattr(codex_router, "accounts", lambda environ: pool)
    monkeypatch.setattr(codex_router, "quotas", lambda accounts, environ: {a.name: drained for a in accounts})
    monkeypatch.setattr(codex_router, "probe", lambda account, environ, run: probed.append(account.name) or live)
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity, "_api", lambda *args: [])
    [row] = capacity._codex({"AH_CX_TOKEN_alpha": "fake-alpha"}, NOW, True)
    assert probed == ["alpha"]
    assert _decisions(row) == ("OPEN", 80.0, NOW, False, "", "routing 80% left, read 0m ago")


def test_a_fresh_codex_reading_past_the_drain_line_still_drains_and_warns(monkeypatch):
    drained = CodexQuota(NOW - 60, "pro", seven_day=balancer.QuotaWindow(95.0, WEEK_RESET))
    pool = [codex_router.CodexAccount("alpha", "AH_CX_TOKEN_alpha")]
    monkeypatch.setattr(codex_router, "accounts", lambda environ: pool)
    monkeypatch.setattr(codex_router, "quotas", lambda accounts, environ: {a.name: drained for a in accounts})
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(capacity, "_api", lambda *args: [])
    [row] = capacity._codex({"AH_CX_TOKEN_alpha": "fake-alpha"}, NOW, False)
    assert _decisions(row) == ("DRAIN", 5.0, NOW - 60, True, "week", "routing 5% left, read 1m ago")
