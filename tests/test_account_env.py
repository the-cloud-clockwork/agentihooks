import io
import json
import os
import subprocess
from pathlib import Path
from urllib.error import URLError

import pytest

from scripts import account_env, install
from scripts.cli_delegates import delegated_cli

pytestmark = pytest.mark.unit

LOADER = 'command -v agentihooks >/dev/null && eval "$(agentihooks accounts env)"'

ACCOUNTS = {
    "OPS+2026@TCC.DEV": "tok b'quoted",
    "NESTOR.COLT@GMAIL.COM": "tok-a",
    "EMPTY@X.IO": "",
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _openbao(monkeypatch, payload=None, error=None):
    seen = []

    def fake(request, timeout):
        seen.append((request.full_url, request.get_header("X-vault-token"), timeout))
        if error:
            raise error
        return _Response(json.dumps({"data": {"data": payload, "metadata": {"version": 1}}}).encode())

    monkeypatch.setattr(account_env, "urlopen", fake)
    return seen


def _environ(tmp_path, monkeypatch, url="http://bao:8200/", bao="fixture-bao"):
    for name in ("OPENBAO_URL", "OPENBAO_TOKEN", "AGENTIHOOKS_HOME"):
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


def _evaluated(script: str) -> dict[str, str]:
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", 'eval "$1"; env -0', "_", script],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        check=True,
    )
    pairs = (item.decode().partition("=") for item in done.stdout.split(b"\0") if item)
    return {name: value for name, _, value in pairs if name.startswith("AH_CC_TOKEN_")}


def _files(root: Path) -> dict[str, bytes]:
    return {str(path): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_openbao_accounts_print_one_export_per_email_under_its_lower_case_name(tmp_path, monkeypatch, capsys):
    _environ(tmp_path, monkeypatch)
    seen = _openbao(monkeypatch, ACCOUNTS)

    assert account_env.main(["env"]) == 0

    out, err = capsys.readouterr()
    assert seen == [("http://bao:8200/v1/secret/data/k8s/swarm-claude-creds", "fixture-bao", 3)]
    assert _evaluated(out) == {
        "AH_CC_TOKEN_nestor_coltatgmail_com": "tok-a",
        "AH_CC_TOKEN_ops_2026attcc_dev": "tok b'quoted",
    }
    assert [line.partition("=")[0] for line in out.splitlines()] == [
        "export AH_CC_TOKEN_nestor_coltatgmail_com",
        "export AH_CC_TOKEN_ops_2026attcc_dev",
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
    assert len(err.strip().splitlines()) == 1
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
    assert len(err.strip().splitlines()) == 1


def test_a_machine_without_openbao_or_fallback_stays_silent(tmp_path, monkeypatch, capsys):
    _environ(tmp_path, monkeypatch, url="", bao="")

    assert account_env.main(["env"]) == 0

    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("payload", [None, [], "text", {"X@Y.IO": 7}])
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
