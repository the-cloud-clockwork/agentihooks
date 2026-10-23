from pathlib import Path

import pytest

from scripts import aicode_tokens, codex_login, codex_logout, codex_router, openbao_step
from tests.codex_fakes import BAO_TOKEN, BAO_URL, FakeBao

BAO_ENV = {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": BAO_TOKEN}
TOKEN = "fake-team-token"
OTHER = "fake-other-token"


class Console:
    def __init__(self, tty=False, answers=()):
        self.is_tty, self.answers, self.prompts = tty, list(answers), []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def hidden(self, prompt):
        raise AssertionError(prompt)

    def tty(self):
        return self.is_tty


def _logout(args, environ=None, bao=None, console=None):
    console = console or Console()
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
    monkeypatch.setattr(codex_logout, "main", lambda args, environ: seen.append((args, environ)) or 0)
    assert codex_router.main(argv, environ={"MARK": "1"}, execvpe=lambda *a: pytest.fail("exec")) == 0
    assert seen == [(argv[1:], {"MARK": "1"})]


@pytest.mark.parametrize("argv", [["logout"], ["logout", "--help"], ["logout", "status"]])
def test_logout_without_a_slug_still_goes_to_codex(monkeypatch, argv):
    monkeypatch.setattr(codex_logout, "main", lambda args, environ: pytest.fail("logout"))
    assert codex_router._own_command(argv, {}) is None


def test_the_shell_block_removal_leaves_files_without_a_block_alone(tmp_path):
    rc = tmp_path / "rc"
    rc.write_text("export AH_CX_TOKEN_team=x\n")
    assert aicode_tokens.remove_exports(rc, ["AH_CX_TOKEN_team"]) is False
    assert rc.read_text() == "export AH_CX_TOKEN_team=x\n"
    assert aicode_tokens.remove_exports(Path(tmp_path / "missing"), ["AH_CX_TOKEN_team"]) is False


def test_a_block_without_the_account_is_left_alone_and_reported_as_absent(capsys):
    _registered()
    before = aicode_tokens.rc_file().read_text()
    assert aicode_tokens.remove_exports(aicode_tokens.rc_file(), ["AH_CX_TOKEN_ghost"]) is False
    assert _logout(["ghost", "--no-openbao"]) == 0
    assert aicode_tokens.rc_file().read_text() == before
    assert "no export of AH_CX_TOKEN_ghost" in capsys.readouterr().out


def test_the_removal_reports_success_and_keeps_the_file_mode():
    _registered()
    rc = aicode_tokens.rc_file()
    rc.chmod(0o600)
    assert aicode_tokens.remove_exports(rc, ["AH_CX_TOKEN_team"]) is True
    assert rc.stat().st_mode & 0o777 == 0o600


def test_each_place_is_named_in_the_report(capsys):
    _registered("team_a", ("AH_CX_TOKEN_team-a", "AH_CX_TOKEN_team_a"))
    bao = FakeBao({"AH_CX_TOKEN_team_a": TOKEN, "AH_CX_REFRESH_team_a": "fake-rt", "AH_CX_TOKEN_other": OTHER})
    assert _logout(["team-a", "--openbao"], BAO_ENV, bao) == 0
    out = capsys.readouterr().out.splitlines()
    rc = aicode_tokens.rc_file()
    assert out == [
        "OpenBao secret/agentihooks/codex-creds: removed AH_CX_REFRESH_team-a, AH_CX_REFRESH_team_a, "
        "AH_CX_TOKEN_team-a, AH_CX_TOKEN_team_a (1 keys left)",
        f"removed {codex_login.homes() / 'team_a'}",
        f"removed AH_CX_TOKEN_team_a, AH_CX_TOKEN_team-a from {rc}",
        "run: unset AH_CX_TOKEN_team_a AH_CX_TOKEN_team-a",
    ]


def test_an_unknown_account_names_both_absent_places(capsys):
    _registered()
    assert _logout(["ghost-x", "--no-openbao"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"no account home at {codex_login.homes() / 'ghost_x'}",
        f"no export of AH_CX_TOKEN_ghost_x, AH_CX_TOKEN_ghost-x in the token block of {aicode_tokens.rc_file()}",
    ]


def test_refusals_go_to_stderr_with_the_command_name_and_the_reason(capsys):
    assert _logout(["default"]) == 2
    err = capsys.readouterr()
    assert "agentihooks codex logout: " in err.err and aicode_tokens.SLUG_RULE in err.err and not err.out
    assert _logout(["team", "--openbao"]) == 1
    err = capsys.readouterr()
    assert f"agentihooks codex logout: {openbao_step.NEEDS_ENV}" in err.err


def test_a_failed_openbao_write_is_named(capsys):
    _registered()
    assert _logout(["team", "--openbao"], BAO_ENV, FakeBao({"AH_CX_TOKEN_team": TOKEN}, fail_writes=True)) == 1
    assert "OpenBao write failed: " in capsys.readouterr().out


def test_usage_names_the_command(capsys):
    with pytest.raises(SystemExit):
        _logout([])
    assert "usage: agentihooks codex logout" in capsys.readouterr().err


def test_no_openbao_on_a_terminal_asks_nothing():
    _registered()
    console = Console(tty=True)
    assert _logout(["team", "--no-openbao"], BAO_ENV, FakeBao(), console) == 0
    assert console.prompts == []


def test_without_a_flag_a_terminal_is_asked_and_the_answer_is_used():
    _registered()
    bao = FakeBao({"AH_CX_TOKEN_team": TOKEN, "AH_CX_TOKEN_other": OTHER})
    console = Console(tty=True, answers=["y", ""])
    assert _logout(["team"], BAO_ENV, bao, console) == 0
    assert len(console.prompts) == 2
    assert bao.data == {"AH_CX_TOKEN_other": OTHER}


def test_without_a_console_off_a_terminal_openbao_is_skipped(monkeypatch):
    _registered()
    monkeypatch.setattr(openbao_step.sys.stdin, "isatty", lambda: False, raising=False)
    assert codex_logout.main(["team"], {}) == 0
    assert "AH_CX_TOKEN_team=" not in aicode_tokens.rc_file().read_text()
