import json
import os
import re

import pytest

from scripts import master_claude_login as login
from scripts import openbao_kv

MASTER_ACCOUNT = {"emailAddress": "master@example.com"}
GRANT = {login.GRANT: {"refreshToken": "r"}}


@pytest.fixture(autouse=True)
def ah(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "MANAGED", tmp_path / "no-managed-settings.json")
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    return tmp_path / "ah"


@pytest.fixture
def rendered(tmp_path):
    out = tmp_path / "profiles" / "master" / "claude"
    out.mkdir(parents=True)
    (out / "settings.json").write_text("{}")
    (out / login.CONFIG).write_text("{}")
    return out


def _environ(rendered):
    return {
        "AGENTIHOOKS_SWARM": "rig",
        "AGENTIHOOKS_AGENT_NAME": "master@rig-1",
        "CLAUDE_CONFIG_DIR": str(rendered),
        "AH_CC_TOKEN_workeratexample_com": "x",
    }


def _legacy(ah, slug, logged_in=True):
    folder = ah / "master-claude" / slug
    (folder / "claude").mkdir(parents=True)
    (folder / login.OWNER).write_text("{}")
    if logged_in:
        (folder / "claude" / login.CREDENTIALS).write_text(json.dumps(GRANT))
        (folder / "claude" / login.CONFIG).write_text(json.dumps({login.ACCOUNT: MASTER_ACCOUNT}))
    return folder


def test_a_launch_with_no_login_anywhere_refuses_without_making_a_home(rendered, tmp_path):
    with pytest.raises(login.LoginRefused, match="^no master claude ai login: run agentihooks claude login --master$"):
        login.prepare(_environ(rendered), tmp_path)
    assert not login.home().exists()


def test_a_launch_reuses_the_global_login_and_leaves_a_legacy_one_alone(ah, rendered, tmp_path):
    home = login.build(rendered)
    (home / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": "global"}}))
    (home / login.CONFIG).write_text(json.dumps({login.ACCOUNT: MASTER_ACCOUNT}))
    legacy = _legacy(ah, "rig")
    target, found = login.prepare(_environ(rendered), tmp_path)
    assert target == home and found == "masteratexample_com"
    assert json.loads((target / login.CREDENTIALS).read_text()) == {login.GRANT: {"refreshToken": "global"}}
    assert (legacy / "claude" / login.CREDENTIALS).is_file()


def test_a_login_another_launch_wrote_after_the_offer_is_kept_over_a_legacy_one(ah, rendered, tmp_path, monkeypatch):
    home = login.build(rendered)
    (home / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": "global"}}))
    (home / login.CONFIG).write_text(json.dumps({login.ACCOUNT: MASTER_ACCOUNT}))
    legacy = _legacy(ah, "rig")
    monkeypatch.setattr(login, "_offered", lambda bao: {})
    target, found = login.prepare(_environ(rendered), tmp_path)
    assert target == home and found == "masteratexample_com"
    assert json.loads((target / login.CREDENTIALS).read_text()) == {login.GRANT: {"refreshToken": "global"}}
    assert (legacy / "claude" / login.CREDENTIALS).is_file()


def test_a_single_legacy_login_is_adopted_and_its_folder_removed(ah, rendered, tmp_path):
    legacy = _legacy(ah, "rig")
    _legacy(ah, "doctor", logged_in=False)
    target, found = login.prepare(_environ(rendered), tmp_path)
    assert target == login.home() == ah / "master-login" / "claude"
    assert found == "masteratexample_com"
    assert json.loads((target / login.CREDENTIALS).read_text()) == GRANT
    assert (target / "settings.json").is_file()
    assert not legacy.exists()
    assert (ah / "master-claude" / "doctor").is_dir()


def test_a_launch_replaces_an_empty_global_home_with_the_legacy_login(ah, rendered, tmp_path):
    login.build(rendered)
    legacy = _legacy(ah, "rig")
    target, found = login.prepare(_environ(rendered), tmp_path)
    assert found == "masteratexample_com"
    assert json.loads((target / login.CREDENTIALS).read_text()) == GRANT
    assert not legacy.exists()


def test_several_legacy_logins_refuse_naming_every_path(ah, rendered, tmp_path):
    first, second = _legacy(ah, "doctor"), _legacy(ah, "rig")
    message = f"several legacy master claude ai logins, keep one: {first / 'claude'}, {second / 'claude'}"
    with pytest.raises(login.LoginRefused, match=f"^{re.escape(message)}$"):
        login.prepare(_environ(rendered), tmp_path)
    assert not login.home().exists()
    assert (first / "claude" / login.CREDENTIALS).is_file() and (second / "claude" / login.CREDENTIALS).is_file()


def test_a_launch_with_only_the_recovery_copy_restores_it(rendered, tmp_path, monkeypatch):
    copy = {"grant": json.dumps(GRANT[login.GRANT]), "account": json.dumps(MASTER_ACCOUNT), "epoch": "0"}
    monkeypatch.setattr(login, "stored", lambda bao, number=None: copy if number == 1 else {})
    monkeypatch.setattr(login, "keep", lambda *_: False)
    target, found = login.prepare(_environ(rendered), tmp_path)
    assert found == "masteratexample_com"
    assert json.loads((target / login.CREDENTIALS).read_text()) == GRANT


def test_the_recovery_copy_wins_over_a_legacy_login(ah, rendered, tmp_path, monkeypatch):
    copy = {"grant": json.dumps({"refreshToken": "newer"}), "account": json.dumps(MASTER_ACCOUNT), "epoch": "0"}
    monkeypatch.setattr(login, "stored", lambda bao, number=None: copy if number == 1 else {})
    monkeypatch.setattr(login, "keep", lambda *_: False)
    legacy = _legacy(ah, "rig")
    target, _ = login.prepare(_environ(rendered), tmp_path)
    assert json.loads((target / login.CREDENTIALS).read_text()) == {login.GRANT: {"refreshToken": "newer"}}
    assert (legacy / "claude" / login.CREDENTIALS).is_file()


def test_a_legacy_login_held_by_a_live_master_refuses_naming_it(ah, rendered, tmp_path):
    legacy = _legacy(ah, "rig")
    (legacy / login.OWNER).write_text(json.dumps({"owner": "master@rig-1", "pid": os.getppid(), "epoch": 3}))
    message = f"the legacy master login {legacy / 'claude'} is held by master@rig-1, still running"
    with pytest.raises(login.LoginRefused, match=f"^{re.escape(message)}$"):
        login.prepare(_environ(rendered), tmp_path)
    assert (legacy / "claude" / login.CREDENTIALS).is_file()
    assert not login.home().exists()


def test_a_launch_beside_a_live_master_never_moves_a_legacy_login_into_its_home(ah, rendered, tmp_path):
    login.build(rendered)
    legacy = _legacy(ah, "rig")
    login.claim("master@doctor-1", os.getppid())
    with pytest.raises(login.LoginRefused, match="^no master claude ai login: run agentihooks claude login --master$"):
        login.prepare(_environ(rendered), tmp_path)
    assert (legacy / "claude" / login.CREDENTIALS).is_file()
    assert not (login.home() / login.CREDENTIALS).exists()
