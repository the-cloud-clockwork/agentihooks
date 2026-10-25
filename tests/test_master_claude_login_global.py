import json
from types import SimpleNamespace

import pytest

from scripts import master_claude_login as login
from scripts import openbao_kv
from scripts.profiles import binding
from scripts.swarm.runtime import HerdrRuntime
from tests.swarm.test_runtime import _passed, validated

MASTER_ACCOUNT = {"emailAddress": "master@example.com"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from scripts.routing import native_login

    monkeypatch.setattr(login, "MANAGED", tmp_path / "no-managed-settings.json")
    monkeypatch.setattr(login.hooks_config, "MASTER_CLAUDE_LOGIN", True)
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    monkeypatch.setenv("SWARM_HIVE_ID", "hive-a")
    monkeypatch.setattr(native_login, "check", lambda *_: {"loggedIn": True, "authMethod": "claude.ai"})


@pytest.fixture
def ah(tmp_path, monkeypatch):
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    return tmp_path / "ah"


@pytest.fixture
def rendered(tmp_path):
    out = tmp_path / "profiles" / "master" / "claude"
    out.mkdir(parents=True)
    (out / "settings.json").write_text("{}")
    (out / login.CONFIG).write_text("{}")
    return out


@pytest.mark.parametrize("slug", ["rig", "doctor"])
def test_the_login_reads_only_the_setting_and_the_front_door_seat(monkeypatch, slug):
    assert login.enabled(slug, f"master@{slug}", "master", "claude") is True
    assert login.enabled(slug, "", "master", "claude") is True
    for seat, lane, harness in (
        (f"master-2@{slug}", "master", "claude"),
        (f"master-3@{slug}", "master", "claude"),
        (f"eng-1@{slug}", "eng", "claude"),
        ("", "eng", "claude"),
        (f"master@{slug}", "master", "codex"),
    ):
        assert login.enabled(slug, seat, lane, harness) is False
    monkeypatch.setattr(login.hooks_config, "MASTER_CLAUDE_LOGIN", False)
    assert login.enabled(slug, f"master@{slug}", "master", "claude") is False


def test_the_setting_defaults_off_and_reads_the_environment(monkeypatch):
    from hooks import config

    monkeypatch.delenv("AGENTIHOOKS_MASTER_CLAUDE_LOGIN", raising=False)
    assert config._env_bool("AGENTIHOOKS_MASTER_CLAUDE_LOGIN", "false") is False
    monkeypatch.setenv("AGENTIHOOKS_MASTER_CLAUDE_LOGIN", "true")
    assert config._env_bool("AGENTIHOOKS_MASTER_CLAUDE_LOGIN", "false") is True


def _environ(rendered, slug):
    return {
        "AGENTIHOOKS_SWARM": slug,
        "AGENTIHOOKS_AGENT_NAME": f"master@{slug}-1",
        "CLAUDE_CONFIG_DIR": str(rendered),
        "AH_CC_TOKEN_workeratexample_com": "x",
    }


def test_several_primary_masters_hold_the_login_at_once_and_one_name_once(ah):
    assert login.claim("master@rig-1", 10, lambda pid: True) == 1
    assert login.claim("master@doctor-1", 20, lambda pid: True) == 2
    holders = json.loads((login.root() / login.OWNER).read_text())["holders"]
    assert holders == {"master@rig-1": 10, "master@doctor-1": 20}
    with pytest.raises(login.LoginRefused, match="^the master login is held by master@rig-1, still running$"):
        login.claim("master@rig-1", 30, lambda pid: True)
    assert login.claim("master@rig-1", 30, lambda pid: pid != 10) == 3


def test_a_front_door_joins_a_legacy_exclusive_live_master(ah, tmp_path, rendered):
    import os

    home = login.build(rendered)
    (home / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": "r"}}))
    (home / login.CONFIG).write_text(json.dumps({login.ACCOUNT: MASTER_ACCOUNT, "live": "rig"}))
    login.claim("master@rig-1", os.getppid(), alone=True)
    assert login.prepare(_environ(rendered, "doctor"), tmp_path) == (home, "masteratexample_com")
    assert login.holders(login.root()) == {"master@rig-1": os.getppid(), "master@doctor-1": os.getpid()}
    assert "alone" not in json.loads((login.root() / login.OWNER).read_text())
    assert json.loads((home / login.CONFIG).read_text())["live"] == "rig"


def _render(rendered, text):
    (rendered / "CLAUDE.md").write_text(binding.persona(text))
    (rendered.parent / "claude.sources.json").write_text("[]")
    binding.write(rendered, "master", "claude")


def _doctor_home(rendered):
    _render(rendered, "Doctor master.\n")
    home = login.build(rendered)
    (home / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": "r"}}))
    (home / login.CONFIG).write_text(json.dumps({login.ACCOUNT: MASTER_ACCOUNT}))
    _render(rendered, "Rig master.\n")
    return home


def test_a_second_front_door_reuses_the_live_home_and_rehomes_its_own_report(ah, tmp_path, rendered):
    import os

    home = _doctor_home(rendered)
    login.claim("master@doctor-1", os.getppid())
    evidence = (home / binding.FILE).read_bytes()
    report = tmp_path / "report.json"
    binding.request(report, "master", "claude", rendered)
    assert login.prepare({**_environ(rendered, "rig"), binding.REPORT: str(report)}, tmp_path)[0] == home
    assert (home / binding.FILE).read_bytes() == evidence
    assert json.loads(report.read_text())["rendered"] == json.loads(evidence)


def test_a_launch_waits_out_a_running_login_command(ah, tmp_path, rendered):
    import os

    login.claim("agentihooks claude login", os.getppid(), alone=True)
    with pytest.raises(login.LoginRefused, match="^no master claude ai login: run agentihooks claude login --master$"):
        login.prepare(_environ(rendered, "rig"), tmp_path)
    assert not login.home().exists()
    assert login.claim("master@rig-1", 10, lambda pid: pid == 10) == 2


def test_two_front_doors_launch_without_token_fallback_and_share_refreshed_credentials(
    ah, tmp_path, rendered, monkeypatch
):
    import os
    from pathlib import Path

    from scripts import master_login_chain as chain

    _render(rendered, "Master.\n")
    home = login.build(rendered)
    (home / login.CREDENTIALS).write_text(json.dumps({login.GRANT: {"refreshToken": "old", "expiresAt": 1}}))
    (home / login.CONFIG).write_text(json.dumps({login.ACCOUNT: MASTER_ACCOUNT}))
    monkeypatch.setattr(chain, "authorize", lambda *_: pytest.fail("valid stored login must not authorize"))
    first_env = _environ(rendered, "rig")
    with monkeypatch.context() as patch:
        patch.setattr(login.os, "getpid", os.getppid)
        first, first_account = login.prepare_launch(first_env, tmp_path)
    first_child = login.child(first_env, first, first_account)
    refreshed = {login.GRANT: {"refreshToken": "refreshed", "expiresAt": 2}}
    Path(first_child["CLAUDE_CONFIG_DIR"], login.CREDENTIALS).write_text(json.dumps(refreshed))
    stale = {
        "grant": json.dumps({"refreshToken": "old", "expiresAt": 1}),
        "generation": "999",
        "account": json.dumps(MASTER_ACCOUNT),
        "epoch": "0",
    }
    monkeypatch.setattr(login, "stored", lambda *_: stale)
    second_env = _environ(rendered, "doctor")
    second, second_account = login.prepare_launch(second_env, tmp_path)
    second_child = login.child(second_env, second, second_account)
    assert (first, first_account) == (second, second_account)
    assert first_child["CLAUDE_CONFIG_DIR"] == second_child["CLAUDE_CONFIG_DIR"]
    assert json.loads(Path(second_child["CLAUDE_CONFIG_DIR"], login.CREDENTIALS).read_text()) == refreshed
    assert not any(key.startswith("AH_CC_TOKEN_") for key in second_child)
    assert login.holders(login.root()) == {"master@rig-1": os.getppid(), "master@doctor-1": os.getpid()}


def _spawn(tmp_path, lane, seat, kind="interactive"):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={lane: {"agent": "claude"}},
        autonomy="delegate",
    )
    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    runtime.spawn(config, lane, "agent", {"id": lane, "title": "proof", "route_kind": kind, "seat": seat})
    return _passed(seen[0])


def test_the_front_door_master_launches_on_the_master_login(tmp_path):
    assert _spawn(tmp_path, "master", "master@proof")[:2] == ["--route", "master-login"]


@pytest.mark.parametrize(("lane", "seat"), [("master", "master-2@proof"), ("eng", "eng-1@proof")])
def test_a_pool_master_and_an_engineer_stay_on_their_routes(tmp_path, lane, seat):
    assert "master-login" not in _spawn(tmp_path, lane, seat)


@pytest.mark.parametrize("kind", ["subscription", "api"])
def test_a_front_door_order_on_an_explicit_route_takes_the_master_login(tmp_path, kind):
    assert _spawn(tmp_path, "master", "master@proof", kind)[:2] == ["--route", "master-login"]
