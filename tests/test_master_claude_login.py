import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import master_claude_login as login
from scripts import openbao_kv
from scripts.profiles import binding
from scripts.routing import master_account, reservation
from scripts.routing.master_account import MasterAccount
from scripts.routing.settings import FileSettings

OPERATOR_ACCOUNT = {"emailAddress": "operator@example.com"}
MASTER_ACCOUNT = {"emailAddress": "master@example.com"}
GRANT = {"accessToken": "access", "refreshToken": "refresh", "expiresAt": 2000}
SHIPPED_MANAGED = login.MANAGED


@pytest.fixture(autouse=True)
def no_managed_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(login, "MANAGED", tmp_path / "no-managed-settings.json")


@pytest.fixture
def ah(tmp_path, monkeypatch):
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    return tmp_path / "ah"


@pytest.fixture
def rendered(tmp_path):
    operator = tmp_path / "operator"
    operator.mkdir()
    (operator / ".credentials.json").write_text("operator login")
    (operator / "projects").mkdir()
    profile = tmp_path / "profiles" / "master"
    out = profile / "claude"
    out.mkdir(parents=True)
    (out / "settings.json").write_text('{"hooks": {"Stop": []}}')
    (out / "CLAUDE.md").write_text("persona")
    (out / ".claude.json").write_text(json.dumps({"mcpServers": {"a": {}}, login.ACCOUNT: OPERATOR_ACCOUNT}))
    (out / binding.FILE).write_text(json.dumps({"profile": "master", "home": str(out)}))
    (out / ".credentials.json").symlink_to(operator / ".credentials.json")
    (out / "projects").symlink_to(operator / "projects")
    (out / "skills").mkdir()
    (out / "skills" / "one.md").write_text("skill")
    (profile / "claude.sources.json").write_text("[]")
    (profile / "claude.mounts.json").write_text("{}")
    return out


def _login(target, account=MASTER_ACCOUNT, grant=GRANT):
    (target / login.CREDENTIALS).write_text(json.dumps({login.GRANT: grant}))
    config = json.loads((target / login.CONFIG).read_text())
    (target / login.CONFIG).write_text(json.dumps({**config, login.ACCOUNT: account}))


def test_the_login_is_on_only_for_a_claude_front_door_master_with_the_setting_on(monkeypatch):
    monkeypatch.setattr(login.hooks_config, "MASTER_CLAUDE_LOGIN", True)
    assert login.enabled("rig", "master@rig", "master", "claude") is True
    assert login.enabled("rig", "master@rig", "master", "codex") is False
    assert login.enabled("rig", "", "eng", "claude") is False
    assert login.enabled("rig", "master-2@rig", "master", "claude") is False
    monkeypatch.setattr(login.hooks_config, "MASTER_CLAUDE_LOGIN", "True")
    assert login.enabled("rig", "master@rig", "master", "claude") is False
    monkeypatch.setattr(login.hooks_config, "MASTER_CLAUDE_LOGIN", False)
    assert login.enabled("rig", "master@rig", "master", "claude") is False
    assert login.route(True) == ["--route", "master-login"]
    assert login.route(False) == []


def test_signed_in_names_the_master_login_account_only_with_the_setting_on(ah, monkeypatch):
    monkeypatch.setattr(login.hooks_config, "MASTER_CLAUDE_LOGIN", True)
    assert login.signed_in() == ""
    login.home().mkdir(parents=True)
    (login.home() / login.CONFIG).write_text(json.dumps({login.ACCOUNT: MASTER_ACCOUNT}))
    assert login.signed_in() == ""
    (login.home() / login.CREDENTIALS).write_text(json.dumps({login.GRANT: GRANT}))
    assert login.signed_in() == "masteratexample_com"
    monkeypatch.setattr(login.hooks_config, "MASTER_CLAUDE_LOGIN", "True")
    assert login.signed_in() == ""


def test_the_private_home_lives_under_the_agentihooks_home(ah):
    assert login.root() == ah / "master-login"
    assert login.home() == ah / "master-login" / "claude"


def test_build_copies_the_rendered_master_home_without_the_shared_credential_link(ah, rendered):
    login.root().mkdir(mode=0o700, parents=True)
    (login.root() / "staging").mkdir()
    (login.root() / "staging" / "left-over").write_text("old")
    target = login.build(rendered)
    assert not (target / "left-over").exists()
    assert target == login.home()
    assert not (target / login.CREDENTIALS).exists() and not (target / login.CREDENTIALS).is_symlink()
    assert (target / "settings.json").read_bytes() == (rendered / "settings.json").read_bytes()
    assert (target / "skills" / "one.md").read_text() == "skill"
    assert (target / "projects").is_symlink() and (target / "projects").readlink() == (rendered / "projects").readlink()
    assert json.loads((target / login.CONFIG).read_text()) == {"mcpServers": {"a": {}}}
    assert json.loads((target / binding.FILE).read_text()) == {"profile": "master", "home": str(target.resolve())}
    assert (login.root() / "claude.sources.json").read_text() == "[]"
    assert (login.root() / "claude.mounts.json").read_text() == "{}"
    assert target.stat().st_mode & 0o777 == login.root().stat().st_mode & 0o777 == 0o700
    assert (target / login.CONFIG).stat().st_mode & 0o777 == 0o600
    assert not (login.root() / "staging").exists()
    assert (rendered / login.CREDENTIALS).is_symlink()
    assert (rendered / login.CREDENTIALS).read_text() == "operator login"


def test_a_rebuild_keeps_the_master_login_and_takes_new_rendered_files(ah, rendered):
    _login(login.build(rendered))
    (rendered / "settings.json").write_text('{"hooks": {}}')
    target = login.build(rendered)
    assert json.loads((target / login.CREDENTIALS).read_text()) == {login.GRANT: GRANT}
    assert (target / login.CREDENTIALS).stat().st_mode & 0o777 == 0o600
    assert not (target / login.CREDENTIALS).is_symlink()
    assert json.loads((target / login.CONFIG).read_text())[login.ACCOUNT] == MASTER_ACCOUNT
    assert (target / "settings.json").read_text() == '{"hooks": {}}'
    assert (rendered.parent.parent.parent / "operator" / ".credentials.json").read_text() == "operator login"


def test_a_rebuild_never_keeps_a_symlinked_credential_file(ah, rendered, tmp_path):
    target = login.build(rendered)
    (target / login.CREDENTIALS).symlink_to(tmp_path / "operator" / ".credentials.json")
    assert not (login.build(rendered) / login.CREDENTIALS).exists()


def test_build_rehomes_the_launch_report_and_only_when_one_is_given(ah, rendered, monkeypatch):
    seen = []
    monkeypatch.setattr(binding, "rehome", lambda report, home: seen.append((report, home)))
    login.build(rendered)
    assert seen == []
    target = login.build(rendered, "/run/report")
    assert seen == [(Path("/run/report"), target)]


def test_build_without_manifests_or_binding_still_builds(ah, tmp_path):
    out = tmp_path / "bare" / "claude"
    out.mkdir(parents=True)
    target = login.build(out)
    assert json.loads((target / binding.FILE).read_text()) == {"home": str(target.resolve())}
    assert json.loads((target / login.CONFIG).read_text()) == {}
    assert sorted(path.name for path in login.root().iterdir()) == ["claude", login.LOCK]
    assert login.root().stat().st_mode & 0o777 == 0o700


def _settings(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


@pytest.mark.parametrize("where", ["managed", "home", "project", "local"])
@pytest.mark.parametrize(
    ("doc", "said"),
    [
        ({"apiKeyHelper": "/bin/key"}, "sets apiKeyHelper"),
        (
            {"env": {"ANTHROPIC_BASE_URL": "x", "CLAUDE_CODE_USE_BEDROCK": "1"}},
            "sets ANTHROPIC_BASE_URL, CLAUDE_CODE_USE_BEDROCK",
        ),
        ({"env": {"CLAUDE_SECURESTORAGE_CONFIG_DIR": "/elsewhere"}}, "sets CLAUDE_SECURESTORAGE_CONFIG_DIR"),
        ({"env": {"AH_CC_TOKEN_one": "x"}}, "sets AH_CC_TOKEN_one"),
        ({"forceLoginMethod": "console"}, "sets forceLoginMethod console"),
    ],
)
def test_settings_that_reintroduce_a_credential_or_another_login_refuse(tmp_path, where, doc, said):
    target, cwd, managed = tmp_path / "home", tmp_path / "repo", tmp_path / "managed.json"
    path = {
        "managed": managed,
        "home": target / "settings.json",
        "project": cwd / ".claude" / "settings.json",
        "local": cwd / ".claude" / "settings.local.json",
    }[where]
    _settings(path, doc)
    assert login.refusal(target, cwd, managed) == f"{path} {said}"


def test_ordinary_settings_and_missing_files_pass(tmp_path):
    target = tmp_path / "home"
    _settings(
        target / "settings.json", {"env": {"ANTHROPIC_MODEL": "opus", "HOME_X": "1"}, "forceLoginMethod": "claudeai"}
    )
    assert login.refusal(target, tmp_path / "repo", tmp_path / "managed.json") == ""
    assert SHIPPED_MANAGED == Path("/etc/claude-code/managed-settings.json")


def test_the_master_account_needs_its_own_login(tmp_path):
    target = tmp_path / "home"
    target.mkdir()
    (target / login.CONFIG).write_text(json.dumps({login.ACCOUNT: MASTER_ACCOUNT}))
    with pytest.raises(login.LoginRefused, match="^no master claude ai login: run agentihooks claude login --master$"):
        login.account(target, {"AH_CC_TOKEN_w": "x"})
    (target / login.CONFIG).write_text("{}")
    (target / login.CREDENTIALS).write_text("{}")
    with pytest.raises(login.LoginRefused, match="^no master claude ai login"):
        login.account(target, {"AH_CC_TOKEN_w": "x"})


def _home(tmp_path):
    target = tmp_path / "home"
    target.mkdir()
    (target / login.CONFIG).write_text(json.dumps({login.ACCOUNT: MASTER_ACCOUNT}))
    (target / login.CREDENTIALS).write_text("{}")
    return target


def _store(tmp_path):
    return FileSettings(tmp_path / "routing-settings.json")


def _never(environ):
    pytest.fail("settings opened for a login without a worker token")


def _given(store, environ):
    def store_of(seen):
        assert seen is environ
        return store

    return store_of


def test_workers_need_tokens_and_a_login_off_every_worker_token_never_declares(tmp_path):
    target = _home(tmp_path)
    with pytest.raises(
        login.LoginRefused, match="^the master claude ai login needs workers on quota tokens; none is set$"
    ):
        login.account(target, {"ANTHROPIC_BASE_URL": "https://gateway"}, _never)
    environ = {"AH_CC_TOKEN_workeratexample_com": "x", "AGENTIHOOKS_AGENT_NAME": "master@a-1"}
    assert login.account(target, environ, _never) == "masteratexample_com"


def test_a_login_on_a_worker_token_account_declares_it_master_when_none_is_declared(tmp_path):
    target, store = _home(tmp_path), _store(tmp_path)
    environ = {"AH_CC_TOKEN_masteratexample_com": "x", "AGENTIHOOKS_AGENT_NAME": "master@a-1"}
    assert login.account(target, environ, _given(store, environ)) == "masteratexample_com"
    assert master_account.declared(store, environ)["claude"] == MasterAccount("claude", "masteratexample_com")
    written = [entry for entry in store.history() if entry["key"] == "master-account-claude"]
    assert [(entry["value"], entry["actor"]) for entry in written] == [("masteratexample_com", "master@a-1")]
    assert isinstance(written[0]["at"], float) and written[0]["at"] > 0


def test_a_login_on_the_declared_master_account_keeps_its_declaration(tmp_path):
    target, store = _home(tmp_path), _store(tmp_path)
    environ = {"AH_CC_TOKEN_masteratexample_com": "x", "AGENTIHOOKS_AGENT_NAME": "master@a-1"}
    master_account.declare(store, {"claude": ("masteratexample_com", "max")}, environ, "operator", 1.0)
    written = len(store.history())
    assert login.account(target, environ, _given(store, environ)) == "masteratexample_com"
    assert len(store.history()) == written
    assert master_account.declared(store, environ)["claude"].tier == "max"


def test_a_login_on_another_account_takes_over_its_slot_declaration(tmp_path):
    target, store = _home(tmp_path), _store(tmp_path)
    environ = {"AH_CC_TOKEN_masteratexample_com": "x", "AH_CC_TOKEN_other": "y", "AGENTIHOOKS_AGENT_NAME": "m"}
    master_account.declare(store, {"claude": ("other", None)}, environ, "operator", 1.0)
    assert login.account(target, environ, _given(store, environ)) == "masteratexample_com"
    assert master_account.declared(store, environ)["claude"].slug == "masteratexample_com"


def test_a_declaration_another_login_wrote_first_is_taken_over(tmp_path):
    class Raced(FileSettings):
        def _claim(self, key, value, entry):
            self._write(key, "other", {**entry, "value": "other", "actor": "rival"})
            return super()._claim(key, value, entry)

    target, store = _home(tmp_path), Raced(tmp_path / "routing-settings.json")
    environ = {"AH_CC_TOKEN_masteratexample_com": "x", "AH_CC_TOKEN_other": "y", "AGENTIHOOKS_AGENT_NAME": "m"}
    assert login.account(target, environ, _given(store, environ)) == "masteratexample_com"
    assert master_account.declared(store, environ)["claude"].slug == "masteratexample_com"


def test_a_second_slot_login_on_a_worker_account_keeps_the_first_slot_declaration(tmp_path, monkeypatch):
    monkeypatch.setenv(login.SLOT_ENV, "2")
    target, store = _home(tmp_path), _store(tmp_path)
    environ = {"AH_CC_TOKEN_masteratexample_com": "x", "AH_CC_TOKEN_other": "y", "AGENTIHOOKS_AGENT_NAME": "m"}
    master_account.declare(store, {"claude": ("other", None)}, environ, "operator", 1.0)
    assert login.account(target, environ, _given(store, environ)) == "masteratexample_com"
    assert master_account.declared(store, environ)["claude"].slug == "other"
    assert store.all()["master-account-claude-2"] == "masteratexample_com"


def test_the_settings_store_follows_the_routing_store(tmp_path, monkeypatch):
    from scripts.routing import place

    environ, client, opened = {"AGENTIHOOKS_HOME": str(tmp_path)}, object(), []
    monkeypatch.setattr(place, "_client", lambda seen: client if seen is environ else None)
    monkeypatch.setattr(login, "open_store", lambda *args: opened.append(args) or "store")
    assert login.routing_store(environ) == "store"
    assert opened == [(client, environ)]


def test_the_login_claims_the_claude_master_with_its_environment_and_name(tmp_path, monkeypatch):
    claimed = []
    monkeypatch.setattr(master_account, "claim", lambda *args: claimed.append(args) or "masteratexample_com")
    environ = {"AH_CC_TOKEN_masteratexample_com": "x", "AGENTIHOOKS_AGENT_NAME": "master@a-1"}
    store = _store(tmp_path)
    login.account(_home(tmp_path), environ, _given(store, environ))
    ((seen_store, harness, slug, seen_environ, actor, at),) = claimed
    assert (seen_store, harness, slug, seen_environ, actor) == (
        store,
        "claude",
        "masteratexample_com",
        environ,
        "master@a-1",
    )
    assert isinstance(at, float) and at > 0


def test_the_child_drops_routed_and_api_credentials_and_pins_both_selectors(tmp_path):
    inherited = {
        "HOME": "/home/op",
        "ANTHROPIC_MODEL": "opus",
        "AGENTIHOOKS_PROFILE_REPORT": "/run/report",
        "CLAUDE_CODE_OAUTH_TOKEN": "x",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "x",
        "ANTHROPIC_API_KEY": "x",
        "ANTHROPIC_AUTH_TOKEN": "x",
        "ANTHROPIC_BASE_URL": "x",
        "ANTHROPIC_BEDROCK_BASE_URL": "x",
        "ANTHROPIC_CUSTOM_HEADERS": "x",
        "ANTHROPIC_VERTEX_PROJECT_ID": "x",
        "ANTHROPIC_FOUNDRY_API_KEY": "x",
        "AWS_BEARER_TOKEN_BEDROCK": "x",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AH_CC_TOKEN_worker": "x",
        "AH_ROUTE_API": "1",
        "AH_ROUTE_INTERACTIVE_old": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_GROWTHBOOK": "1",
        "CLAUDE_CONFIG_DIR": "/home/op/.claude",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": "",
    }
    child = login.child(inherited, tmp_path / "home", "masteratexample_com")
    home = str(tmp_path / "home")
    assert child == {
        "HOME": "/home/op",
        "ANTHROPIC_MODEL": "opus",
        "AGENTIHOOKS_PROFILE_REPORT": "/run/report",
        "AH_ROUTE_INTERACTIVE_masteratexample_com": "1",
        "CLAUDE_CONFIG_DIR": home,
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": home,
    }
    assert login.child({"HOME": "/h"}, tmp_path) == {
        "HOME": "/h",
        "CLAUDE_CONFIG_DIR": str(tmp_path),
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(tmp_path),
    }


def test_the_first_owner_takes_epoch_one_and_each_claim_takes_the_next(ah):
    assert login.claim("master@a-1", 10, lambda pid: False) == 1
    assert login.claim("master@a-1", 11, lambda pid: False) == 2
    owner = login.root() / login.OWNER
    assert json.loads(owner.read_text()) == {"holders": {"master@a-1": 11}, "epoch": 2}
    assert owner.stat().st_mode & 0o777 == 0o600
    assert login.root().stat().st_mode & 0o777 == 0o700


def test_a_live_holder_fences_only_another_process_under_its_own_name(ah):
    login.claim("master@a-1", 10, lambda pid: True)
    with pytest.raises(login.LoginRefused, match="^the master login is held by master@a-1, still running$"):
        login.claim("master@a-1", 20, lambda pid: pid == 10)
    assert login.claim("master@a-2", 20, lambda pid: True) == 2
    assert login.claim("master@a-1", 10, lambda pid: True) == 3
    assert login.claim("master@a-3", 30, lambda pid: pid == 20) == 4
    holders = json.loads((login.root() / login.OWNER).read_text())["holders"]
    assert holders == {"master@a-2": 20, "master@a-3": 30}


def test_a_claim_starts_above_the_epoch_another_host_stored(ah):
    assert login.claim("master@a-1", 10, lambda pid: False, floor=7) == 8
    assert login.claim("master@a-1", 10, lambda pid: False, floor=3) == 9


def test_a_process_is_alive_only_by_its_integer_pid():
    assert login._alive(os.getpid()) is True
    assert login._alive(str(os.getpid())) is False
    assert login._alive(None) is False
    assert login._alive(2**22 + 7) is False


class _Stdout:
    def __init__(self):
        self.text, self.flushed = "", ""

    def write(self, text):
        self.text += text

    def flush(self):
        self.flushed = self.text


class FakeBao:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})
        self.writes = []

    def install(self, monkeypatch):
        monkeypatch.setattr(
            openbao_kv,
            "read",
            lambda bao, path: (self.stored if (bao, path) == ("bao", "agentihooks/master-claude-login") else {}, 3),
        )
        monkeypatch.setattr(
            openbao_kv,
            "merge",
            lambda bao, path, entries: self.writes.append((path, dict(entries))) if bao == "bao" else None,
        )
        return self


def _home(tmp_path, grant=GRANT):
    target = tmp_path / "home"
    target.mkdir()
    (target / login.CONFIG).write_text("{}")
    _login(target, grant=grant)
    return target


def test_publish_stores_only_the_native_grant_and_account_with_its_epoch(tmp_path, monkeypatch):
    bao = FakeBao().install(monkeypatch)
    assert login.publish("bao", _home(tmp_path), 4) is True
    assert bao.writes == [
        (
            "agentihooks/master-claude-login",
            {"grant": json.dumps(GRANT), "account": json.dumps(MASTER_ACCOUNT), "epoch": "4", "generation": "2000"},
        )
    ]


@pytest.mark.parametrize(("epoch", "generation"), [("5", "0"), ("4", "2000"), ("4", "3000")])
def test_publish_never_replaces_a_newer_or_equal_copy(tmp_path, monkeypatch, epoch, generation):
    bao = FakeBao({"epoch": epoch, "generation": generation}).install(monkeypatch)
    assert login.publish("bao", _home(tmp_path), 4) is False
    assert bao.writes == []


def test_publish_replaces_an_older_generation_of_the_same_epoch(tmp_path, monkeypatch):
    bao = FakeBao({"epoch": "4", "generation": "1999"}).install(monkeypatch)
    assert login.publish("bao", _home(tmp_path), 4) is True
    assert len(bao.writes) == 1


def test_publish_reads_missing_generations_as_zero(tmp_path, monkeypatch):
    bao = FakeBao({"epoch": "4"}).install(monkeypatch)
    assert login.publish("bao", _home(tmp_path, {"refreshToken": "r", "expiresAt": 1}), 4) is True
    other = tmp_path / "other"
    other.mkdir()
    bao.stored = {}
    assert login.publish("bao", _home(other, {"refreshToken": "r"}), 1) is True
    assert [entries["generation"] for _, entries in bao.writes] == ["1", "0"]


def test_publish_reads_a_missing_stored_epoch_as_zero(tmp_path, monkeypatch):
    bao = FakeBao({"generation": "3000"}).install(monkeypatch)
    assert login.publish("bao", _home(tmp_path), 1) is True
    assert len(bao.writes) == 1


def test_publish_skips_a_home_without_a_refresh_token(tmp_path, monkeypatch):
    bao = FakeBao().install(monkeypatch)
    assert login.publish("bao", _home(tmp_path, {"accessToken": "a"}), 1) is False
    assert bao.writes == []


def test_restore_writes_the_stored_grant_into_a_home_without_one(tmp_path):
    target = tmp_path / "home"
    target.mkdir()
    (target / login.CONFIG).write_text('{"mcpServers": {}}')
    copy = {"grant": json.dumps(GRANT), "account": json.dumps(MASTER_ACCOUNT)}
    assert login.restore(copy, target) is True
    assert json.loads((target / login.CREDENTIALS).read_text()) == {login.GRANT: GRANT}
    assert (target / login.CREDENTIALS).stat().st_mode & 0o777 == 0o600
    assert json.loads((target / login.CONFIG).read_text()) == {"mcpServers": {}, login.ACCOUNT: MASTER_ACCOUNT}


def test_restore_keeps_a_live_login_and_skips_an_empty_copy(tmp_path):
    target = _home(tmp_path)
    assert login.restore({"grant": json.dumps({"accessToken": "old"}), "account": "{}"}, target) is False
    assert json.loads((target / login.CREDENTIALS).read_text()) == {login.GRANT: GRANT}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert login.restore({}, empty) is False
    assert not (empty / login.CREDENTIALS).exists()


def test_stored_reads_the_swarm_copy_and_reports_an_outage(monkeypatch, capsys):
    FakeBao({"epoch": "3"}).install(monkeypatch)
    assert login.stored("bao") == {"epoch": "3"}
    assert login.stored(None) == {}

    def down(bao, path):
        raise openbao_kv.OpenBaoError(0, "refused")

    monkeypatch.setattr(openbao_kv, "read", down)
    assert login.stored("bao") == {}
    assert capsys.readouterr().err == "master login recovery copy not read: OpenBao request failed: refused\n"


def test_keep_publishes_and_reports_a_failed_write(tmp_path, monkeypatch, capsys):
    bao = FakeBao().install(monkeypatch)
    target = _home(tmp_path)
    assert login.keep(None, target, 1) is False
    assert login.keep("bao", target, 1) is True
    assert [path for path, _ in bao.writes] == ["agentihooks/master-claude-login"]

    def sealed(*_):
        raise openbao_kv.OpenBaoError(503, "sealed")

    monkeypatch.setattr(openbao_kv, "merge", sealed)
    assert login.keep("bao", target, 2) is False
    assert capsys.readouterr().err == "master login recovery copy not stored: OpenBao HTTP 503: sealed\n"


def test_a_checkpoint_publishes_only_from_the_master_login_home(ah, rendered, monkeypatch):
    kept = []
    monkeypatch.setattr(login, "keep", lambda bao, target, epoch, number: kept.append((bao, target, epoch)) or True)
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: "bao" if environ.get("OPENBAO_URL") else None)
    _seed(rendered)
    master = {"OPENBAO_URL": "u", "CLAUDE_CONFIG_DIR": str(login.home())}
    assert login.checkpoint(master) is True
    login.claim("master@a-1", 10, lambda pid: False)
    assert login.checkpoint({"OPENBAO_URL": "u", "CLAUDE_CONFIG_DIR": str(rendered)}) is False
    assert login.checkpoint({"OPENBAO_URL": "u"}) is False
    assert login.checkpoint(master) is True
    assert kept == [("bao", login.home(), 0), ("bao", login.home(), 1)]


def _environ(rendered, **extra):
    return {
        "AGENTIHOOKS_SWARM": "rig",
        "AGENTIHOOKS_AGENT_NAME": "master@a-1",
        "CLAUDE_CONFIG_DIR": str(rendered),
        "AH_CC_TOKEN_workeratexample_com": "x",
        **extra,
    }


def _seed(rendered):
    _login(login.build(rendered))


def test_prepare_claims_above_the_stored_epoch_builds_restores_and_keeps(ah, rendered, tmp_path, monkeypatch):
    _seed(rendered)
    calls = []
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: "bao" if environ.get("OPENBAO_URL") else None)
    monkeypatch.setattr(login, "stored", lambda bao, number=None: {"epoch": "5"} if bao == "bao" else {})
    monkeypatch.setattr(login, "restore", lambda copy, target: calls.append(("restore", copy, target)))
    monkeypatch.setattr(login, "keep", lambda bao, target, epoch, number: calls.append(("keep", bao, target, epoch)))
    monkeypatch.setattr(binding, "rehome", lambda report, home, shared: calls.append(("rehome", report, home, shared)))
    environ = _environ(rendered, OPENBAO_URL="u", AGENTIHOOKS_PROFILE_REPORT="/run/report")
    target, found = login.prepare(environ, tmp_path)
    assert (target, found) == (login.home(), "masteratexample_com")
    assert calls == [
        ("rehome", Path("/run/report"), target, False),
        ("restore", {"epoch": "5"}, target),
        ("keep", "bao", target, 6),
    ]
    owner = json.loads((login.root() / login.OWNER).read_text())
    assert owner["holders"] == {"master@a-1": os.getpid()}
    calls.clear()
    login.prepare(_environ(rendered), tmp_path)
    assert calls == [("restore", {}, target), ("keep", None, target, 7)]


def test_prepare_without_a_stored_epoch_takes_epoch_one(ah, rendered, tmp_path, monkeypatch):
    _seed(rendered)
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: "bao")
    monkeypatch.setattr(login, "stored", lambda bao, number=None: {})
    monkeypatch.setattr(login, "keep", lambda *_: True)
    login.prepare(_environ(rendered), tmp_path)
    assert json.loads((login.root() / login.OWNER).read_text())["epoch"] == 1


def test_prepare_refuses_incompatible_settings_before_touching_the_lease_or_home(ah, rendered, tmp_path, monkeypatch):
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: pytest.fail("store read before refusal"))
    _settings(tmp_path / ".claude" / "settings.json", {"apiKeyHelper": "/bin/key"})
    with pytest.raises(login.LoginRefused, match="sets apiKeyHelper$"):
        login.prepare(_environ(rendered), tmp_path)
    assert not login.root().exists()
    _settings(rendered / "settings.json", {"env": {"ANTHROPIC_API_KEY": "x"}})
    with pytest.raises(login.LoginRefused, match="sets ANTHROPIC_API_KEY$"):
        login.prepare(_environ(rendered), tmp_path / "elsewhere")


def test_refusal_reads_the_managed_settings_by_default(tmp_path, monkeypatch):
    managed = tmp_path / "managed.json"
    _settings(managed, {"apiKeyHelper": "/bin/key"})
    monkeypatch.setattr(login, "MANAGED", managed)
    assert login.refusal(tmp_path / "home", tmp_path) == f"{managed} sets apiKeyHelper"


def test_launch_execs_native_claude_in_the_private_home_with_the_session_setting(
    ah, rendered, tmp_path, monkeypatch, capsys
):
    from scripts import install
    from scripts.routing import native_login

    monkeypatch.setattr(native_login, "check", lambda *_: dict(native_login.PROOF))
    _seed(rendered)
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    monkeypatch.chdir(tmp_path)
    executed, out = [], _Stdout()
    monkeypatch.setattr(
        login.os, "execvpe", lambda binary, argv, env: executed.append((binary, argv, env, out.flushed))
    )
    monkeypatch.setattr(login.sys, "stdout", out)
    report = tmp_path / "route"
    login.launch_claude(
        "/opt/claude", ["--model", "opus"], _environ(rendered, CLAUDE_CODE_OAUTH_TOKEN="x"), str(report)
    )
    binary, argv, env, flushed = executed[0]
    assert flushed == "[agenti] account=masteratexample_com route=master-login\n"
    assert binary == "/opt/claude"
    assert argv == install._claude_command(
        "/opt/claude", ["--settings", '{"forceLoginMethod":"claudeai"}', "--model", "opus"]
    )
    assert env["CLAUDE_CONFIG_DIR"] == env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(login.home())
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "AH_CC_TOKEN_workeratexample_com" not in env
    assert env["AH_ROUTE_INTERACTIVE_masteratexample_com"] == "1"
    assert report.read_text().splitlines() == ["status=routed", "account=masteratexample_com", "placement=forced"]


def test_launch_turns_a_refusal_into_a_routing_error(ah, rendered, tmp_path, monkeypatch):
    from scripts.claude_quota_balancer import RoutingError

    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RoutingError, match="^no master claude ai login: run"):
        login.launch_claude("/opt/claude", [], _environ(rendered), "")


def test_the_claude_launcher_hands_the_master_login_route_to_this_module(tmp_path, monkeypatch):
    from scripts import install

    seen = []
    monkeypatch.setattr(install, "_load_claude_runtime_env", lambda: None)
    monkeypatch.setattr("scripts.deps_preflight.ensure", lambda: None)
    monkeypatch.setattr("scripts.claude_quota_balancer._cache_path", lambda _: tmp_path / "quota.json")
    monkeypatch.setattr(install.shutil, "which", lambda name: f"/opt/{name}")
    monkeypatch.setattr(login, "launch_claude", lambda *args: seen.append(args))
    install.cmd_claude(["--route", "master-login", "--agentihooks-report", str(tmp_path / "r"), "--version"])
    assert seen == [("/opt/claude", ["--version"], install.os.environ, str(tmp_path / "r"))]


def test_the_stop_hook_checkpoints_the_master_login_and_logs_a_failure(monkeypatch):
    from hooks import hook_manager

    seen, logged = [], []
    monkeypatch.setattr(login, "checkpoint", lambda environ: seen.append(environ))
    monkeypatch.setattr(hook_manager, "log", lambda message, data: logged.append((message, data)))
    hook_manager._master_login_checkpoint()
    assert seen == [os.environ] and logged == []

    def broken(environ):
        raise OSError("disk full")

    monkeypatch.setattr(login, "checkpoint", broken)
    hook_manager._master_login_checkpoint()
    assert logged == [("master login checkpoint failed", {"error": "disk full"})]


def test_a_worker_token_master_launch_holds_its_workers_at_the_one_master_ceiling(ah, rendered, tmp_path, monkeypatch):
    from scripts.claude_quota_balancer import ClaudeTokenSource, ProbeResult, QuotaWindow

    _seed(rendered)
    store = _store(tmp_path)
    monkeypatch.setattr(login, "routing_store", lambda environ: store)
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    monkeypatch.chdir(tmp_path)
    executed = []
    monkeypatch.setattr(login.os, "execvpe", lambda binary, argv, env: executed.append(env))
    environ = _environ(rendered, AH_CC_TOKEN_masteratexample_com="x")
    report = tmp_path / "route"
    login.launch_claude("/opt/claude", [], environ, str(report))
    assert report.read_text().splitlines() == ["status=routed", "account=masteratexample_com", "placement=forced"]
    env = executed[0]
    assert not any(name.startswith("AH_CC_TOKEN_") for name in env)
    proc = tmp_path / "proc" / "10"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"claude\0")
    (proc / "comm").write_text("claude\n")
    (proc / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
    monkeypatch.setattr(master_account, "load", lambda seen: master_account.declared(store, seen))
    agent = SimpleNamespace(
        lane="master", state="working", harness="claude", account="masteratexample_com", route_kind="interactive"
    )
    swarms = SimpleNamespace(slugs=lambda: ["rig"], agents=lambda slug: [agent])
    held = reservation.current("claude", environ, store_of=lambda seen: swarms, proc=tmp_path / "proc")
    assert held == reservation.Reservation("claude", "masteratexample_com", 1)
    source = ClaudeTokenSource(held=held)

    def week(used, account="masteratexample_com"):
        return ProbeResult(account, "allowed", "NORMAL", 90, QuotaWindow(10), QuotaWindow(used))

    assert source.cap(week(70.0), 0) == 0
    assert source.cap(week(69.9), 0) > 0
    assert source.cap(week(70.0, "workeratexample_com"), 0) > 0


def test_slot_one_keeps_the_original_home_and_record(ah, monkeypatch):
    monkeypatch.delenv(login.SLOT_ENV, raising=False)
    assert login.slot() == 1
    assert login.root() == ah / "master-login"
    assert login.creds() == login.CREDS


def test_the_active_slot_file_picks_the_home_and_record(ah, monkeypatch):
    monkeypatch.delenv(login.SLOT_ENV, raising=False)
    login.use(4)
    assert login.root() == ah / "master-login-4"
    assert login.creds() == f"{login.CREDS}-4"


def test_an_unreadable_or_out_of_range_slot_falls_back_to_slot_one(ah, monkeypatch):
    monkeypatch.setenv(login.SLOT_ENV, "nine")
    assert login.slot() == 1
    monkeypatch.setenv(login.SLOT_ENV, str(login.MAX_SLOTS + 1))
    assert login.slot() == 1


def test_use_refuses_a_slot_outside_the_cap(ah):
    with pytest.raises(login.LoginRefused):
        login.use(0)


@pytest.mark.parametrize("number", [0, login.MAX_SLOTS + 1])
def test_use_names_the_refused_slot_and_the_cap(ah, number):
    with pytest.raises(login.LoginRefused) as refused:
        login.use(number)
    assert str(refused.value) == f"master login slot {number} is outside 1 to {login.MAX_SLOTS}"
    assert not (ah / login.SLOT_FILE).exists()


@pytest.mark.parametrize("number", [1, login.MAX_SLOTS])
def test_use_and_slot_accept_the_first_and_last_slot(ah, monkeypatch, number):
    monkeypatch.delenv(login.SLOT_ENV, raising=False)
    login.use(number)
    assert (ah / login.SLOT_FILE).read_text() == f"{number}\n"
    assert login.slot() == number
    monkeypatch.setenv(login.SLOT_ENV, str(number))
    assert login.slot() == number


def test_slot_zero_falls_back_to_slot_one(ah, monkeypatch):
    monkeypatch.setenv(login.SLOT_ENV, "0")
    assert login.slot() == 1


def test_use_creates_a_nested_missing_home(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b" / "ah"
    monkeypatch.setattr(login.hooks_config, "AGENTIHOOKS_HOME", str(nested))
    monkeypatch.delenv(login.SLOT_ENV, raising=False)
    login.use(2)
    assert (nested / login.SLOT_FILE).read_text() == "2\n"


def test_a_slot_takeover_records_the_login_time(tmp_path, monkeypatch):
    monkeypatch.setenv(login.SLOT_ENV, "2")
    monkeypatch.setattr(login, "time", SimpleNamespace(time=lambda: 1234.5))
    target, store = _home(tmp_path), _store(tmp_path)
    environ = {"AH_CC_TOKEN_masteratexample_com": "x", "AH_CC_TOKEN_other": "y", "AGENTIHOOKS_AGENT_NAME": "m"}
    store.set("master-account-claude-2", "other", "operator", 1.0)
    assert login.account(target, environ, _given(store, environ)) == "masteratexample_com"
    written = [entry for entry in store.history() if entry["key"] == "master-account-claude-2"]
    assert (written[-1]["value"], written[-1]["actor"], written[-1]["at"]) == ("masteratexample_com", "m", 1234.5)


@pytest.mark.parametrize(
    ("logins", "expected"), [({1: "operator"}, ""), ({1: ""}, login.NO_LOGIN), ({}, login.NO_LOGIN)]
)
def test_unusable_names_why_a_hive_cannot_run_the_master_login(monkeypatch, logins, expected):
    monkeypatch.setattr(login.hooks_config, "MASTER_CLAUDE_LOGIN", True)
    monkeypatch.setattr(login, "logins", lambda: logins)
    assert login.unusable() == expected
    monkeypatch.setattr(login.hooks_config, "MASTER_CLAUDE_LOGIN", "True")
    assert login.unusable() == login.OFF


@pytest.mark.parametrize(
    "valid,outcome", [(True, "failure"), (False, "success"), (False, "captcha"), (False, "failure")]
)
def test_launch_preparation_checks_live_login_then_waits_for_browser(
    ah, rendered, tmp_path, monkeypatch, valid, outcome
):
    from scripts import master_login_chain as chain
    from scripts.routing import native_login
    from scripts.swarm import operator_email

    _seed(rendered)
    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    calls = []

    def check(environ, account):
        calls.append("current")
        assert account == "masteratexample_com"
        assert environ["CLAUDE_CONFIG_DIR"] == str(login.home())
        if not valid and calls.count("current") == 1:
            raise native_login.LoginError(native_login.EXPIRED)
        return dict(native_login.PROOF)

    def browser(environ, target):
        calls.append("browser")
        assert environ == _environ(rendered)
        assert target == login.home()
        return outcome

    monkeypatch.setattr(native_login, "check", check)
    monkeypatch.setattr(chain, "authorize", browser, raising=False)
    monkeypatch.setattr(operator_email, "send", lambda *_: pytest.fail("login must not send email"))
    prepared = login.prepare_launch(_environ(rendered), tmp_path)
    if valid or outcome == "success":
        assert prepared == (login.home(), "masteratexample_com")
        assert calls == (["current"] if valid else ["current", "browser", "current"])
    else:
        assert prepared is None
        assert calls == ["current", "browser"]
        assert login.holders(login.root()) == {}


def test_browser_preparation_skips_a_slot_held_by_another_process(ah, rendered, tmp_path, monkeypatch):
    from scripts import master_login_chain as chain
    from scripts.routing import native_login

    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    login.root(1).mkdir(parents=True)
    login._write(login.root(1) / login.OWNER, {"epoch": 4, "holders": {"other": os.getppid()}})
    observed = []

    def browser(environ, target):
        observed.append(target)
        _login(target)
        return "success"

    monkeypatch.setattr(chain, "authorize", browser, raising=False)
    monkeypatch.setattr(native_login, "check", lambda *_: dict(native_login.PROOF))
    assert login.prepare_launch(_environ(rendered), tmp_path) == (login.home(2), "masteratexample_com")
    assert observed == [login.home(2)]
    assert login.holders(login.root(1)) == {"other": os.getppid()}
    assert login.holders(login.root(2)) == {"master@a-1": os.getpid()}


def test_prepare_launch_keeps_incompatible_settings_out_of_every_fallback(ah, rendered, tmp_path, monkeypatch):
    from scripts import master_login_chain as chain

    (rendered / "settings.json").write_text('{"apiKeyHelper":"unsafe"}')
    monkeypatch.setattr(
        chain, "authorize", lambda *_: pytest.fail("settings refusal must precede browser"), raising=False
    )
    with pytest.raises(login.LoginRefused, match="sets apiKeyHelper$"):
        login.prepare_launch(_environ(rendered), tmp_path)
    assert not login.root().exists()


def test_browser_home_refuses_when_all_slots_are_occupied(ah, rendered):
    for number in login.numbers():
        login.root(number).mkdir(parents=True)
        login._write(login.root(number) / login.OWNER, {"holders": {"other": os.getppid()}})
    with pytest.raises(login.LoginRefused, match="^all 5 master login slots hold a login"):
        login.browser_home(_environ(rendered))
    assert all(login.holders(login.root(number)) == {"other": os.getppid()} for number in login.numbers())


def test_release_keeps_other_holders_and_the_owner_epoch(ah, rendered):
    login.root().mkdir(parents=True)
    login._write(
        login.root() / login.OWNER,
        {"epoch": 7, "alone": "master@a-1", "holders": {"master@a-1": os.getpid(), "other": os.getppid()}},
    )
    login.release(login.home(), _environ(rendered))
    assert json.loads((login.root() / login.OWNER).read_text()) == {"epoch": 7, "holders": {"other": os.getppid()}}


def test_release_never_drops_a_successor_with_the_same_name(ah, rendered):
    login.root().mkdir(parents=True)
    login._write(
        login.root() / login.OWNER, {"epoch": 8, "alone": "master@a-1", "holders": {"master@a-1": os.getppid()}}
    )
    login.release(login.home(), _environ(rendered))
    assert json.loads((login.root() / login.OWNER).read_text()) == {
        "epoch": 8,
        "alone": "master@a-1",
        "holders": {"master@a-1": os.getppid()},
    }


def test_prepare_launch_falls_back_without_touching_fully_held_logins(ah, rendered, tmp_path, monkeypatch):
    from scripts import master_login_chain as chain
    from scripts import master_logins

    monkeypatch.setattr(master_logins, "left", lambda *_: 100.0)

    monkeypatch.setattr(openbao_kv, "from_env", lambda environ: None)
    for number in login.numbers():
        target = login.build(rendered) if number == login.slot() else login._rebuild(login.root(number), rendered)
        _login(target)
        login._write(
            login.root(number) / login.OWNER, {"epoch": 3, "alone": "other", "holders": {"other": os.getppid()}}
        )
    monkeypatch.setattr(chain, "authorize", lambda *_: pytest.fail("there is no free browser slot"), raising=False)
    assert login.prepare_launch(_environ(rendered), tmp_path) is None
    assert all(login.holders(login.root(number)) == {"other": os.getppid()} for number in login.numbers())


def test_browser_home_preserves_an_existing_unheld_grant(ah, rendered):
    _seed(rendered)
    assert login.browser_home(_environ(rendered)) == login.home(2)
    assert login.login_account(1) == "masteratexample_com"
    held = json.loads((login.root(2) / login.OWNER).read_text())
    assert held == {"epoch": 1, "alone": "master@a-1", "holders": {"master@a-1": os.getpid()}}


@pytest.mark.parametrize("outcome", ["captcha", "failure"])
def test_master_launch_uses_subscription_fallback_without_remote_control(ah, rendered, tmp_path, monkeypatch, outcome):
    from hooks.context import account_sessions
    from scripts import claude_quota_balancer as router
    from scripts import master_login_chain as chain
    from scripts.hive import account_slots
    from scripts.routing import native_login

    _seed(rendered)
    (rendered / "settings.json").write_text('{"forceLoginMethod":"claudeai","hooks":{"Stop":[]}}')
    monkeypatch.setattr(openbao_kv, "from_env", lambda _: None)
    monkeypatch.chdir(tmp_path)
    selections, executed = [], []

    def invalid(*_):
        raise native_login.LoginError(native_login.EXPIRED)

    def select(environ, **options):
        selections.append(options)
        return router.RouteDecision(router.Credential("AH_CC_TOKEN_workeratexample_com", "routed"), None, "cache")

    monkeypatch.setattr(native_login, "check", invalid)
    monkeypatch.setattr(chain, "authorize", lambda *_: outcome)
    monkeypatch.setattr(router, "select_credential", select)
    monkeypatch.setattr(account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(account_slots, "execute", lambda harness, account, environ, launch: launch(environ))
    monkeypatch.setattr(login.os, "execvpe", lambda binary, argv, environ: executed.append((binary, argv, environ)))
    report = tmp_path / "route"
    args = ["--remote-control", "master proof", "--remote-control-session-name-prefix", "proof", "--model", "opus"]
    login.launch_claude("/opt/claude", args, _environ(rendered, ANTHROPIC_API_KEY="api"), str(report))
    assert selections[0]["exclude"] == (router.API_ACCOUNT,)
    assert selections[0]["sessions"] == {}
    binary, argv, environ = executed[0]
    assert binary == "/opt/claude"
    assert "--remote-control" not in argv and "--remote-control-session-name-prefix" not in argv
    assert argv[-2:] == ["--model", "opus"]
    assert environ["CLAUDE_CODE_OAUTH_TOKEN"] == "routed"
    assert "ANTHROPIC_API_KEY" not in environ
    target = Path(environ["CLAUDE_CONFIG_DIR"])
    assert target != rendered and target != login.home()
    assert environ["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(target)
    assert json.loads((target / "settings.json").read_text()) == {"hooks": {"Stop": []}}
    assert not (target / login.CREDENTIALS).exists()
    assert login.holders(login.root()) == {}
    assert dict(line.split("=", 1) for line in report.read_text().splitlines()) == {
        "status": "routed",
        "account": "workeratexample_com",
        "placement": "fallback",
        "remote_control": "off",
    }


def test_native_master_launch_checks_the_session_before_exec(ah, rendered, tmp_path, monkeypatch):
    from scripts.hive import account_slots
    from scripts.routing import native_login

    _seed(rendered)
    checked = []
    monkeypatch.setattr(openbao_kv, "from_env", lambda _: None)
    monkeypatch.setattr(native_login, "check", lambda environ, account: checked.append(account))
    monkeypatch.setattr(account_slots, "execute", lambda harness, account, environ, launch: launch(environ))
    monkeypatch.setattr(login.os, "execvpe", lambda *_: None)
    monkeypatch.chdir(tmp_path)
    login.launch_claude("/opt/claude", ["--model", "opus"], _environ(rendered), "")
    assert checked == ["masteratexample_com"]
