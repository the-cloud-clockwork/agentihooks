import io
import json
import os
import subprocess
from http.client import BadStatusLine
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from scripts import account_env, account_names, aicode_tokens, codex_refresh, codex_router, install, openbao_kv
from scripts.cli_delegates import delegated_cli

pytestmark = pytest.mark.unit

CODEX_PATH_ENV = "AGENTIHOOKS_CODEX_CREDS_PATH"
CODEX_PATH = "ops/codex-accounts"
CODEX_TWO = "AH_CX_TOKEN_" + aicode_tokens.slug("Ops.Two@Example.com")
CODEX_BASE = {"AH_CX_TOKEN_baseatx_io": "cxB"}
CODEX_ACCOUNTS = {
    CODEX_TWO: "cx2",
    "AH_CX_TOKEN_teamatx_io": "cx t'",
    "AH_CX_REFRESH_teamatx_io": "rt1",
    "AH_CX_TOKEN_emptyatx_io": "",
    "AH_CX_TOKEN_Upper_x": "cxU",
    "AH_CC_TOKEN_crossatx_io": "ccX",
    "AGENTIHOOKS_AICODE_TOKEN_REFRESH": "true",
}

LOADER = 'command -v agentihooks >/dev/null && eval "$(agentihooks accounts env)"'

ACCOUNTS = {
    "AH_CC_TOKEN_ops_2026attcc_dev": "tok b'quoted",
    "AH_CC_TOKEN_sv2_alphaatexample_com": "tok-a",
    "AH_CC_TOKEN_emptyatx_io": "",
    "ops+2026@example.com": "tok-email",
    "AH_CC_TOKEN_bad name": "tok-bad",
    "AH_CC_TOKEN_Upper_x": "tok-upper",
    "OTHER_NAME": "tok-other",
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _openbao(monkeypatch, payload=None, error=None, codex=None):
    seen = []

    def fake(request, timeout):
        seen.append((request.full_url, request.get_header("X-vault-token"), timeout))
        if error:
            raise error
        found = (CODEX_BASE if codex is None else codex) if "codex" in request.full_url else payload
        return _Response(json.dumps({"data": {"data": found, "metadata": {"version": 1}}}).encode())

    monkeypatch.setattr(account_env, "urlopen", fake)
    return seen


def _environ(tmp_path, monkeypatch, url="http://bao:8200/", bao="fixture-bao"):
    for name in ("OPENBAO_URL", "OPENBAO_TOKEN", "AGENTIHOOKS_HOME", CODEX_PATH_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    if url:
        monkeypatch.setenv("OPENBAO_URL", url)
    if bao:
        monkeypatch.setenv("OPENBAO_TOKEN", bao)


def _fallback(tmp_path, text, mode=0o600):
    state = tmp_path / ".agentihooks"
    path = state / "accounts" / "claude.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _shell(script: str) -> dict[str, str]:
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", 'eval "$1"; env -0', "_", script],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        check=True,
    )
    pairs = (item.decode().partition("=") for item in done.stdout.split(b"\0") if item)
    return {name: value for name, _, value in pairs}


def _evaluated(script: str) -> dict[str, str]:
    return {name: value for name, value in _shell(script).items() if name.startswith("AH_CC_TOKEN_")}


def _codex_vars(script: str) -> dict[str, str]:
    return {name: value for name, value in _shell(script).items() if name.startswith("AH_CX_")}


def _files(root: Path) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_openbao_token_names_print_one_export_each_under_the_same_name(tmp_path, monkeypatch, capsys):
    _environ(tmp_path, monkeypatch)
    seen = _openbao(monkeypatch, ACCOUNTS)

    assert account_env.main(["env"]) == 0

    out, err = capsys.readouterr()
    assert seen == [
        ("http://bao:8200/v1/secret/data/agentihooks/claude-creds", "fixture-bao", 3),
        ("http://bao:8200/v1/secret/data/agentihooks/codex-creds", "fixture-bao", 3),
    ]
    assert _evaluated(out) == {
        "AH_CC_TOKEN_ops_2026attcc_dev": "tok b'quoted",
        "AH_CC_TOKEN_sv2_alphaatexample_com": "tok-a",
    }
    assert [line.partition("=")[0] for line in out.splitlines()] == [
        "export AH_CC_TOKEN_ops_2026attcc_dev",
        "export AH_CC_TOKEN_sv2_alphaatexample_com",
        "export AH_CX_TOKEN_baseatx_io",
    ]
    assert err == ""


def test_the_command_writes_nothing_to_disk(tmp_path, monkeypatch, capsys):
    _environ(tmp_path, monkeypatch)
    _openbao(monkeypatch, ACCOUNTS)
    before = _files(tmp_path)

    account_env.main(["env"])

    assert _files(tmp_path) == before
    assert "tok-a" in capsys.readouterr().out


@pytest.mark.parametrize(
    "error",
    [URLError("refused"), TimeoutError("slow"), OSError("no route")],
)
def test_unreachable_openbao_falls_back_to_the_owner_only_file(tmp_path, monkeypatch, capsys, error):
    _environ(tmp_path, monkeypatch)
    _openbao(monkeypatch, error=error)
    _fallback(
        tmp_path,
        "# operator written\nexport AH_CC_TOKEN_nestor_coltatgmail_com='tok-a'\n"
        "AH_CC_TOKEN_broken='unclosed\n"
        '  AH_CC_TOKEN_second="tok two"\nOTHER_NAME=nope\nAH_CC_TOKEN_bad name=x\nAH_CC_TOKEN_split=tok one\n'
        "AH_CC_TOKEN_padded=tok==\n\n",
    )

    assert account_env.main(["env"]) == 0

    out, err = capsys.readouterr()
    assert _evaluated(out) == {
        "AH_CC_TOKEN_nestor_coltatgmail_com": "tok-a",
        "AH_CC_TOKEN_second": "tok two",
        "AH_CC_TOKEN_padded": "tok==",
    }
    assert "OTHER_NAME" not in out
    assert len(err.strip().splitlines()) == 2
    assert "OpenBao" in err and "tok-a" not in err and "tok two" not in err


@pytest.mark.parametrize(("url", "bao"), [("", ""), ("http://bao:8200", ""), ("", "fixture-bao")])
def test_missing_openbao_settings_go_straight_to_the_fallback(tmp_path, monkeypatch, capsys, url, bao):
    _environ(tmp_path, monkeypatch, url=url, bao=bao)
    seen = _openbao(monkeypatch, ACCOUNTS)
    _fallback(tmp_path, "AH_CC_TOKEN_one=tok-1\n")

    assert account_env.main(["env"]) == 0

    assert seen == []
    assert _evaluated(capsys.readouterr().out) == {"AH_CC_TOKEN_one": "tok-1"}


def test_the_fallback_follows_the_agentihooks_home(tmp_path, monkeypatch, capsys):
    _environ(tmp_path, monkeypatch, url="", bao="")
    state = tmp_path / "elsewhere"
    path = state / "accounts" / "claude.env"
    path.parent.mkdir(parents=True)
    path.write_text("AH_CC_TOKEN_one=tok-1\n")
    path.chmod(0o600)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(state))

    account_env.main(["env"])

    assert _evaluated(capsys.readouterr().out) == {"AH_CC_TOKEN_one": "tok-1"}


def test_neither_env_loader_reads_the_fallback(tmp_path, monkeypatch):
    from hooks import config
    from scripts import operator_env

    path = _fallback(tmp_path, "AH_CC_TOKEN_one=tok-1\n")
    (path.parent.parent / ".env").write_text("MAIN=1\n")
    (path.parent.parent / "other.env").write_text("OTHER=1\n")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(path.parent.parent))

    names = {name for name, _ in config._env_files_fingerprint()}
    assert "other.env" in names
    assert not any(name.endswith("claude.env") for name in names)
    assert path not in operator_env.files({"HOME": str(tmp_path)})


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_a_fallback_others_can_read_is_refused(tmp_path, monkeypatch, capsys, mode):
    _environ(tmp_path, monkeypatch, url="", bao="")
    _fallback(tmp_path, "AH_CC_TOKEN_one=tok-1\n", mode)

    assert account_env.main(["env"]) == 0

    out, err = capsys.readouterr()
    assert out == ""
    assert "600" in err and len(err.strip().splitlines()) == 1


def test_no_openbao_and_no_fallback_fails_quietly(tmp_path, monkeypatch, capsys):
    _environ(tmp_path, monkeypatch)
    _openbao(monkeypatch, error=URLError("refused"))

    assert account_env.main(["env"]) == 0

    out, err = capsys.readouterr()
    assert out == ""
    assert len(err.strip().splitlines()) == 2


def test_a_machine_without_openbao_or_fallback_stays_silent(tmp_path, monkeypatch, capsys):
    _environ(tmp_path, monkeypatch, url="", bao="")

    assert account_env.main(["env"]) == 0

    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("payload", [None, [], "text", {"AH_CC_TOKEN_x": 7}, {"X@Y.IO": "tok"}])
def test_a_malformed_secret_falls_back(tmp_path, monkeypatch, capsys, payload):
    _environ(tmp_path, monkeypatch)
    _openbao(monkeypatch, payload)
    _fallback(tmp_path, "AH_CC_TOKEN_one=tok-1\n")

    account_env.main(["env"])

    assert _evaluated(capsys.readouterr().out) == {"AH_CC_TOKEN_one": "tok-1"}


@pytest.mark.parametrize("argv", [[], ["show"], ["env", "extra"]])
def test_anything_but_env_prints_usage(argv, capsys):
    assert account_env.main(argv) == 2
    assert "accounts env" in capsys.readouterr().err


def test_the_cli_delegates_accounts_to_the_module():
    assert delegated_cli(["accounts", "env"]) is account_env.main


def _bashrc(tmp_path, monkeypatch):
    state = tmp_path / ".agentihooks"
    state.mkdir(exist_ok=True)
    bashrc = tmp_path / ".bashrc"
    monkeypatch.setattr(install, "_BASHRC", bashrc)
    monkeypatch.setattr(install, "_ENV_FILE_DST", state / ".env")
    return bashrc


def test_init_writes_one_loader_line_however_often_it_runs(tmp_path, monkeypatch):
    bashrc = _bashrc(tmp_path, monkeypatch)
    bashrc.write_text("export KEEP=1\n")

    install._update_bashrc_block()
    install._update_bashrc_block()

    text = bashrc.read_text()
    assert text.count(LOADER) == 1
    assert text.index("\nagentienv\n") < text.index(LOADER)
    assert "AH_CC_TOKEN_" not in text
    assert text.startswith("export KEEP=1\n")


def test_every_new_shell_loads_the_accounts_the_command_prints(tmp_path, monkeypatch):
    bashrc = _bashrc(tmp_path, monkeypatch)
    install._update_bashrc_block()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "agentihooks"
    fake.write_text('#!/usr/bin/env bash\n[[ "$*" == "accounts env" ]] && echo "export AH_CC_TOKEN_one=tok-1"\n')
    fake.chmod(0o755)

    shell = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", f'. "{bashrc}" >/dev/null 2>&1; printf %s "$AH_CC_TOKEN_one"'],
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        check=True,
    )

    assert shell.stdout == b"tok-1"


def test_a_shell_without_the_cli_still_starts(tmp_path, monkeypatch):
    bashrc = _bashrc(tmp_path, monkeypatch)
    install._update_bashrc_block()

    shell = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", f'. "{bashrc}"; echo started'],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
    )

    assert shell.stdout.decode().strip().endswith("started")


def _codex_environ(tmp_path, monkeypatch, **kwargs):
    _environ(tmp_path, monkeypatch, **kwargs)
    monkeypatch.setenv(CODEX_PATH_ENV, CODEX_PATH)


def _codex_fallback(tmp_path, text, mode=0o600):
    path = tmp_path / ".agentihooks" / "accounts" / "codex.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _codex_shell(tmp_path, monkeypatch, capsys) -> dict[str, str]:
    _codex_environ(tmp_path, monkeypatch)
    _openbao(monkeypatch, ACCOUNTS, codex=CODEX_ACCOUNTS)
    account_env.main(["env"])
    return _shell(capsys.readouterr().out)


def test_codex_token_names_load_from_the_secret_the_setting_names(tmp_path, monkeypatch, capsys):
    _codex_environ(tmp_path, monkeypatch)
    seen = _openbao(monkeypatch, ACCOUNTS, codex=CODEX_ACCOUNTS)
    before = _files(tmp_path)

    assert account_env.main(["env"]) == 0

    out, err = capsys.readouterr()
    assert [url for url, _, _ in seen] == [
        "http://bao:8200/v1/secret/data/agentihooks/claude-creds",
        f"http://bao:8200/v1/secret/data/{CODEX_PATH}",
    ]
    assert CODEX_TWO == "AH_CX_TOKEN_ops_twoatexample_com"
    assert _evaluated(out) == {
        "AH_CC_TOKEN_ops_2026attcc_dev": "tok b'quoted",
        "AH_CC_TOKEN_sv2_alphaatexample_com": "tok-a",
    }
    assert _codex_vars(out) == {CODEX_TWO: "cx2", "AH_CX_TOKEN_teamatx_io": "cx t'"}
    assert err == ""
    assert _files(tmp_path) == before


@pytest.mark.parametrize("setting", [None, ""])
def test_an_unset_codex_path_reads_the_secret_codex_login_writes(tmp_path, monkeypatch, capsys, setting):
    _environ(tmp_path, monkeypatch)
    if setting is not None:
        monkeypatch.setenv(CODEX_PATH_ENV, setting)
    seen = _openbao(monkeypatch, ACCOUNTS, codex=CODEX_ACCOUNTS)

    account_env.main(["env"])

    out, err = capsys.readouterr()
    assert seen[1][0] == f"http://bao:8200/v1/secret/data/{openbao_kv.CODEX_CREDS}"
    assert openbao_kv.CODEX_CREDS == "agentihooks/codex-creds"
    assert _codex_vars(out) == {CODEX_TWO: "cx2", "AH_CX_TOKEN_teamatx_io": "cx t'"}
    assert err == ""


def test_a_loaded_shell_holds_codex_access_tokens_but_no_refresh_token_or_authority(tmp_path, monkeypatch, capsys):
    shell = _codex_shell(tmp_path, monkeypatch, capsys)

    assert shell["AH_CX_TOKEN_teamatx_io"] == "cx t'"
    assert not [name for name in shell if name.startswith("AH_CX_REFRESH_")]
    assert "rt1" not in shell.values()
    assert "AGENTIHOOKS_AICODE_TOKEN_REFRESH" not in shell


def test_codex_refresh_in_a_loaded_shell_makes_no_grant_and_no_openbao_call(tmp_path, monkeypatch, capsys):
    shell = _codex_shell(tmp_path, monkeypatch, capsys)
    grants, requests = [], []
    environ = {
        **shell,
        "OPENBAO_URL": "http://bao:8200",
        "OPENBAO_TOKEN": "fixture-bao",
        "AGENTIHOOKS_AICODE_TOKEN_STORE": "openbao",
        CODEX_PATH_ENV: CODEX_PATH,
    }
    deps = codex_refresh.Deps(grant=grants.append, opener=lambda request, timeout=None: requests.append(request))

    assert codex_refresh.main([], environ, deps) == 0

    assert shell[CODEX_TWO] == "cx2"
    assert grants == [] and requests == []
    assert "not the refresh authority" in capsys.readouterr().out


def test_the_router_routes_among_every_codex_account_a_shell_loads(tmp_path, monkeypatch, capsys):
    shell = _codex_shell(tmp_path, monkeypatch, capsys)

    assert sorted(account.env_name for account in codex_router.token_accounts(shell)) == [
        CODEX_TWO,
        "AH_CX_TOKEN_teamatx_io",
    ]


def test_unreachable_openbao_falls_back_to_the_owner_only_codex_file(tmp_path, monkeypatch, capsys):
    _codex_environ(tmp_path, monkeypatch)
    _openbao(monkeypatch, error=URLError("refused"))
    _codex_fallback(tmp_path, "export AH_CX_TOKEN_oneatx_io='cx1'\nAH_CX_REFRESH_oneatx_io=rt1\nAH_CC_TOKEN_x=cc1\n")

    assert account_env.main(["env"]) == 0

    out, err = capsys.readouterr()
    assert _codex_vars(out) == {"AH_CX_TOKEN_oneatx_io": "cx1"}
    assert len(err.strip().splitlines()) == 2
    assert "codex.env" in err and "cx1" not in err


@pytest.mark.parametrize(
    "error",
    [URLError("refused"), TimeoutError("slow"), OSError("no route")],
)
def test_an_unreachable_openbao_costs_one_timeout_not_two(tmp_path, monkeypatch, capsys, error):
    _codex_environ(tmp_path, monkeypatch)
    seen = _openbao(monkeypatch, error=error)
    _fallback(tmp_path, "AH_CC_TOKEN_one=tok-1\n")
    _codex_fallback(tmp_path, "AH_CX_TOKEN_oneatx_io=cx1\n")

    assert account_env.main(["env"]) == 0

    out, err = capsys.readouterr()
    assert len(seen) == 1 and "codex" not in seen[0][0]
    assert _evaluated(out) == {"AH_CC_TOKEN_one": "tok-1"}
    assert _codex_vars(out) == {"AH_CX_TOKEN_oneatx_io": "cx1"}
    assert len(err.strip().splitlines()) == 2


@pytest.mark.parametrize(
    "answer",
    [HTTPError("http://bao", 403, "denied", None, None), BadStatusLine("garbled"), b"not json", b'{"data": {}}'],
)
def test_an_openbao_that_answers_the_claude_read_with_an_error_still_gets_the_codex_read(
    tmp_path, monkeypatch, capsys, answer
):
    _codex_environ(tmp_path, monkeypatch)
    seen = []

    def fake(request, timeout):
        seen.append(request.full_url)
        if "codex" not in request.full_url:
            if isinstance(answer, Exception):
                raise answer
            return _Response(answer)
        return _Response(json.dumps({"data": {"data": CODEX_BASE}}).encode())

    monkeypatch.setattr(account_env, "urlopen", fake)

    assert account_env.main(["env"]) == 0

    assert len(seen) == 2
    assert _codex_vars(capsys.readouterr().out) == CODEX_BASE


def test_a_machine_without_openbao_loads_the_codex_file(tmp_path, monkeypatch, capsys):
    _environ(tmp_path, monkeypatch, url="", bao="")
    _codex_fallback(tmp_path, "AH_CX_TOKEN_oneatx_io=cx1\n")

    account_env.main(["env"])

    out, err = capsys.readouterr()
    assert _codex_vars(out) == {"AH_CX_TOKEN_oneatx_io": "cx1"}
    assert len(err.strip().splitlines()) == 1


def test_a_codex_file_others_can_read_is_refused(tmp_path, monkeypatch, capsys):
    _environ(tmp_path, monkeypatch, url="", bao="")
    _codex_fallback(tmp_path, "AH_CX_TOKEN_oneatx_io=cx1\n", 0o644)

    account_env.main(["env"])

    out, err = capsys.readouterr()
    assert out == ""
    assert "600" in err and "codex.env" in err


def test_codex_accounts_keyed_by_email_export_under_the_shared_naming_form(tmp_path, monkeypatch, capsys):
    _codex_environ(tmp_path, monkeypatch)
    keyed = {
        "Ops.Two@Example.com": "cx2",
        "TEAM@X.IO": "cxT",
        "AH_CX_TOKEN_teamatx_io": "cxN",
        "no-at-sign": "cxZ",
        "Empty@X.IO": "",
        "AH_CX_REFRESH_teamatx_io": "rt1",
    }
    _openbao(monkeypatch, {}, codex=keyed)

    assert account_env.main(["env"]) == 0

    shell = _shell(capsys.readouterr().out)
    loaded = {name: value for name, value in shell.items() if name.startswith("AH_CX_")}
    assert account_names.codex_token_env("Ops.Two@Example.com") == CODEX_TWO
    assert loaded == {CODEX_TWO: "cx2", "AH_CX_TOKEN_teamatx_io": "cxN"}
    assert sorted(account.env_name for account in codex_router.token_accounts(shell)) == [
        CODEX_TWO,
        "AH_CX_TOKEN_teamatx_io",
    ]


def test_claude_accounts_keyed_by_email_stay_skipped(tmp_path, monkeypatch, capsys):
    _environ(tmp_path, monkeypatch)
    _openbao(monkeypatch, {"Ops@X.IO": "tok-e", "AH_CC_TOKEN_one": "tok-1"})

    account_env.main(["env"])

    assert _evaluated(capsys.readouterr().out) == {"AH_CC_TOKEN_one": "tok-1"}
