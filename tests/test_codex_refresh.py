import fcntl
import io
import json
import urllib.parse
from pathlib import Path

import pytest

from scripts import aicode_tokens, codex_refresh, openbao_kv
from tests.codex_fakes import BAO_TOKEN, BAO_URL, NOW, FakeBao, access_token, http_error

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
    write_private, write_rc, merge = codex_refresh._write_private, aicode_tokens.write_rc, openbao_kv.merge
    monkeypatch.setattr(codex_refresh, "_write_private", lambda *a: order.append("auth") or write_private(*a))
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
    assert "alpha: refresh failed HTTP 400 invalid_grant" in out
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
    recovery = _home("alpha") / "auth.json"
    assert json.loads(recovery.read_text())["tokens"]["refresh_token"] == "rt-alpha-next"
    assert recovery.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert str(recovery) in out and "rt-alpha-next" not in out


def test_a_second_concurrent_run_on_the_same_machine_skips(capsys):
    _account("alpha", 1)
    lock = (Path.home() / ".agentihooks" / "codex-refresh.lock").open("a+")
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
        seen.append(request)
        return io.BytesIO(json.dumps({"access_token": FRESH}).encode())

    assert codex_refresh.grant("rt-one", opener=opener) == {"access_token": FRESH}
    request = seen[0]
    assert (request.full_url, request.get_method()) == ("https://auth.openai.com/oauth/token", "POST")
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
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
