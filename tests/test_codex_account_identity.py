import base64
import hashlib
import json
import os

import pytest

from hooks import hook_manager
from scripts import agents_quota, codex_quota, codex_rollouts, codex_router
from scripts.claude_quota_balancer import QuotaWindow
from scripts.codex_router import CodexAccount

NOW = 2_000_000_000.0
ALPHA = "11111111-1111-1111-1111-111111111111"
BETA = "22222222-2222-2222-2222-222222222222"
OWN = "33333333-3333-3333-3333-333333333333"


def _jwt(account_id, session=""):
    def part(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    claims = {codex_router.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": account_id}, "sid": session}
    return ".".join([part(b'{"alg":"RS256"}'), part(json.dumps(claims).encode()), "c2ln"])


def _default_login(home, account_id):
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    (home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": _jwt(account_id)}}))


def _rollout(home, session, used, observed="2026-10-04T10:00:00Z"):
    limits = {
        "limit_id": "codex",
        "primary": {"used_percent": used, "window_minutes": 10080, "resets_at": 2_100_000_000},
        "plan_type": "pro",
    }
    event = {"timestamp": observed, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}
    path = home / ".codex" / "sessions" / "2026" / "10" / "04" / f"rollout-2026-10-04T10-00-00-{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event) + "\n")
    return path


def test_the_fingerprint_is_a_short_hash_that_never_shows_the_account_id():
    assert codex_router.fingerprint("acct-secret") == hashlib.sha256(b"acct-secret").hexdigest()[:8]
    assert len(codex_router.fingerprint("acct-secret")) == 8
    assert "acct" not in codex_router.fingerprint("acct-secret")
    assert codex_router.fingerprint("") == ""


def test_identity_reads_the_token_claim_or_the_default_login_and_never_an_api_key(tmp_path):
    _default_login(tmp_path, "acct-default")
    environ = {"HOME": str(tmp_path), "AH_CX_TOKEN_ncgma": _jwt("acct-ncgma"), "AH_CX_TOKEN_bare": "at-plain"}
    assert codex_router.identity(CodexAccount("ncgma", "AH_CX_TOKEN_ncgma"), environ) == "acct-ncgma"
    assert codex_router.identity(CodexAccount("default"), environ) == "acct-default"
    assert codex_router.identity(CodexAccount("bare", "AH_CX_TOKEN_bare"), environ) == ""
    assert codex_router.identity(CodexAccount("api", key_env="CODEX_API_KEY"), environ) == ""
    assert codex_router.identity(CodexAccount("default"), {"HOME": str(tmp_path / "none")}) == ""


def test_a_token_account_repeating_an_earlier_chatgpt_account_is_refused_with_a_warning(tmp_path, capsys):
    environ = {
        "HOME": str(tmp_path),
        "AH_CX_TOKEN_alpha": _jwt("acct-1"),
        "AH_CX_TOKEN_beta": _jwt("acct-1", "second login"),
        "AH_CX_TOKEN_gamma": _jwt("acct-2"),
    }
    tokens = codex_router.token_accounts(environ)
    assert codex_router.duplicates(tokens, environ) == {"beta": "alpha"}
    pool = codex_router.CodexAccountSource(lambda *a, **k: None).pool(environ)
    assert [account.name for account in pool] == ["alpha", "gamma"]
    assert codex_router.identity_warnings(environ) == [
        "AH_CX_TOKEN_beta is the same ChatGPT account as alpha; it is not routed"
    ]
    assert codex_router.identity_warnings({"AH_CX_TOKEN_alpha": _jwt("acct-1")}) == []
    output = "\n".join(codex_router.identity_warnings(environ))
    assert "acct-1" not in output


def test_the_default_login_is_never_counted_as_a_token_duplicate(tmp_path):
    _default_login(tmp_path, "acct-1")
    environ = {"HOME": str(tmp_path), "AH_CX_TOKEN_alpha": _jwt("acct-1")}
    pool = [CodexAccount("default"), CodexAccount("alpha", "AH_CX_TOKEN_alpha")]
    assert codex_router.duplicates(pool, environ) == {}


def test_accounts_without_a_token_never_count_as_duplicates_of_each_other(tmp_path):
    environ = {"HOME": str(tmp_path)}
    pool = [CodexAccount("default"), CodexAccount("api", key_env="CODEX_API_KEY")]
    assert codex_router.duplicates(pool, environ) == {}
    assert codex_router.identity(CodexAccount("gone", "AH_CX_TOKEN_gone"), environ) == ""


def test_the_launcher_warns_on_stderr_about_a_duplicate_token_account(tmp_path, monkeypatch, capsys):
    environ = {
        "HOME": str(tmp_path),
        "AH_CX_TOKEN_alpha": _jwt("acct-1"),
        "AH_CX_TOKEN_beta": _jwt("acct-1", "second login"),
    }
    monkeypatch.setattr(codex_router, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(codex_router, "quotas", lambda accounts, environ: {})
    monkeypatch.setattr(codex_router.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("scripts.deps_preflight.refresh_codex_wrapper", lambda name: None)
    seen = []
    rc = codex_router.main(["--route", "alpha"], environ=environ, execvpe=lambda *a: seen.append(a[0]))
    assert (rc, seen) == (0, ["/usr/bin/codex"])
    assert capsys.readouterr().err == (
        "agentihooks codex: warning: AH_CX_TOKEN_beta is the same ChatGPT account as alpha; it is not routed\n"
    )


def test_the_stop_hook_hands_its_transcript_to_the_rollout_link(monkeypatch):
    seen = []
    monkeypatch.setattr(hook_manager, "_link_codex_rollout", seen.append)
    monkeypatch.setattr(hook_manager, "_confirm_inbox", lambda session_id: None)
    monkeypatch.setattr("hooks.config.BRAIN_WRITER_ENABLED", False)
    hook_manager.on_stop({"session_id": "s-link", "transcript_path": "/nonexistent/rollout.jsonl"})
    assert seen == ["/nonexistent/rollout.jsonl"]


def test_balance_names_whether_the_default_login_and_ncgma_are_distinct(tmp_path):
    _default_login(tmp_path, "acct-shared")
    same = {"HOME": str(tmp_path), "AH_CX_TOKEN_ncgma": _jwt("acct-shared")}
    mark = codex_router.fingerprint("acct-shared")
    assert codex_router.default_distinction(same) == f"codex default login #{mark}: same ChatGPT account as ncgma"
    other = {"HOME": str(tmp_path), "AH_CX_TOKEN_ncgma": _jwt("acct-other"), "AH_CX_TOKEN_beta": _jwt("acct-b")}
    assert codex_router.default_distinction(other) == f"codex default login #{mark}: distinct from beta, ncgma"
    unknown = {"HOME": str(tmp_path / "none"), "AH_CX_TOKEN_ncgma": _jwt("acct-shared")}
    assert codex_router.default_distinction(unknown) == "codex default login: identity unknown"
    assert codex_router.default_distinction({"HOME": str(tmp_path)}) == ""
    assert "acct-shared" not in codex_router.default_distinction(same)


def test_each_codex_row_shows_its_fingerprint_beside_the_account(tmp_path):
    environ = {"AH_CX_TOKEN_ncgma": _jwt("acct-n")}
    pool = [CodexAccount("ncgma", "AH_CX_TOKEN_ncgma"), CodexAccount("other", "AH_CX_TOKEN_other")]
    identities = agents_quota.fingerprints(pool, environ)
    assert identities == {"ncgma": codex_router.fingerprint("acct-n"), "other": ""}
    rows = agents_quota.codex_rows(pool, {}, {}, NOW, identities=identities)
    assert [row.identity for row in rows] == [codex_router.fingerprint("acct-n"), ""]
    table = agents_quota.render(rows, now=int(NOW)).splitlines()
    assert table[2].split()[2:4] == ["ncgma", f"#{codex_router.fingerprint('acct-n')}"]
    assert table[3].split()[2:4] == ["other", "subscription"]
    assert [row.identity for row in agents_quota.codex_rows(pool, {}, {}, NOW)] == ["", ""]


def test_a_stale_row_whose_week_has_reset_reads_unknown_instead_of_a_full_window(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CODEX_READING_MAX_AGE_H", raising=False)
    account = CodexAccount("alpha", "AH_CX_TOKEN_alpha")
    old = codex_quota.CodexQuota(NOW - 7 * 3600, "pro", seven_day=QuotaWindow(80.0, int(NOW - 60)))
    [row] = agents_quota.codex_rows([account], {"alpha": old}, {}, NOW)
    assert (row.state, row.seven_day_left, row.routing_left, row.cap) == ("STALE", None, None, None)
    recent = codex_quota.CodexQuota(NOW - 3600, "pro", seven_day=QuotaWindow(80.0, int(NOW - 60)))
    [row] = agents_quota.codex_rows([account], {"alpha": recent}, {}, NOW)
    assert (row.seven_day_left, row.routing_left, row.cap) == (100.0, 100.0, 6)
    running = codex_quota.CodexQuota(NOW - 7 * 3600, "pro", seven_day=QuotaWindow(80.0, int(NOW + 3600)))
    [row] = agents_quota.codex_rows([account], {"alpha": running}, {}, NOW)
    assert (row.state, row.seven_day_left, row.routing_left) == ("STALE", 20.0, 20.0)
    short = {"AGENTIHOOKS_CODEX_READING_MAX_AGE_H": "0.5"}
    [row] = agents_quota.codex_rows([account], {"alpha": recent}, {}, NOW, short)
    assert (row.seven_day_left, row.routing_left) == (None, None)


def test_a_rollout_links_into_its_own_account_folder_once(tmp_path):
    environ = {"HOME": str(tmp_path)}
    rollout = _rollout(tmp_path, ALPHA, 10.0)
    target = codex_rollouts.link(environ, "alpha", rollout)
    expected = tmp_path / ".codex" / "accounts" / "alpha" / "sessions" / "2026" / "10" / "04" / rollout.name
    assert target == expected
    assert target.samefile(rollout)
    assert codex_rollouts.link(environ, "alpha", rollout) == expected
    assert sorted(p.name for p in expected.parent.iterdir()) == [rollout.name]
    assert codex_rollouts.session_ids(environ, "alpha") == {ALPHA}
    assert codex_rollouts.session_ids(environ, "beta") == set()
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text("{}\n")
    assert codex_rollouts.link(environ, "alpha", outside) is None


def test_a_rewritten_rollout_is_linked_again(tmp_path):
    environ = {"HOME": str(tmp_path)}
    rollout = _rollout(tmp_path, ALPHA, 10.0)
    codex_rollouts.link(environ, "alpha", rollout)
    rollout.unlink()
    rollout = _rollout(tmp_path, ALPHA, 20.0)
    target = codex_rollouts.link(environ, "alpha", rollout)
    assert target.samefile(rollout)


def test_a_probe_thread_links_its_rollout(tmp_path):
    environ = {"HOME": str(tmp_path)}
    rollout = _rollout(tmp_path, BETA, 10.0)
    assert codex_rollouts.link_thread(environ, "beta", BETA).samefile(rollout)
    assert codex_rollouts.link_thread(environ, "beta", OWN) is None


def test_separate_folders_keep_each_account_on_its_own_reading(tmp_path, monkeypatch):
    environ = {"HOME": str(tmp_path), "AH_CX_TOKEN_alpha": "at-a", "AH_CX_TOKEN_beta": "at-b"}
    monkeypatch.setattr(codex_router, "_registry", lambda: {ALPHA: {"account": "beta"}, OWN: {"account": "default"}})
    monkeypatch.setattr(codex_router.codex_usage, "cached", lambda: {})
    codex_rollouts.link(environ, "alpha", _rollout(tmp_path, ALPHA, 10.0))
    codex_rollouts.link(environ, "beta", _rollout(tmp_path, BETA, 60.0))
    _rollout(tmp_path, OWN, 30.0)
    pool = [CodexAccount("alpha", "AH_CX_TOKEN_alpha"), CodexAccount("beta", "AH_CX_TOKEN_beta")]
    found = codex_router.quotas([*pool, CodexAccount("default")], environ)
    assert {name: quota.seven_day.used for name, quota in found.items()} == {
        "alpha": 10.0,
        "beta": 60.0,
        "default": 30.0,
    }


def test_the_default_reading_skips_a_session_linked_into_a_token_folder(tmp_path, monkeypatch):
    environ = {"HOME": str(tmp_path), "AH_CX_TOKEN_alpha": "at-a"}
    monkeypatch.setattr(codex_router, "_registry", lambda: {ALPHA: {"account": "default"}})
    monkeypatch.setattr(codex_router.codex_usage, "cached", lambda: {})
    codex_rollouts.link(environ, "alpha", _rollout(tmp_path, ALPHA, 10.0))
    assert codex_router.quotas([CodexAccount("default")], environ) == {"default": None}


def test_the_codex_stop_hook_links_the_transcript_under_the_session_account(tmp_path, monkeypatch):
    rollout = _rollout(tmp_path, ALPHA, 10.0)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr("hooks.targets.is_codex", lambda: True)
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: 4242)
    monkeypatch.setattr("hooks.context.account_sessions.session_account", lambda pid: f"acct{pid}")
    hook_manager._link_codex_rollout(str(rollout))
    linked = tmp_path / ".codex" / "accounts" / "acct4242" / "sessions" / "2026" / "10" / "04" / rollout.name
    assert linked.samefile(rollout)
    assert codex_rollouts.link_current("") is None


def test_a_claude_stop_links_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("hooks.targets.is_codex", lambda: False)
    monkeypatch.setattr(codex_rollouts, "link_current", lambda path: pytest.fail("Claude links no rollout"))
    hook_manager._link_codex_rollout(str(tmp_path / "x.jsonl"))


def test_a_failed_link_is_logged_and_never_raises(monkeypatch):
    seen = []
    monkeypatch.setattr("hooks.targets.is_codex", lambda: True)

    def boom(path):
        raise RuntimeError("boom")

    monkeypatch.setattr(codex_rollouts, "link_current", boom)
    monkeypatch.setattr(hook_manager, "log", lambda message, data: seen.append((message, data)))
    hook_manager._link_codex_rollout("/x.jsonl")
    assert seen == [("codex rollout link failed", {"error": "boom"})]


def test_a_stale_reset_needs_both_a_reading_past_its_max_age_and_a_passed_week(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_CODEX_READING_MAX_AGE_H", raising=False)
    old = codex_quota.CodexQuota(NOW - 7 * 3600, "pro", seven_day=QuotaWindow(80.0, int(NOW - 60)))
    recent = codex_quota.CodexQuota(NOW - 3600, "pro", seven_day=QuotaWindow(80.0, int(NOW - 60)))
    running = codex_quota.CodexQuota(NOW - 7 * 3600, "pro", seven_day=QuotaWindow(80.0, int(NOW + 3600)))
    assert [codex_router.stale_reset(q, NOW) for q in (old, recent, running)] == [True, False, False]
    assert codex_router.stale_reset(recent, NOW, {"AGENTIHOOKS_CODEX_READING_MAX_AGE_H": "0.5"}) is True


def test_an_empty_account_folder_falls_back_to_the_registry_reading(tmp_path, monkeypatch):
    environ = {"HOME": str(tmp_path), "AH_CX_TOKEN_alpha": "at-a"}
    monkeypatch.setattr(codex_router, "_registry", lambda: {ALPHA: {"account": "alpha"}})
    monkeypatch.setattr(codex_router.codex_usage, "cached", lambda: {})
    _rollout(tmp_path, ALPHA, 40.0)
    codex_rollouts.folder(environ, "alpha").mkdir(parents=True)
    found = codex_router.quotas([CodexAccount("alpha", "AH_CX_TOKEN_alpha")], environ)
    assert found["alpha"].seven_day.used == 40.0


def test_a_thread_id_is_matched_literally(tmp_path):
    environ = {"HOME": str(tmp_path)}
    _rollout(tmp_path, ALPHA, 10.0)
    assert codex_rollouts.link_thread(environ, "alpha", "*") is None
    assert codex_rollouts.session_ids(environ, "alpha") == set()


def test_an_exec_probe_links_its_rollout_into_the_account_folder(tmp_path, monkeypatch):
    environ = {"HOME": str(tmp_path), "AH_CX_TOKEN_alpha": "at-a"}
    rollout = _rollout(tmp_path, ALPHA, 25.0)

    class Done:
        stdout = json.dumps({"type": "thread.started", "thread_id": ALPHA}) + "\n"

    found = codex_router.probe(CodexAccount("alpha", "AH_CX_TOKEN_alpha"), environ, lambda *a, **k: Done())
    assert found.seven_day.used == 25.0
    linked = codex_rollouts.folder(environ, "alpha") / "2026" / "10" / "04" / rollout.name
    assert linked.samefile(rollout)


def test_the_balance_table_ends_with_the_duplicate_warning_and_the_default_verdict(tmp_path, monkeypatch):
    _default_login(tmp_path, "acct-1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    for name in [name for name in os.environ if name.startswith("AH_CX_TOKEN_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AH_CX_TOKEN_alpha", _jwt("acct-1"))
    monkeypatch.setenv("AH_CX_TOKEN_ncgma", _jwt("acct-1", "second login"))
    monkeypatch.setattr("hooks.context.account_sessions.codex_sessions_by_account", lambda: {})
    row = agents_quota.QuotaRow("codex", "alpha", "UNKNOWN", 0, None, None, None, "log")
    lines = agents_quota._render_live([row], {}, NOW).splitlines()
    mark = codex_router.fingerprint("acct-1")
    assert lines[3:] == [
        "AH_CX_TOKEN_ncgma is the same ChatGPT account as alpha; it is not routed",
        f"codex default login #{mark}: same ChatGPT account as alpha, ncgma",
    ]
    assert len(agents_quota._render_live([row], {}, NOW, fable=True).splitlines()) == 3


def _jwt_with_email(account_id, email):
    def part(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    claims = {
        codex_router.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": account_id},
        codex_router.OPENAI_PROFILE_CLAIM: {"email": email},
    }
    return ".".join([part(b'{"alg":"RS256"}'), part(json.dumps(claims).encode()), "c2ln"])


def test_a_duplicate_routes_under_the_slug_of_its_email_not_the_first_name_in_sort_order(tmp_path):
    token = _jwt_with_email("acct-1", "Nestor.Colt@gmail.com")
    environ = {
        "HOME": str(tmp_path),
        "AH_CX_TOKEN_alpha": token,
        "AH_CX_TOKEN_nestor_coltatgmail_com": token + "x",
        "AH_CX_TOKEN_gamma": _jwt("acct-2"),
    }
    tokens = codex_router.token_accounts(environ)
    assert codex_router.duplicates(tokens, environ) == {"alpha": "nestor_coltatgmail_com"}
    pool = codex_router.CodexAccountSource(lambda *a, **k: None).pool(environ)
    assert [account.name for account in pool] == ["gamma", "nestor_coltatgmail_com"]
    assert codex_router.identity_warnings(environ) == [
        "AH_CX_TOKEN_alpha is the same ChatGPT account as nestor_coltatgmail_com; it is not routed"
    ]


def test_without_an_email_claim_an_email_shaped_slug_is_kept_over_an_earlier_name(tmp_path):
    environ = {
        "HOME": str(tmp_path),
        "AH_CX_TOKEN_aaa": _jwt("acct-1"),
        "AH_CX_TOKEN_devatexample_com": _jwt("acct-1", "second login"),
        "AH_CX_TOKEN_zed": _jwt("acct-1", "third login"),
    }
    tokens = codex_router.token_accounts(environ)
    assert codex_router.duplicates(tokens, environ) == {"aaa": "devatexample_com", "zed": "devatexample_com"}


def test_the_email_claim_decides_between_two_email_shaped_slugs(tmp_path):
    token = _jwt_with_email("acct-1", "ops@example.com")
    environ = {
        "HOME": str(tmp_path),
        "AH_CX_TOKEN_devatexample_com": token,
        "AH_CX_TOKEN_opsatexample_com": token + "x",
    }
    tokens = codex_router.token_accounts(environ)
    assert codex_router.duplicates(tokens, environ) == {"devatexample_com": "opsatexample_com"}


@pytest.mark.parametrize(
    ("name", "shaped"),
    [
        ("aatb_cc", True),
        ("nestor_coltatgmail_com", True),
        ("aat__cc", True),
        ("atb_cc", False),
        ("aat_cc", False),
        ("aatb_c", False),
        ("aatb_c1", False),
        ("aatb_cC", False),
        ("a-atb_cc", False),
        ("aAtb_cc", False),
        ("aatbcc", False),
        ("aatb_", False),
    ],
)
def test_an_email_shaped_slug_is_user_at_domain_and_a_letter_suffix(name, shaped):
    assert codex_router.email_shaped(name) is shaped
