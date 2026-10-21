import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import claude_login, openbao_kv, openbao_step
from scripts import master_claude_login as login


@pytest.fixture
def ah(tmp_path, monkeypatch):
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    monkeypatch.setattr(claude_login.shutil, "which", lambda name: f"/opt/{name}")
    rendered = tmp_path / "profiles" / "master" / "claude"
    rendered.mkdir(parents=True)
    (rendered / "settings.json").write_text("{}")
    return rendered


def _console(tty=False):
    return openbao_step.Console(lambda prompt: "", lambda prompt: "", lambda: tty)


def _deps(rendered, runs, code=0):
    def run(argv, env):
        runs.append((argv, env))
        return SimpleNamespace(returncode=code)

    return claude_login.Deps(
        console=_console(), run=run, render=lambda profile: rendered if profile == "master" else None
    )


def test_master_mode_runs_the_native_login_in_the_private_home_only(ah, capsys):
    runs = []
    environ = {
        "HOME": "/home/op",
        "CLAUDE_CODE_OAUTH_TOKEN": "x",
        "AH_CC_TOKEN_w": "x",
        "CLAUDE_CONFIG_DIR": "/home/op/.claude",
    }
    assert claude_login.master(False, environ, _deps(ah, runs)) == 0
    argv, env = runs[0]
    assert argv == ["/opt/claude", "--settings", '{"forceLoginMethod":"claudeai"}', "auth", "login"]
    home = str(login.home())
    assert env == {"HOME": "/home/op", "CLAUDE_CONFIG_DIR": home, "CLAUDE_SECURESTORAGE_CONFIG_DIR": home}
    assert (login.home() / "settings.json").read_text() == "{}"
    out = capsys.readouterr().out
    assert f"Sign in to the master claude ai account in the browser; the login stays in {home}" in out
    owner = json.loads((login.root() / login.OWNER).read_text())
    assert owner["holders"] == {claude_login.PROG: os.getpid()}


def test_master_mode_reports_a_failed_login_and_stores_nothing(ah, monkeypatch, capsys):
    monkeypatch.setattr(openbao_step, "target", lambda *_: pytest.fail("no store after a failed login"))
    assert claude_login.master(True, {}, _deps(ah, [], code=4)) == 4
    assert "claude auth login exited 4; the master login is unchanged" in capsys.readouterr().err


def test_master_mode_refuses_while_any_master_holds_the_login(ah, monkeypatch, capsys):
    login.claim("master@a-1", os.getppid())
    runs = []
    assert claude_login.master(False, {}, _deps(ah, runs)) == 1
    assert runs == []
    assert "held by master@a-1, still running" in capsys.readouterr().err


def test_master_mode_reports_a_missing_claude_binary(ah, capsys):
    def run(argv, env):
        raise FileNotFoundError(2, "No such file or directory")

    deps = claude_login.Deps(console=_console(), run=run, render=lambda profile: ah)
    assert claude_login.master(False, {}, deps) == 1
    assert "cannot run /opt/claude: No such file or directory" in capsys.readouterr().err


def test_master_mode_falls_back_to_claude_on_the_path(ah, monkeypatch):
    runs = []
    monkeypatch.setattr(claude_login.shutil, "which", lambda name: None)
    assert claude_login.master(False, {}, _deps(ah, runs)) == 0
    assert runs[0][0][0] == "claude"


def test_master_mode_stores_the_recovery_copy_when_asked(ah, monkeypatch, capsys):
    stored = []
    deps = replace(_deps(ah, []), opener="opener")
    monkeypatch.setattr(
        openbao_step,
        "target",
        lambda choice, environ, console, opener: (
            "bao" if (choice, environ, console, opener) == (True, {"A": "1"}, deps.console, "opener") else None
        ),
    )
    monkeypatch.setattr(login, "publish", lambda bao, target, epoch: stored.append((bao, target, epoch)))
    assert claude_login.master(True, {"A": "1"}, deps) == 0
    assert stored == [("bao", login.home(), 1)]
    assert "Storing the master login recovery copy in OpenBao" in capsys.readouterr().out


def test_master_mode_reports_a_refused_or_failed_store(ah, monkeypatch, capsys):
    def refused(*_):
        raise openbao_step.StoreRefused("needs both")

    monkeypatch.setattr(openbao_step, "target", refused)
    assert claude_login.master(True, {}, _deps(ah, [])) == 1
    assert "needs both" in capsys.readouterr().err

    def failed(*_):
        raise openbao_kv.OpenBaoError(503, "sealed")

    monkeypatch.setattr(openbao_step, "target", lambda *_: "bao")
    monkeypatch.setattr(login, "publish", failed)
    assert claude_login.master(True, {}, _deps(ah, [])) == 1
    assert "OpenBao HTTP 503: sealed" in capsys.readouterr().err


def test_the_master_flag_routes_main_to_master_mode(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(claude_login, "master", lambda *args: seen.append(args) or 0)
    console = _console()
    assert claude_login.main(["--master", "--openbao"], {"A": "1"}, console=console, opener="op") == 0
    openbao, environ, deps, slot = seen[0]
    assert (openbao, environ, deps.console, deps.opener, slot) == (True, {"A": "1"}, console, "op", None)
    with pytest.raises(SystemExit):
        claude_login.main(["--master", "rig"], {}, console=console)
    assert len(seen) == 1
    assert claude_login.MASTER_SLUG in capsys.readouterr().err


def test_without_the_master_flag_the_setup_token_login_is_unchanged(monkeypatch):
    monkeypatch.setattr(claude_login, "master", lambda *args: pytest.fail("master mode"))
    assert claude_login.main([], {}, console=_console(tty=False)) == 2


def test_the_default_render_renders_the_profile_and_returns_its_claude_home(monkeypatch, tmp_path):
    from scripts.profiles import render

    rendered = []
    monkeypatch.setattr(render, "render_claude", rendered.append)
    monkeypatch.setattr(render, "profile_dir", lambda name: tmp_path / name)
    assert claude_login._rendered("master") == tmp_path / "master" / "claude"
    assert rendered == ["master"]
    assert claude_login.Deps(console=_console()).render is None


def test_a_slot_login_builds_its_own_home_and_becomes_the_active_master_login(ah, monkeypatch):
    monkeypatch.setenv(login.SLOT_ENV, "1")
    runs = []
    assert claude_login.master(False, {"HOME": "/home/op"}, _deps(ah, runs), 2) == 0
    base = Path(login.hooks_config.AGENTIHOOKS_HOME)
    home = str(base / "master-login-2" / "claude")
    assert runs[0][1]["CLAUDE_CONFIG_DIR"] == home
    monkeypatch.delenv(login.SLOT_ENV)
    assert login.slot() == 2
    assert login.home() == base / "master-login-2" / "claude"
    assert login.creds() == "agentihooks/master-claude-login-2"


def test_a_failed_slot_login_keeps_the_active_master_login(ah, monkeypatch):
    monkeypatch.setenv(login.SLOT_ENV, "1")
    assert claude_login.master(False, {"HOME": "/home/op"}, _deps(ah, [], code=1), 3) == 1
    monkeypatch.delenv(login.SLOT_ENV)
    assert login.slot() == 1
    assert login.home() == Path(login.hooks_config.AGENTIHOOKS_HOME) / "master-login" / "claude"


def test_a_slot_outside_the_cap_is_refused_before_any_login(ah, monkeypatch, capsys):
    monkeypatch.setenv(login.SLOT_ENV, "1")
    runs = []
    assert claude_login.master(False, {"HOME": "/home/op"}, _deps(ah, runs), login.MAX_SLOTS + 1) == 2
    assert runs == []
    assert "outside 1 to" in capsys.readouterr().err


def test_slot_needs_the_master_flag():
    with pytest.raises(SystemExit):
        claude_login.main(["--slot", "2"])
