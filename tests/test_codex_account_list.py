import base64
import json

from scripts import agents_quota, codex_router
from scripts import claude_quota_balancer as balancer
from scripts.codex_quota import CodexQuota
from scripts.hive import daemon
from scripts.swarm import capacity

KEPT = "nestor_coltatgmail_com"


def _jwt(account_id, email=""):
    def part(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    claims = {
        codex_router.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": account_id},
        codex_router.OPENAI_PROFILE_CLAIM: {"email": email},
    }
    return ".".join([part(b'{"alg":"RS256"}'), part(json.dumps(claims).encode()), "c2ln"])


def _environ(tmp_path):
    token = _jwt("acct-1", "nestor.colt@gmail.com")
    return {
        "HOME": str(tmp_path),
        "AH_CX_TOKEN_alpha": token,
        f"AH_CX_TOKEN_{KEPT}": token + "x",
        "AH_CX_TOKEN_gamma": _jwt("acct-2"),
    }


def test_the_hives_table_and_the_quota_table_list_the_same_deduplicated_codex_accounts(tmp_path, monkeypatch):
    environ = {**_environ(tmp_path), "AH_CX_TOKEN_keyed": "sk-placeholder"}
    weekly = CodexQuota(100, "pro", seven_day=balancer.QuotaWindow(used=10, resets_at=10**10))
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {a.name: weekly for a in pool})
    monkeypatch.setattr(codex_router, "probe", lambda *a, **kw: None)
    monkeypatch.setattr(capacity.account_sessions, "codex_sessions_by_account", lambda **kw: {"alpha": 2})
    monkeypatch.setattr(daemon, "claude_signed_in", lambda: False)
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(balancer, "discover_credentials", lambda environ: [])
    slots, quota = daemon._subscriptions(environ, {"claude": {}, "codex": {}}, 100 + 3600)
    hives = [slot["account"] for slot in slots if slot["harness"] == "codex"]
    rows = [row.name for row in capacity._codex(environ, 100 + 3600, True)]
    assert hives == rows == ["gamma", KEPT]
    assert sorted(key for key in quota if key.startswith("codex:")) == ["codex:gamma", f"codex:{KEPT}"]


def test_the_page_quota_table_shows_a_duplicate_as_its_own_row_with_a_duplicate_state(tmp_path, monkeypatch):
    from hooks.context import account_sessions

    environ = {**_environ(tmp_path), "AH_CX_TOKEN_delta": _jwt("acct-1") + "y"}
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    agents_quota._page_cache.clear()
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [])
    monkeypatch.setattr(balancer, "discover_credentials", lambda environ: [])
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda **kw: {})
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda **kw: {"alpha": 2})
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {})
    rows = agents_quota.page_quota(now=100.0)["rows"]
    codex = [row for row in rows if row["agent"] == "codex" and row["kind"] != "api"]
    assert [row["account"] for row in codex[:2]] == ["gamma", KEPT]
    assert agents_quota.DUPLICATE not in {row["state"] for row in codex[:2]}
    duplicates = {row["account"]: row for row in codex[2:]}
    assert sorted(duplicates) == ["alpha", "delta"]
    assert {name: row["sessions"] for name, row in duplicates.items()} == {"alpha": 2, "delta": 0}
    for row in duplicates.values():
        assert row["state"] == agents_quota.DUPLICATE
        name = row["account"]
        assert (
            row["source"]
            == f"same ChatGPT account as {KEPT}; sign in its own account with agentihooks codex login {name}"
        )
        assert row["kind"] == "subscription"
        assert row["identity"] == codex_router.fingerprint("acct-1")
        empty = ("five_hour_left", "seven_day_left", "seven_day_resets_at", "cap", "routing_left")
        assert [row[field] for field in empty] == [None] * len(empty)
    assert codex_router.routed_live([], {"alpha": 2, "other": 1}, dict(_environ(tmp_path))) == [
        codex_router.CodexAccount("other", "AH_CX_TOKEN_other")
    ]


def test_balance_lists_every_refused_token_account_with_its_state_and_login_command(tmp_path, monkeypatch):
    import time

    from hooks.context import account_sessions
    from tests.codex_fakes import DAY, jwt

    now = time.time()

    def token(account_id, exp):
        return jwt({"exp": int(exp), codex_router.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": account_id}})

    environ = {
        **_environ(tmp_path),
        "AH_CX_TOKEN_old": token("acct-3", now - 4 * DAY),
        "AH_CX_TOKEN_soon": token("acct-4", now + 60),
        "AH_CX_TOKEN_keyed": "sk-placeholder",
    }
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(account_sessions, "codex_sessions_by_account", lambda **kw: {"old": 1})
    monkeypatch.setattr(codex_router, "fresh_quotas", lambda *a, **kw: {})
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {})
    rows = {row.account: row for row in agents_quota._codex(now) if row.kind != "api"}
    assert sorted(rows) == ["alpha", "gamma", "keyed", KEPT, "old", "soon"]
    assert rows["alpha"].state == agents_quota.DUPLICATE
    assert rows["alpha"].source.endswith("agentihooks codex login alpha")
    assert rows["old"].state == agents_quota.EXPIRED
    assert rows["old"].source == "token expired: run agentihooks codex login old"
    assert rows["old"].sessions == 1
    assert rows["soon"].state == agents_quota.EXPIRING
    assert rows["soon"].source == codex_router.expiry_refusal(
        codex_router.CodexAccount("soon", "AH_CX_TOKEN_soon"), environ, now
    )
    assert "agentihooks codex login soon" in rows["soon"].source
    assert rows["keyed"].state == agents_quota.REFUSED
    assert "CODEX_API_KEY" in rows["keyed"].source
    for name in ("alpha", "old", "soon", "keyed"):
        assert (rows[name].cap, rows[name].routing_left, rows[name].seven_day_left) == (None, None, None)


def test_a_refused_token_account_replaces_the_row_the_table_lists_for_it(tmp_path):
    from tests.codex_fakes import DAY, jwt

    expired = jwt({"exp": 100 - 4 * DAY, codex_router.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": "acct-3"}})
    environ = {**_environ(tmp_path), "AH_CX_TOKEN_old": expired}
    claude = agents_quota.QuotaRow("claude", "old", "OPEN", 1, None, None, None, "")
    listed = agents_quota.QuotaRow("codex", "old", "UNKNOWN", 2, None, None, None, "")
    found = agents_quota.with_unrouted([claude, listed], environ, {"old": 2}, 100.0)
    assert [(row.agent, row.account, row.state) for row in found] == [
        ("claude", "old", "OPEN"),
        ("codex", "old", agents_quota.EXPIRED),
        ("codex", "alpha", agents_quota.DUPLICATE),
    ]
    assert found[1].sessions == 2
