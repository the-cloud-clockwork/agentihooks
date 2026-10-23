from pathlib import Path

import pytest

from scripts import aicode_tokens, codex_login, codex_logout, codex_router, openbao_step
from tests.codex_fakes import BAO_TOKEN, BAO_URL, FakeBao

BAO_ENV = {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": BAO_TOKEN}
TOKEN = "fake-team-token"
OTHER = "fake-other-token"


class Console:
    def __init__(self, tty=False):
        self.is_tty = tty

    def ask(self, prompt):
        raise AssertionError(prompt)

    def hidden(self, prompt):
        raise AssertionError(prompt)

    def tty(self):
        return self.is_tty


def _logout(args, environ=None, bao=None):
    console = Console()
    return codex_logout.main(
        args, {} if environ is None else environ, openbao_step.Console(console.ask, console.hidden, console.tty), bao
    )


def _registered(slug="team", names=("AH_CX_TOKEN_team",)):
    home = codex_login.homes() / slug
    home.mkdir(parents=True)
    (home / "auth.json").write_text("{}")
    lines = "".join(f"export {name}={TOKEN}\n" for name in names)
    aicode_tokens.rc_file().write_text(
        f"keep=1\n{aicode_tokens.START}\n{lines}export AH_CX_TOKEN_other={OTHER}\n"
        f"export {aicode_tokens.REFRESH_FLAG}=true\n{aicode_tokens.END}\n"
    )
    return home


def test_logout_removes_the_account_from_openbao_its_home_and_the_shell_block():
    home = _registered()
    bao = FakeBao({"AH_CX_TOKEN_team": TOKEN, "AH_CX_REFRESH_team": "fake-rt", "AH_CX_TOKEN_other": OTHER})
    assert _logout(["team", "--openbao"], BAO_ENV, bao) == 0
    assert bao.data == {"AH_CX_TOKEN_other": OTHER}
    assert not home.exists()
    assert aicode_tokens.rc_file().read_text() == (
        f"keep=1\n{aicode_tokens.START}\nexport AH_CX_TOKEN_other={OTHER}\n"
        f"export {aicode_tokens.REFRESH_FLAG}=true\n{aicode_tokens.END}\n"
    )


def test_logout_without_openbao_removes_only_the_local_registration():
    home = _registered()
    bao = FakeBao({"AH_CX_TOKEN_team": TOKEN})
    assert _logout(["team", "--no-openbao"], BAO_ENV, bao) == 0
    assert not bao.requests
    assert not home.exists()
    assert "AH_CX_TOKEN_team=" not in aicode_tokens.rc_file().read_text()


def test_a_failed_openbao_write_keeps_the_local_registration_and_prints_no_value(capsys):
    home = _registered()
    assert _logout(["team", "--openbao"], BAO_ENV, FakeBao({"AH_CX_TOKEN_team": TOKEN}, fail_writes=True)) == 1
    assert home.exists()
    assert "AH_CX_TOKEN_team=" in aicode_tokens.rc_file().read_text()
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_logout_also_removes_the_unnormalised_name_a_login_replaced():
    _registered("team_a", ("AH_CX_TOKEN_team-a", "AH_CX_TOKEN_team_a"))
    bao = FakeBao({"AH_CX_TOKEN_team-a": TOKEN, "AH_CX_REFRESH_team-a": "fake-rt", "AH_CX_TOKEN_team_a": TOKEN})
    assert _logout(["team-a", "--openbao"], BAO_ENV, bao) == 0
    assert bao.data == {}
    assert "team" not in aicode_tokens.rc_file().read_text().replace("keep", "")


def test_logout_reports_each_place_and_never_a_value(capsys):
    home = _registered()
    assert _logout(["team", "--no-openbao"]) == 0
    out = capsys.readouterr().out
    assert str(home) in out
    assert str(aicode_tokens.rc_file()) in out
    assert TOKEN not in out and OTHER not in out


def test_logout_of_an_unknown_account_changes_nothing_and_succeeds(capsys):
    assert _logout(["ghost", "--no-openbao"]) == 0
    assert not aicode_tokens.rc_file().exists()
    assert "AH_CX_TOKEN_ghost" in capsys.readouterr().out


def test_a_reserved_slug_is_refused():
    assert _logout(["default", "--no-openbao"]) == 2


@pytest.mark.parametrize("argv", [["logout", "team", "--no-openbao"], ["logout", "team"]])
def test_the_router_hands_logout_with_a_slug_to_the_logout_command(monkeypatch, argv):
    seen = []
    monkeypatch.setattr(codex_logout, "main", lambda args, environ: seen.append(args) or 0)
    assert codex_router.main(argv, environ={}, execvpe=lambda *a: pytest.fail("exec")) == 0
    assert seen == [argv[1:]]


def test_logout_without_a_slug_still_goes_to_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_logout, "main", lambda args, environ: pytest.fail("logout"))
    monkeypatch.setattr(codex_router, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(codex_router, "quotas", lambda accounts, environ: {})
    monkeypatch.setattr(codex_router, "default_signed_in", lambda environ, run=None: True)
    monkeypatch.setattr(codex_router.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("scripts.deps_preflight.refresh_codex_wrapper", lambda name: None)
    seen = []
    environ = {"HOME": str(tmp_path)}
    assert codex_router.main(["logout"], environ=environ, execvpe=lambda *a: seen.append(a[1])) == 0
    assert seen and seen[0][-1] == "logout"


def test_the_shell_block_removal_leaves_files_without_a_block_alone(tmp_path):
    rc = tmp_path / "rc"
    rc.write_text("export AH_CX_TOKEN_team=x\n")
    assert aicode_tokens.remove_exports(rc, ["AH_CX_TOKEN_team"]) is False
    assert rc.read_text() == "export AH_CX_TOKEN_team=x\n"
    assert aicode_tokens.remove_exports(Path(tmp_path / "missing"), ["AH_CX_TOKEN_team"]) is False
