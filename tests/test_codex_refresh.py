import fcntl
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from hooks import config
from scripts import aicode_tokens, codex_refresh, openbao_kv
from tests.codex_fakes import BAO_TOKEN, BAO_URL, NOW, FakeBao, access_token, http_error, jwt

FLAG = {"AGENTIHOOKS_AICODE_TOKEN_REFRESH": "true"}
BAO_ENV = {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": BAO_TOKEN}
OPENBAO_STORE = {"AGENTIHOOKS_AICODE_TOKEN_STORE": "openbao"}
FRESH = access_token(10)


class Grant:
    def __init__(self, rotate=True, fail=()):
        self.rotate, self.fail, self.calls = rotate, set(fail), []

    def __call__(self, refresh_token):
        self.calls.append(refresh_token)
        if refresh_token in self.fail:
            raise codex_refresh.GrantError(400, "invalid_grant")
        new = f"{refresh_token}-next" if self.rotate else refresh_token
        return {"access_token": FRESH, "refresh_token": new, "id_token": "id-next"}


def _home(slug):
    return Path.home() / ".agentihooks" / "codex" / slug


def _account(slug, days_left, refresh=None):
    home = _home(slug)
    home.mkdir(parents=True, exist_ok=True)
    tokens = {"access_token": access_token(days_left), "refresh_token": refresh or f"rt-{slug}", "id_token": "id"}
    (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}))
    return home / "auth.json"


def _run(args, environ, grant=None, bao=None):
    deps = codex_refresh.Deps(grant=grant or Grant(), opener=bao, now=lambda: NOW)
    return codex_refresh.main(args, environ, deps)


def test_a_machine_that_is_not_the_refresh_authority_refreshes_nothing(capsys):
    _account("alpha", 1)
    grant = Grant()
    assert _run([], {}, grant) == 0
    assert not grant.calls
    assert "not the refresh authority" in capsys.readouterr().out


def test_the_local_store_refreshes_accounts_under_the_threshold_into_auth_json_and_the_block(capsys):
    alpha, beta = _account("alpha", 1), _account("beta", 8)
    before = beta.read_text()
    assert _run([], FLAG) == 0
    tokens = json.loads(alpha.read_text())
    assert tokens["tokens"] == {"access_token": FRESH, "refresh_token": "rt-alpha-next", "id_token": "id-next"}
    assert tokens["auth_mode"] == "chatgpt" and tokens["last_refresh"]
    assert alpha.stat().st_mode & 0o777 == 0o600
    assert beta.read_text() == before
    assert f"export AH_CX_TOKEN_alpha={FRESH}\n" in aicode_tokens.rc_file().read_text()
    out = capsys.readouterr().out
    assert "alpha: 1.0d left, refreshed, 10.0d left, refresh token rotated=True" in out
    assert "beta: 8.0d left, ok" in out
    assert FRESH not in out and "rt-alpha" not in out


def test_days_come_from_the_flag_then_the_environment_then_three(capsys):
    _account("alpha", 1)
    _account("beta", 8)
    grant = Grant()
    assert _run(["--days", "9"], FLAG, grant) == 0
    assert grant.calls == ["rt-alpha", "rt-beta"]
    _account("alpha", 1)
    _account("beta", 8)
    grant = Grant()
    assert _run([], {**FLAG, "AGENTIHOOKS_AICODE_TOKEN_REFRESH_DAYS": "9"}, grant) == 0
    assert grant.calls == ["rt-alpha", "rt-beta"]
    _account("alpha", 2.9)
    _account("beta", 3.1)
    grant = Grant()
    assert _run([], FLAG, grant) == 0
    assert grant.calls == ["rt-alpha"]


def test_a_refresh_without_openbao_credentials_says_openbao_was_not_updated(capsys):
    _account("alpha", 1)
    assert _run([], FLAG) == 0
    assert "OpenBao not updated: OPENBAO_URL or OPENBAO_TOKEN unset" in capsys.readouterr().out
    _account("beta", 8)
    _run(["--force", "beta"], {**FLAG, **BAO_ENV}, bao=FakeBao())
    assert "OpenBao not updated" not in capsys.readouterr().out


def test_a_write_error_on_one_account_is_reported_and_the_others_still_refresh(monkeypatch, capsys):
    _account("alpha", 1)
    _account("beta", 1)
    write_rc = aicode_tokens.write_rc

    def failing(path, entries, stale=()):
        if "AH_CX_TOKEN_alpha" in entries:
            raise OSError(28, "No space left on device")
        write_rc(path, entries, stale)

    monkeypatch.setattr(aicode_tokens, "write_rc", failing)
    grant = Grant()
    assert _run([], FLAG, grant) == 1
    assert grant.calls == ["rt-alpha", "rt-beta"]
    assert "alpha: write failed" in capsys.readouterr().out
    assert f"export AH_CX_TOKEN_beta={FRESH}\n" in aicode_tokens.rc_file().read_text()


def test_force_refreshes_that_one_account_now():
    _account("alpha", 1)
    _account("beta", 8)
    grant = Grant()
    assert _run(["--force", "beta"], FLAG, grant) == 0
    assert grant.calls == ["rt-beta"]


def test_folders_that_are_not_valid_slugs_are_ignored():
    _account("_hidden", 1)
    _account("alpha", 1)
    grant = Grant()
    assert _run([], FLAG, grant) == 0
    assert grant.calls == ["rt-alpha"]


def test_the_local_store_writes_auth_json_then_the_block_then_openbao(monkeypatch):
    _account("alpha", 1)
    order = []
    save_auth, write_rc, merge = codex_refresh._save_auth, aicode_tokens.write_rc, openbao_kv.merge
    monkeypatch.setattr(codex_refresh, "_save_auth", lambda *a: order.append("auth") or save_auth(*a))
    monkeypatch.setattr(aicode_tokens, "write_rc", lambda *a, **k: order.append("rc") or write_rc(*a, **k))
    monkeypatch.setattr(openbao_kv, "merge", lambda *a, **k: order.append("bao") or merge(*a, **k))
    bao = FakeBao({"OTHER": "o"})
    assert _run([], {**FLAG, **BAO_ENV}, bao=bao) == 0
    assert order == ["auth", "rc", "bao"]
    assert bao.data == {"OTHER": "o", "AH_CX_TOKEN_alpha": FRESH, "AH_CX_REFRESH_alpha": "rt-alpha-next"}


def test_an_unrotated_refresh_token_is_reported(capsys):
    _account("alpha", 1)
    assert _run([], FLAG, Grant(rotate=False)) == 0
    assert "refresh token rotated=False" in capsys.readouterr().out


def test_a_failed_grant_names_the_account_and_moves_on(capsys):
    _account("alpha", 1, refresh="rt-bad")
    _account("beta", 1)
    grant = Grant(fail={"rt-bad"})
    assert _run([], FLAG, grant) == 1
    assert grant.calls == ["rt-bad", "rt-beta"]
    out = capsys.readouterr().out
    assert "alpha: refresh failed: HTTP 400 invalid_grant" in out
    assert "beta: 1.0d left, refreshed" in out


def test_the_openbao_store_refuses_to_run_without_credentials(capsys):
    assert _run([], {**FLAG, **OPENBAO_STORE}) == 1
    assert "OPENBAO_URL" in capsys.readouterr().out


def test_the_openbao_store_reads_and_writes_only_openbao_and_an_existing_block():
    secret = {
        "AH_CX_TOKEN_alpha": access_token(1),
        "AH_CX_REFRESH_alpha": "rt-alpha",
        "AH_CX_TOKEN_beta": access_token(8),
        "AH_CX_REFRESH_beta": "rt-beta",
        "OTHER": "o",
    }
    bao = FakeBao(secret)
    grant = Grant()
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, grant, bao) == 0
    assert grant.calls == ["rt-alpha"]
    assert bao.data == {**secret, "AH_CX_TOKEN_alpha": FRESH, "AH_CX_REFRESH_alpha": "rt-alpha-next"}
    assert not aicode_tokens.rc_file().exists() and not _home("alpha").exists()
    rc = aicode_tokens.rc_file()
    rc.write_text(f"{aicode_tokens.START}\n{aicode_tokens.END}\n")
    assert _run(["--force", "beta"], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, Grant(), bao) == 0
    assert f"export AH_CX_TOKEN_beta={FRESH}\n" in rc.read_text()


def test_a_failed_openbao_write_after_a_refresh_keeps_the_new_pair_in_a_recovery_file(capsys):
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"}, fail_writes=True)
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, Grant(), bao) == 1
    recovery = _home("alpha") / "recovery.json"
    assert json.loads(recovery.read_text())["tokens"]["refresh_token"] == "rt-alpha-next"
    assert recovery.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert str(recovery) in out and "rt-alpha-next" not in out


def test_a_write_answered_with_something_other_than_json_keeps_the_new_pair_in_a_recovery_file():
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"})
    bao.garbage_writes = True
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, Grant(), bao) == 1
    assert json.loads((_home("alpha") / "recovery.json").read_text())["tokens"]["refresh_token"] == "rt-alpha-next"


def test_the_next_openbao_run_stores_a_newer_recovery_pair_and_removes_the_file(capsys):
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"}, fail_writes=True)
    environ = {**FLAG, **BAO_ENV, **OPENBAO_STORE}
    assert _run([], environ, Grant(), bao) == 1
    bao.fail_writes = False
    grant = Grant()
    assert _run([], environ, grant, bao) == 0
    assert not grant.calls
    assert bao.data == {"AH_CX_TOKEN_alpha": FRESH, "AH_CX_REFRESH_alpha": "rt-alpha-next"}
    assert not (_home("alpha") / "recovery.json").exists()
    assert "using the newer pair kept in" in capsys.readouterr().out


def test_the_openbao_store_never_touches_the_login_auth_json():
    login = _account("alpha", 9)
    before = login.read_bytes()
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"})
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, Grant(), bao) == 0
    assert login.read_bytes() == before
    assert bao.data["AH_CX_REFRESH_alpha"] == "rt-alpha-next"


def _recovery_file(slug, tokens):
    path = _home(slug) / "recovery.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": tokens}))
    return path


def test_a_recovery_pair_older_than_openbao_is_removed(capsys):
    stale = _recovery_file("alpha", {"access_token": access_token(1), "refresh_token": "rt-spent"})
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(8), "AH_CX_REFRESH_alpha": "rt-alpha"})
    grant = Grant()
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, grant, bao) == 0
    assert not stale.exists() and not grant.calls
    assert "older than the pair in OpenBao" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["not json", json.dumps({"tokens": {"access_token": "x.e30.y"}})])
def test_an_unreadable_recovery_file_is_reported_kept_and_the_run_continues(content, capsys):
    path = _home("alpha") / "recovery.json"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"})
    grant = Grant()
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, grant, bao) == 0
    assert grant.calls == ["rt-alpha"] and path.read_text() == content
    assert "ignoring unreadable" in capsys.readouterr().out


def test_a_malformed_expiry_in_the_recovery_file_is_unreadable_not_fatal(capsys):
    _recovery_file("alpha", {"access_token": jwt({"exp": "soon"}), "refresh_token": "rt-kept"})
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"})
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, Grant(), bao) == 0
    assert "ignoring unreadable" in capsys.readouterr().out


def test_an_openbao_account_whose_new_pair_cannot_be_kept_anywhere_warns_and_the_run_continues(monkeypatch, capsys):
    save = codex_refresh._save_auth

    def failing(path, fresh, now):
        if path.name == "recovery.json":
            raise OSError(30, "Read-only file system")
        save(path, fresh, now)

    monkeypatch.setattr(codex_refresh, "_save_auth", failing)
    secret = {"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"}
    secret |= {"AH_CX_TOKEN_beta": access_token(1), "AH_CX_REFRESH_beta": "rt-beta"}
    grant = Grant()
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, grant, FakeBao(secret, fail_writes=True)) == 1
    assert grant.calls == ["rt-alpha", "rt-beta"]
    out = capsys.readouterr().out
    assert "alpha: new token pair not persisted; run agentihooks codex login alpha" in out
    assert "beta: new token pair not persisted; run agentihooks codex login beta" in out


def test_a_failed_local_save_still_stores_the_pair_in_openbao_and_warns_only_when_nothing_kept_it(monkeypatch, capsys):
    def failing(path, fresh, now):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(codex_refresh, "_save_auth", failing)
    _account("alpha", 1)
    bao = FakeBao()
    assert _run([], {**FLAG, **BAO_ENV}, Grant(), bao) == 1
    assert bao.data == {"AH_CX_TOKEN_alpha": FRESH, "AH_CX_REFRESH_alpha": "rt-alpha-next"}
    assert "not persisted" not in capsys.readouterr().out
    assert _run(["--force", "alpha"], FLAG, Grant()) == 1
    out = capsys.readouterr().out
    assert "alpha: new token pair not persisted; run agentihooks codex login alpha" in out
    assert "OpenBao not updated" not in out


def test_openbao_not_updated_is_said_only_after_a_local_save(capsys):
    _account("alpha", 8)
    assert _run([], FLAG) == 0
    _account("beta", 1, refresh="rt-bad")
    assert _run(["--force", "beta"], FLAG, Grant(fail={"rt-bad"})) == 1
    assert "OpenBao not updated" not in capsys.readouterr().out


def test_a_second_concurrent_run_on_the_same_machine_skips(capsys):
    _account("alpha", 1)
    lock = (Path(config.AGENTIHOOKS_HOME) / "codex-refresh.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    grant = Grant()
    try:
        assert _run([], FLAG, grant) == 0
    finally:
        lock.close()
    assert not grant.calls
    assert "already running" in capsys.readouterr().out


def test_the_grant_posts_the_refresh_form_to_the_openai_token_endpoint():
    seen = []

    def opener(request, timeout=None):
        seen.append((request, timeout))
        return io.BytesIO(json.dumps({"access_token": FRESH}).encode())

    assert codex_refresh.grant("rt-one", opener=opener) == {"access_token": FRESH}
    request, timeout = seen[0]
    assert (request.full_url, request.get_method(), timeout) == ("https://auth.openai.com/oauth/token", "POST", 20)
    assert request.get_header("Accept") == "application/json"
    assert urllib.parse.parse_qs(request.data.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["rt-one"],
        "client_id": ["app_EMoamEEZ73f0CkXaXp7hrann"],
    }


def test_a_refused_grant_carries_the_status_and_the_oauth_error():
    def opener(request, timeout=None):
        raise http_error(request.full_url, 401, b'{"error": "invalid_grant"}')

    with pytest.raises(codex_refresh.GrantError) as raised:
        codex_refresh.grant("rt-one", opener=opener)
    assert (raised.value.status, raised.value.error) == (401, "invalid_grant")


def _raising(error):
    def opener(request, timeout=None):
        raise error

    return opener


@pytest.mark.parametrize(
    "opener, message",
    [
        (_raising(http_error(codex_refresh.TOKEN_URL, 401, b'{"error": "invalid_grant"}')), "HTTP 401 invalid_grant"),
        (_raising(http_error(codex_refresh.TOKEN_URL, 400, b"")), "HTTP 400 with no OAuth error"),
        (_raising(http_error(codex_refresh.TOKEN_URL, 400, b'{"error": {"code": 1}}')), "HTTP 400 with no OAuth error"),
        (_raising(urllib.error.URLError("timed out")), "unreachable: timed out"),
        (_raising(OSError("network down")), "unreachable: network down"),
        (lambda request, timeout=None: io.BytesIO(b"<html>"), "unreachable: Expecting value: line 1 column 1 (char 0)"),
    ],
)
def test_every_grant_failure_says_what_happened(opener, message):
    with pytest.raises(codex_refresh.GrantError) as raised:
        codex_refresh.grant("rt-one", opener=opener)
    assert str(raised.value) == message
    assert raised.value.status == (int(message.split()[1]) if message.startswith("HTTP") else 0)


def test_a_grant_answer_that_is_not_an_object_has_no_tokens():
    assert codex_refresh.grant("rt-one", opener=lambda request, timeout=None: io.BytesIO(b"[1]")) == {}


def test_failed_refreshes_say_why(capsys):
    _account("alpha", 1)
    (_home("alpha") / "auth.json").write_text(json.dumps({"tokens": {"access_token": access_token(1)}}))
    _account("beta", 1)
    assert _run([], FLAG, lambda refresh: {"refresh_token": "x"}) == 1
    assert capsys.readouterr().out == (
        "alpha: refresh failed: no refresh token\nbeta: refresh failed: the grant returned no access token\n"
    )


def test_a_grant_without_a_new_refresh_token_keeps_the_old_one(capsys):
    auth = _account("alpha", 1)
    assert _run([], FLAG, lambda refresh: {"access_token": FRESH}) == 0
    assert json.loads(auth.read_text())["tokens"]["refresh_token"] == "rt-alpha"
    assert "refresh token rotated=False" in capsys.readouterr().out


def test_exactly_the_threshold_left_is_not_refreshed_and_a_token_without_expiry_is(capsys):
    _account("alpha", 3)
    (_home("beta")).mkdir(parents=True)
    (_home("beta") / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "opaque", "refresh_token": "rt-b"}})
    )
    grant = Grant()
    assert _run([], FLAG, grant) == 0
    assert grant.calls == ["rt-b"]
    out = capsys.readouterr().out
    assert "alpha: 3.0d left, ok\n" in out and "beta: 0.0d left, refreshed, 10.0d left" in out


def test_a_refresh_stamps_auth_json_with_the_refresh_time():
    auth = _account("alpha", 1)
    _run([], FLAG)
    assert json.loads(auth.read_text())["last_refresh"] == "2027-01-15T08:00:00Z"


def test_a_recovery_file_starts_a_private_folder(capsys):
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"}, fail_writes=True)
    assert not _home("alpha").parent.exists()
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, Grant(), bao) == 1
    assert _home("alpha").stat().st_mode & 0o777 == 0o700
    assert json.loads((_home("alpha") / "recovery.json").read_text())["last_refresh"] == "2027-01-15T08:00:00Z"
    assert f"alpha: the new token pair is kept in {_home('alpha') / 'recovery.json'}; " in capsys.readouterr().out


def test_a_recovery_pair_as_old_as_openbao_is_removed():
    stale = _recovery_file("alpha", {"access_token": access_token(8), "refresh_token": "rt-spent"})
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(8), "AH_CX_REFRESH_alpha": "rt-alpha"})
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, Grant(), bao) == 0
    assert not stale.exists()


def test_a_recovery_pair_whose_store_fails_again_is_kept():
    recovery = _recovery_file("alpha", {"access_token": access_token(2), "refresh_token": "rt-kept"})
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"}, fail_writes=True)
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, Grant(), bao) == 1
    assert json.loads(recovery.read_text())["tokens"]["refresh_token"] == "rt-kept-next"


def test_the_openbao_store_reports_failed_grants_reads_and_unknown_accounts(capsys):
    environ = {**FLAG, **BAO_ENV, **OPENBAO_STORE}
    bao = FakeBao({"AH_CX_REFRESH_alpha": "rt-bad", "AH_CX_REFRESH__hidden": "rt-hidden"})
    grant = Grant(fail={"rt-bad"})
    assert _run([], environ, grant, bao) == 1
    assert grant.calls == ["rt-bad"]
    assert _run(["--force", "ghost"], environ, Grant(), bao) == 1
    assert _run([], environ, Grant(), _sealed()) == 1
    out = capsys.readouterr().out
    assert "alpha: refresh failed: HTTP 400 invalid_grant\n" in out
    assert "ghost: no such Codex account in OpenBao\n" in out
    assert "OpenBao read failed: OpenBao HTTP 503: sealed\n" in out


def _sealed():
    def opener(request, timeout=None):
        raise http_error(request.full_url, 503, b'{"errors": ["sealed"]}')

    return opener


def test_force_in_the_openbao_store_refreshes_only_that_account():
    secret = {"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"}
    secret |= {"AH_CX_TOKEN_beta": access_token(1), "AH_CX_REFRESH_beta": "rt-beta"}
    grant = Grant()
    assert _run(["--force", "beta"], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, grant, FakeBao(secret)) == 0
    assert grant.calls == ["rt-beta"]


def test_force_on_an_unknown_local_account_says_so(capsys):
    assert _run(["--force", "ghost"], FLAG) == 1
    assert capsys.readouterr().out == f"ghost: no such Codex account under {_home('ghost').parent}\n"


@pytest.mark.parametrize(
    "args, environ, message",
    [
        (
            [],
            {"AGENTIHOOKS_AICODE_TOKEN_STORE": "vault"},
            "AGENTIHOOKS_AICODE_TOKEN_STORE must be local or openbao, not 'vault'",
        ),
        (["--force", "_bad"], {}, f"{aicode_tokens.SLUG_RULE}: '_bad'"),
    ],
)
def test_bad_settings_exit_two_with_the_reason(args, environ, message, capsys):
    assert _run(args, {**FLAG, **environ}) == 2
    assert capsys.readouterr().out == f"agentihooks codex refresh: {message}\n"


def test_messages_for_a_missing_authority_openbao_settings_and_a_busy_machine(capsys):
    assert _run([], {"AGENTIHOOKS_AICODE_TOKEN_REFRESH": "no"}) == 0
    assert _run([], {**FLAG, **OPENBAO_STORE}) == 1
    lock = (Path(config.AGENTIHOOKS_HOME) / "codex-refresh.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        assert _run([], FLAG) == 0
    finally:
        lock.close()
    assert capsys.readouterr().out == (
        "AGENTIHOOKS_AICODE_TOKEN_REFRESH is not true: this machine is not the refresh authority\n"
        "AGENTIHOOKS_AICODE_TOKEN_STORE=openbao needs OPENBAO_URL and OPENBAO_TOKEN\n"
        "another codex refresh is already running on this machine; skipping\n"
    )


def test_the_lock_folder_is_created_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", str(tmp_path / "fresh" / "home"))
    _account("alpha", 8)
    assert _run([], {**FLAG, "AGENTIHOOKS_AICODE_TOKEN_STORE": " LOCAL "}) == 0
    assert (tmp_path / "fresh" / "home" / "codex-refresh.lock").exists()


def test_an_unreadable_days_setting_falls_back_to_three():
    _account("alpha", 2.9)
    _account("beta", 3.1)
    grant = Grant()
    assert _run([], {**FLAG, "AGENTIHOOKS_AICODE_TOKEN_REFRESH_DAYS": "soon"}, grant) == 0
    assert grant.calls == ["rt-alpha"]


def test_an_unknown_flag_shows_the_refresh_usage(capsys):
    with pytest.raises(SystemExit):
        _run(["--bogus"], FLAG)
    assert capsys.readouterr().err.startswith("usage: agentihooks codex refresh")


def test_the_openbao_summary_follows_each_local_refresh(capsys):
    _account("alpha", 1)
    _run([], {**FLAG, **BAO_ENV}, bao=FakeBao())
    assert "OpenBao secret/agentihooks/codex-creds: wrote AH_CX_REFRESH_alpha, AH_CX_TOKEN_alpha (2 keys total)\n" in (
        capsys.readouterr().out
    )


def _plan(force="", bao=None, grant=None):
    return codex_refresh.Run(force, 3.0, bao, codex_refresh.Deps(grant=grant or Grant(), now=lambda: NOW))


def test_the_store_runs_count_one_failure_for_a_missing_account_or_an_unreadable_secret():
    assert codex_refresh.run_local(_plan("ghost")) == 1
    bao = openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, _sealed())
    assert codex_refresh.run_openbao(_plan(bao=bao)) == 1
    bao = openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, FakeBao({"OTHER": "o"}))
    assert codex_refresh.run_openbao(_plan("ghost", bao)) == 1


def test_recovery_messages_name_the_account_and_the_file(capsys):
    path = _home("alpha") / "recovery.json"
    environ = {**FLAG, **BAO_ENV, **OPENBAO_STORE}
    _recovery_file("alpha", {"access_token": access_token(2), "refresh_token": "rt-kept"})
    _run([], environ, Grant(), FakeBao({"AH_CX_REFRESH_alpha": "rt-alpha"}))
    assert f"alpha: using the newer pair kept in {path}\n" in capsys.readouterr().out
    _recovery_file("alpha", {"access_token": access_token(1), "refresh_token": "rt-spent"})
    _run([], environ, Grant(), FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"}))
    assert f"alpha: removing {path}, older than the pair in OpenBao\n" in capsys.readouterr().out
    path.write_text("not json")
    _run([], environ, Grant(), FakeBao({"AH_CX_TOKEN_alpha": access_token(8), "AH_CX_REFRESH_alpha": "rt-alpha"}))
    assert capsys.readouterr().out.startswith(f"alpha: ignoring unreadable {path}\n")


def _full(*args, **kwargs):
    raise OSError(28, "No space left on device")


def test_a_failed_block_write_names_the_account_and_the_error(monkeypatch, capsys):
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"})
    aicode_tokens.rc_file().write_text(f"{aicode_tokens.START}\n{aicode_tokens.END}\n")
    monkeypatch.setattr(aicode_tokens, "write_rc", _full)
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, Grant(), bao) == 1
    assert "alpha: write failed: [Errno 28] No space left on device\n" in capsys.readouterr().out


@pytest.mark.parametrize("store", [OPENBAO_STORE, {}])
def test_a_failed_pair_save_names_the_account_and_the_error(store, monkeypatch, capsys):
    monkeypatch.setattr(codex_refresh, "_save_auth", _full)
    _account("alpha", 1)
    bao = FakeBao({"AH_CX_TOKEN_alpha": access_token(1), "AH_CX_REFRESH_alpha": "rt-alpha"}, fail_writes=True)
    assert _run([], {**FLAG, **BAO_ENV, **store}, Grant(), bao) == 1
    out = capsys.readouterr().out
    assert "OpenBao write failed: OpenBao HTTP 503: sealed\n" in out
    assert "alpha: write failed: [Errno 28] No space left on device\n" in out


def _locked_recovery(monkeypatch):
    unlink = Path.unlink

    def guarded(self, *args, **kwargs):
        if self.name == "recovery.json":
            raise PermissionError(13, "Permission denied", str(self))
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded)


@pytest.mark.parametrize("kept_days, openbao_days", [(1, 8), (9, 1)])
def test_a_recovery_file_that_cannot_be_removed_is_reported_and_the_run_continues(
    kept_days, openbao_days, monkeypatch, capsys
):
    path = _recovery_file("alpha", {"access_token": access_token(kept_days), "refresh_token": "rt-kept"})
    _locked_recovery(monkeypatch)
    secret = {"AH_CX_TOKEN_alpha": access_token(openbao_days), "AH_CX_REFRESH_alpha": "rt-alpha"}
    secret |= {"AH_CX_TOKEN_beta": access_token(1), "AH_CX_REFRESH_beta": "rt-beta"}
    grant = Grant()
    assert _run([], {**FLAG, **BAO_ENV, **OPENBAO_STORE}, grant, FakeBao(secret)) == 0
    assert grant.calls == ["rt-beta"]
    assert f"alpha: write failed: [Errno 13] Permission denied: '{path}'\n" in capsys.readouterr().out
