import json
import subprocess
from pathlib import Path

import pytest

from scripts import aicode_tokens, codex_login
from tests.codex_fakes import BAO_TOKEN, BAO_URL, FakeBao, access_token

ACCESS = access_token(10, workspace="ws-team", plan="team")
REFRESH = "rt-value-1"
TYPED = "typed-bao"
TYPED_URL = "https://typed.test"
CODEX_FILE_STORE = ["-c", 'cli_auth_credentials_store="file"']
BAO_ENV = {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": BAO_TOKEN}
ENV = {
    "PATH": "/usr/bin",
    "AH_CX_TOKEN_other": "cx-oth",
    "AH_CC_TOKEN_home": "cc-v",
    "CODEX_ACCESS_TOKEN": "cx-old",
    "AGENTIHOOKS_CHATGPT_ACCOUNT_ID": "ws-old",
    "OPENAI_API_KEY": "sk-oa",
    "CODEX_API_KEY": "sk-cx",
}
ASK_STORE = "Do you want to store them in OpenBao? [y/N] "
ASK_DETECTED = f"OpenBao credentials detected ({BAO_URL}). Use them? [Y/n] "
ASK_TYPED = ["OpenBao URL: ", "OpenBao token: "]


class Console:
    def __init__(self, answers=(), tty=True, hidden=TYPED):
        self.answers, self.prompts, self.is_tty, self.secret = list(answers), [], tty, hidden

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def hidden(self, prompt):
        self.prompts.append(prompt)
        return self.secret

    def tty(self):
        return self.is_tty


def _codex(tokens=None, returncode=0):
    seen = {}

    def run(argv, env, **kwargs):
        seen.update(argv=argv, env=env)
        if not returncode:
            auth = {"tokens": {"access_token": ACCESS, "refresh_token": REFRESH} if tokens is None else tokens}
            (Path(env["CODEX_HOME"]) / "auth.json").write_text(json.dumps(auth))
        return subprocess.CompletedProcess(argv, returncode)

    return run, seen


@pytest.fixture(autouse=True)
def _which(monkeypatch):
    monkeypatch.setattr(codex_login.shutil, "which", lambda name: f"/usr/bin/{name}")


def _login(args, environ=ENV, run=None, console=None, bao=None):
    run = run or _codex()[0]
    console = console or Console(tty=False)
    return codex_login.main(args, environ, run, codex_login.Console(console.ask, console.hidden, console.tty), bao)


def _home(slug):
    return Path.home() / ".agentihooks" / "codex" / slug


def test_login_runs_codex_in_the_account_home_with_no_other_credential():
    run, seen = _codex()
    assert _login(["team-a", "--no-openbao"], run=run) == 0
    assert seen["argv"] == ["/usr/bin/codex", "login", *CODEX_FILE_STORE]
    assert seen["env"]["CODEX_HOME"] == str(_home("team_a"))
    assert seen["env"]["AGENTIHOOKS_INTERNAL_RUN"] == "1"
    assert seen["env"]["PATH"] == "/usr/bin"
    assert not (set(ENV) - {"PATH"}) & set(seen["env"])
    assert _home("team_a").stat().st_mode & 0o777 == 0o700


def test_device_auth_asks_codex_for_the_device_flow():
    run, seen = _codex()
    assert _login(["team", "--device-auth", "--no-openbao"], run=run) == 0
    assert seen["argv"] == ["/usr/bin/codex", "login", "--device-auth", *CODEX_FILE_STORE]


def test_a_login_writes_the_token_and_the_refresh_flag_and_prints_no_value(capsys):
    assert _login(["team-a", "--no-openbao"]) == 0
    rc = aicode_tokens.rc_file()
    text = rc.read_text()
    assert f"export AH_CX_TOKEN_team_a={ACCESS}\n" in text
    assert "export AGENTIHOOKS_AICODE_TOKEN_REFRESH=true\n" in text
    out = capsys.readouterr()
    assert all(part in out.out for part in ("team_a", "ws-team", "team", f"source {rc}"))
    assert ACCESS not in out.out + out.err and REFRESH not in out.out + out.err


def test_a_login_reusing_a_hyphenated_slug_removes_its_hyphen_form_everywhere():
    rc = aicode_tokens.rc_file()
    rc.write_text(f"{aicode_tokens.START}\nexport AH_CX_TOKEN_team-a=old\n{aicode_tokens.END}\n")
    bao = FakeBao({"AH_CX_TOKEN_team-a": "old", "AH_CX_REFRESH_team-a": "old-rt", "OTHER": "o"})
    assert _login(["team-a", "--openbao"], environ={**ENV, **BAO_ENV}, bao=bao) == 0
    assert "team-a" not in rc.read_text()
    assert bao.data == {"OTHER": "o", "AH_CX_TOKEN_team_a": ACCESS, "AH_CX_REFRESH_team_a": REFRESH}


def test_a_failed_login_exits_with_the_codex_code_and_writes_nothing():
    bao = FakeBao()
    assert _login(["team", "--openbao"], environ={**ENV, **BAO_ENV}, run=_codex(returncode=7)[0], bao=bao) == 7
    assert not aicode_tokens.rc_file().exists() and not bao.requests
    assert not _home("team").exists()
    _home("kept").mkdir(parents=True)
    assert _login(["kept", "--no-openbao"], run=_codex(returncode=7)[0]) == 7
    assert _home("kept").is_dir()


def test_a_missing_codex_binary_fails_the_login_with_one_line(capsys):
    def run(argv, env, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    assert _login(["team", "--no-openbao"], run=run) == 1
    assert "cannot run codex" in capsys.readouterr().err
    assert not _home("team").exists()


def test_a_home_that_cannot_be_removed_after_a_failed_login_does_not_crash(monkeypatch):
    def rmtree(path, ignore_errors=False):
        if not ignore_errors:
            raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(codex_login.shutil, "rmtree", rmtree)
    assert _login(["team", "--no-openbao"], run=_codex(returncode=7)[0]) == 7


def test_a_login_that_leaves_no_access_token_fails():
    assert _login(["team", "--no-openbao"], run=_codex(tokens={"refresh_token": REFRESH})[0]) != 0
    assert not aicode_tokens.rc_file().exists()
    assert not _home("team").exists()


def test_a_bad_slug_is_refused_before_codex_runs():
    def run(argv, env, **kwargs):
        raise AssertionError("codex must not run")

    assert _login(["_bad"], run=run) == 2
    assert _login(["default"], run=run) == 2


@pytest.mark.parametrize(
    "args, environ, console, stored_at, prompts",
    [
        (["--openbao"], BAO_ENV, Console(tty=True), BAO_URL, []),
        (["--no-openbao"], BAO_ENV, Console(tty=True), None, []),
        ([], BAO_ENV, Console(["n"]), None, [ASK_STORE]),
        ([], BAO_ENV, Console(["y", ""]), BAO_URL, [ASK_STORE, ASK_DETECTED]),
        ([], BAO_ENV, Console(["y", "n", TYPED_URL]), TYPED_URL, [ASK_STORE, ASK_DETECTED, *ASK_TYPED]),
        ([], {}, Console(["y", TYPED_URL]), TYPED_URL, [ASK_STORE, *ASK_TYPED]),
        ([], BAO_ENV, Console(tty=False), None, []),
    ],
)
def test_the_openbao_step_follows_the_flags_the_terminal_and_the_answers(args, environ, console, stored_at, prompts):
    typed = stored_at == TYPED_URL
    bao = FakeBao(url=stored_at or BAO_URL, token=TYPED if typed else BAO_TOKEN)
    assert _login(["team", *args], environ={**ENV, **environ}, console=console, bao=bao) == 0
    assert console.prompts == prompts
    assert bool(bao.writes) == bool(stored_at)
    if stored_at:
        assert bao.data == {"AH_CX_TOKEN_team": ACCESS, "AH_CX_REFRESH_team": REFRESH}
    assert TYPED not in aicode_tokens.rc_file().read_text()


def test_off_a_terminal_without_a_flag_the_openbao_step_is_skipped_and_said(capsys):
    assert _login(["team"], environ={**ENV, **BAO_ENV}, bao=FakeBao()) == 0
    assert "skipping OpenBao" in capsys.readouterr().out


def test_a_failed_openbao_write_fails_the_login_without_printing_a_value(capsys):
    bao = FakeBao(fail_writes=True)
    assert _login(["team", "--openbao"], environ={**ENV, **BAO_ENV}, bao=bao) == 1
    out = capsys.readouterr()
    assert "OpenBao write failed" in out.out
    assert ACCESS not in out.out + out.err and REFRESH not in out.out + out.err


def test_a_login_prints_where_the_token_went_and_what_it_belongs_to(capsys):
    assert _login(["team", "--openbao"], environ={**ENV, **BAO_ENV}, bao=FakeBao()) == 0
    rc = aicode_tokens.rc_file()
    assert capsys.readouterr().out == (
        f"AH_CX_TOKEN_team written to {rc}\n"
        "  workspace ws-team, plan team\n"
        "  expires 2027-01-25 08:00 UTC\n"
        f"run: source {rc}\n"
        "OpenBao secret/agentihooks/codex-creds: wrote AH_CX_REFRESH_team, AH_CX_TOKEN_team (2 keys total)\n"
    )


def test_a_token_without_claims_is_described_as_unknown(capsys):
    assert _login(["team", "--no-openbao"], run=_codex(tokens={"access_token": "opaque"})[0]) == 0
    assert "  workspace unknown, plan unknown\n  expires unknown\n" in capsys.readouterr().out


def test_codex_runs_by_name_when_it_is_not_on_the_path(monkeypatch):
    monkeypatch.setattr(codex_login.shutil, "which", lambda name: None)
    run, seen = _codex()
    assert _login(["team", "--no-openbao"], run=run) == 0
    assert seen["argv"][0] == "codex"


def test_a_bad_slug_is_refused_with_the_rule_on_stderr(capsys):
    assert _login(["_bad"]) == 2
    assert capsys.readouterr().err == f"agentihooks codex login: {aicode_tokens.SLUG_RULE}: '_bad'\n"


def test_a_typed_url_without_a_token_is_refused(capsys):
    assert _login(["team"], console=Console(["y", TYPED_URL], hidden="")) == 1
    assert capsys.readouterr().err == "agentihooks codex login: OpenBao needs both a URL and a token\n"


def test_the_refusal_without_openbao_variables_names_both(capsys):
    assert _login(["team", "--openbao"], console=Console(tty=False)) == 1
    assert capsys.readouterr().err == (
        "agentihooks codex login: --openbao needs OPENBAO_URL and OPENBAO_TOKEN off a terminal\n"
    )


def test_a_missing_token_and_a_missing_codex_say_so_on_stderr(capsys):
    assert _login(["team", "--no-openbao"], run=_codex(tokens={})[0]) == 1
    home = _home("team")
    assert capsys.readouterr().err == f"agentihooks codex login: no tokens.access_token in {home / 'auth.json'}\n"

    def run(argv, env, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    assert _login(["team", "--no-openbao"], run=run) == 1
    assert capsys.readouterr().err == "agentihooks codex login: cannot run codex: No such file or directory\n"


def test_off_a_terminal_the_skip_is_said_in_full(capsys):
    assert _login(["team"], environ={**ENV, **BAO_ENV}) == 0
    assert capsys.readouterr().out.endswith(
        "not a terminal: skipping OpenBao (pass --openbao to store the tokens there)\n"
    )


def test_an_unknown_flag_shows_the_login_usage(capsys):
    with pytest.raises(SystemExit):
        _login(["team", "--bogus"])
    assert capsys.readouterr().err.startswith("usage: agentihooks codex login")
