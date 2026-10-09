import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_v2 import worker_home

WORKSTATION_PYTHON = "/home/operator/dev/tcc-ecosystem/.venv/bin/python"
ENDPOINTS = {"AGENTIHOOKS_LEDGER_URL": "http://ledger.swarm.svc:8765", "BRAIN_URL": "https://brain.swarm.svc"}
ACCOUNTS = {"claude": "AH_CC_TOKEN_POOL_A", "codex": "AH_CX_TOKEN_POOL_B"}


FIXTURES = Path(__file__).resolve().parents[1] / "docker" / "swarm-node" / "fixtures" / "profiles"


@pytest.fixture
def fixture(tmp_path):
    templates = tmp_path / "templates"
    shutil.copytree(FIXTURES, templates)
    volume = tmp_path / "volume"
    volume.mkdir()
    return templates, volume


def request(templates: Path, volume: Path, attempt: str = "attempt-1", **changes) -> worker_home.Request:
    values = {
        "root": volume,
        "attempt": attempt,
        "templates": templates,
        "profiles": {"claude": "fixture-claude", "codex": "fixture-codex"},
        "interpreter": Path(sys.executable),
        "accounts": dict(ACCOUNTS),
        "endpoints": dict(ENDPOINTS),
        "uid": os.geteuid(),
        "gid": os.getegid(),
    }
    return worker_home.Request(**(values | changes))


def snapshot(root: Path) -> dict[str, tuple]:
    return {
        str(p.relative_to(root)): (p.is_symlink(), p.read_bytes() if p.is_file() and not p.is_symlink() else None)
        for p in sorted(root.rglob("*"))
    }


def hook_commands(settings: dict) -> list[str]:
    return [h["command"] for groups in settings["hooks"].values() for g in groups for h in g["hooks"]]


def test_bootstrap_renders_both_targets_into_separate_homes(fixture):
    templates, volume = fixture
    record = worker_home.bootstrap(request(templates, volume))
    attempt = volume / "attempt-1"
    claude_home, codex_home = attempt / "homes" / "claude", attempt / "homes" / "codex"
    python = str(Path(sys.executable))

    assert record["reused"] is False
    assert record["homes"] == {"claude": "homes/claude", "codex": "homes/codex"}
    assert record["accounts"] == ACCOUNTS
    assert record["profiles"] == {"claude": "fixture-claude", "codex": "fixture-codex"}
    assert record["worker_profile_materialization_seconds"] > 0
    assert {**json.loads((attempt / worker_home.RECORD).read_text()), "reused": False} == record
    assert not (attempt / worker_home.PENDING).exists()
    assert (attempt / "run").is_dir() and (attempt / "tmp").is_dir()
    assert oct(attempt.stat().st_mode & 0o777) == "0o700"
    assert sorted(p.name for p in (attempt / "profiles").iterdir()) == ["fixture-claude", "fixture-codex"]

    settings = json.loads((claude_home / ".claude" / "settings.json").read_text())
    assert settings["enabledPlugins"] == {"fixture@market": True}
    assert settings["env"]["FIXTURE_PROFILE"] == "fixture-claude"
    assert {k: settings["env"][k] for k in ENDPOINTS} == ENDPOINTS
    assert all(python in command for command in hook_commands(settings))
    servers = json.loads((claude_home / ".claude.json").read_text())["mcpServers"]
    assert sorted(servers) == ["agentihooks", "fixture-claude-mcp"]
    assert servers["agentihooks"]["command"] == python

    config = tomllib.loads((codex_home / ".codex" / "config.toml").read_text())
    assert sorted(config["mcp_servers"]) == ["agentihooks", "fixture-codex-mcp"]
    assert config["mcp_servers"]["agentihooks"]["command"] == python
    assert config["model_reasoning_effort"] == "high"
    assert config["notify"][0] == python
    wrapper = codex_home / ".codex" / "agentihooks-hook.sh"
    hooks = json.loads((codex_home / ".codex" / "hooks.json").read_text())["hooks"]
    assert {h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]} == {str(wrapper)}
    script = wrapper.read_text()
    assert f"exec {python} -m hooks" in script
    assert all(f'export {key}="${{{key}:={value}}}"' in script for key, value in ENDPOINTS.items())

    claude_text = "".join(p.read_text() for p in claude_home.rglob("*.json") if p.is_file())
    codex_text = "".join(p.read_text() for p in codex_home.rglob("*") if p.is_file() and p.suffix in (".toml", ".json"))
    assert str(codex_home) not in claude_text and str(claude_home) not in codex_text
    assert "AH_CC_TOKEN_POOL_A" not in claude_text + codex_text


def comparable(volume: Path) -> dict:
    attempt = volume / "attempt-1"
    docs = {
        "claude": json.loads((attempt / "homes/claude/.claude/settings.json").read_text()),
        "claude_mcp": json.loads((attempt / "homes/claude/.claude.json").read_text())["mcpServers"],
        "codex": tomllib.loads((attempt / "homes/codex/.codex/config.toml").read_text()),
        "codex_hooks": json.loads((attempt / "homes/codex/.codex/hooks.json").read_text()),
    }
    return json.loads(json.dumps(docs).replace(str(volume), "<volume>"))


def test_second_independent_fixture_renders_the_same_configuration(fixture, tmp_path):
    templates, volume = fixture
    worker_home.bootstrap(request(templates, volume))
    other = tmp_path / "other-volume"
    other.mkdir()
    worker_home.bootstrap(request(templates, other))
    assert comparable(other) == comparable(volume)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"interpreter": Path(WORKSTATION_PYTHON)}, f"interpreter cannot run agentihooks: {WORKSTATION_PYTHON}"),
        ({"attempt": "../escape"}, "invalid attempt id: ../escape"),
        ({"profiles": {"claude": "../fixture-claude"}}, "invalid profile name: ../fixture-claude"),
        ({"profiles": {"copilot": "fixture-claude"}}, "unsupported target: copilot"),
        ({"profiles": {}}, "no target profiles requested"),
        ({"profiles": {"claude": "missing"}}, "profile template not found: missing"),
        ({"accounts": {"claude": "sk-ant-secret value"}}, "invalid account reference for claude"),
        ({"accounts": {"copilot": "AH_CP_TOKEN"}}, "account reference for unrequested target: copilot"),
        ({"endpoints": {"BRAIN_URL": "https://user:pw@brain.svc"}}, "invalid service endpoint: BRAIN_URL"),
        ({"endpoints": {"BRAIN_URL": "file:///etc/passwd"}}, "invalid service endpoint: BRAIN_URL"),
        ({"endpoints": {"bad-key": "https://brain.svc"}}, "invalid service endpoint: bad-key"),
        ({"uid": os.geteuid() + 1}, f"bootstrap must run as {os.geteuid() + 1}:{os.getegid()}"),
        ({"gid": os.getegid() + 1}, f"bootstrap must run as {os.geteuid()}:{os.getegid() + 1}"),
    ],
)
def test_invalid_request_is_refused_before_any_write(fixture, changes, message):
    templates, volume = fixture
    worker_home.bootstrap(request(templates, volume, attempt="accepted"))
    before = snapshot(volume)
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume, **({"attempt": "attempt-2"} | changes)))
    assert str(error.value) == message
    assert snapshot(volume) == before


def test_profile_pointing_at_a_workstation_venv_fails_bootstrap(fixture):
    templates, volume = fixture
    worker_home.bootstrap(request(templates, volume, attempt="accepted"))
    before, sources = snapshot(volume), snapshot(templates)
    bad = request(templates, volume, profiles={"claude": "fixture-workstation", "codex": "fixture-codex"})
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(bad)
    assert str(error.value) == f"claude hook command leaves the execution root: {WORKSTATION_PYTHON}"
    assert snapshot(volume) == before
    assert snapshot(templates) == sources
    assert not (volume / "attempt-1").exists()


@pytest.mark.parametrize(
    ("command", "offending"),
    [
        (f"PYTHONPATH={sys.prefix}:/etc/shadow python -m hooks", "/etc/shadow"),
        ("/usr/bin/env:/root/.ssh/id_rsa python -m hooks", "/root/.ssh/id_rsa"),
        ("../../../../home/operator/.venv/bin/python -m hooks", "../../../../home/operator/.venv/bin/python"),
        ("python -c 'import sys' https://brain.svc/x /etc/passwd", "/etc/passwd"),
        (f"PYTHONPATH={sys.prefix}:file:///etc/shadow python -m hooks", "/etc/shadow"),
        ("python -m hooks --config=file:///etc/x", "/etc/x"),
        ("~/.venv/bin/python -m hooks", "~/.venv/bin/python"),
        ("$HOME/.venv/bin/python -m hooks", "$HOME/.venv/bin/python"),
    ],
)
def test_hidden_or_relative_paths_in_a_hook_fail_bootstrap(fixture, command, offending):
    templates, volume = fixture
    settings = templates / "fixture-workstation" / ".claude" / "settings.overrides.json"
    document = json.loads(settings.read_text())
    document["hooks"]["SessionStart"][0]["hooks"][0]["command"] = command
    settings.write_text(json.dumps(document))
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume, profiles={"claude": "fixture-workstation"}, accounts={}))
    assert str(error.value) == f"claude hook command leaves the execution root: {offending}"
    assert list(volume.iterdir()) == []


def test_url_arguments_and_contained_relative_paths_are_admitted(fixture):
    templates, volume = fixture
    settings = templates / "fixture-workstation" / ".claude" / "settings.overrides.json"
    document = json.loads(settings.read_text())
    document["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "python bin/probe.py https://brain.svc/x/y"
    settings.write_text(json.dumps(document))
    record = worker_home.bootstrap(request(templates, volume, profiles={"claude": "fixture-workstation"}, accounts={}))
    assert record["profiles"] == {"claude": "fixture-workstation"}


def test_a_workstation_path_in_a_settings_environment_value_fails_bootstrap(fixture):
    templates, volume = fixture
    settings = templates / "fixture-claude" / ".claude" / "settings.overrides.json"
    document = json.loads(settings.read_text())
    document["env"]["PATH"] = "/home/operator/dev/tcc-ecosystem/.venv/bin:/usr/bin"
    settings.write_text(json.dumps(document))
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume, profiles={"claude": "fixture-claude"}, accounts={}))
    assert (
        str(error.value)
        == "claude environment value leaves the execution root: /home/operator/dev/tcc-ecosystem/.venv/bin"
    )
    assert list(volume.iterdir()) == []


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_workstation_path_in_an_mcp_environment_fails_bootstrap(fixture, target):
    templates, volume = fixture
    mcp = templates / f"fixture-{target}" / ".mcp.json"
    document = json.loads(mcp.read_text())
    document["mcpServers"][f"fixture-{target}-mcp"]["env"] = {"VIRTUAL_ENV": "/home/operator/.venv"}
    mcp.write_text(json.dumps(document))
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume, profiles={target: f"fixture-{target}"}, accounts={}))
    assert str(error.value) == f"{target} MCP server leaves the execution root: /home/operator/.venv"
    assert list(volume.iterdir()) == []


def test_a_missing_execution_root_is_refused(fixture, tmp_path, capsys):
    templates, _ = fixture
    missing = tmp_path / "missing"
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, missing))
    assert str(error.value) == f"execution root not found: {missing}"
    assert not missing.exists()


def test_render_child_environment_points_into_the_attempt(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("PYTHONPATH", "/opt/code")
    monkeypatch.setenv("AH_CC_TOKEN_POOL_A", "secret-value")
    home = tmp_path / "homes" / "claude"
    assert worker_home.child_environment(home, Path("/opt/venv/bin/python")) == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(Path(worker_home.__file__).resolve().parents[2]),
        "HOME": str(home),
        "AGENTIHOOKS_HOME": str(home / ".agentihooks"),
        "AGENTIHOOKS_PYTHON": "/opt/venv/bin/python",
        "AGENTIHOOKS_MCP_TRANSPORT": "stdio",
    }


def test_bootstrap_holds_an_exclusive_lock_on_the_volume(fixture, monkeypatch):
    templates, volume = fixture
    calls = []
    monkeypatch.setattr(
        worker_home.fcntl, "flock", lambda fd, operation: calls.append((os.readlink(f"/proc/self/fd/{fd}"), operation))
    )
    worker_home.bootstrap(request(templates, volume))
    assert calls == [(str(volume), worker_home.fcntl.LOCK_EX)]


def test_record_names_the_digest_of_each_selected_profile(fixture):
    templates, volume = fixture
    record = worker_home.bootstrap(request(templates, volume))
    assert sorted(record["profile_digests"]) == ["fixture-claude", "fixture-codex"]
    (templates / "fixture-claude" / "CLAUDE.md").write_text("# changed\n")
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume))
    assert str(error.value) == "attempt attempt-1 was accepted from a different request"
    other = worker_home.bootstrap(request(templates, volume, attempt="attempt-2"))
    assert other["profile_digests"]["fixture-claude"] != record["profile_digests"]["fixture-claude"]
    assert other["profile_digests"]["fixture-codex"] == record["profile_digests"]["fixture-codex"]


def test_profile_link_escaping_its_template_fails_bootstrap(fixture, tmp_path):
    templates, volume = fixture
    outside = tmp_path / "outside"
    outside.mkdir()
    skills = templates / "fixture-claude" / ".claude" / "skills"
    skills.mkdir()
    (skills / "escape").symlink_to(outside)
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume))
    assert str(error.value) == "profile escapes its template: fixture-claude"
    assert list(volume.iterdir()) == []


def test_rendered_link_escaping_the_execution_root_fails_bootstrap(fixture, tmp_path, monkeypatch):
    templates, volume = fixture
    outside = tmp_path / "outside"
    outside.mkdir()
    render = worker_home.render

    def plant(attempt, target):
        render(attempt, target)
        if target == "codex":
            (attempt / "homes" / "codex" / "escape").symlink_to(outside)

    monkeypatch.setattr(worker_home, "render", plant)
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume))
    assert str(error.value) == f"codex link leaves the execution root: {outside}"
    assert list(volume.iterdir()) == []


def test_failed_render_removes_the_unstarted_attempt(fixture, monkeypatch):
    templates, volume = fixture
    monkeypatch.setattr(
        worker_home, "child_command", lambda attempt, target: [sys.executable, "-c", "raise SystemExit(3)"]
    )
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume))
    assert str(error.value) == "claude render failed with exit 3"
    assert list(volume.iterdir()) == []


def mount(flags: int) -> SimpleNamespace:
    return SimpleNamespace(f_flag=flags)


@pytest.mark.parametrize(
    ("profiles", "refused"), [({"codex": "fixture-codex"}, True), ({"claude": "fixture-claude"}, False)]
)
def test_a_noexec_volume_refuses_codex_whose_hook_wrapper_must_execute(fixture, monkeypatch, profiles, refused):
    templates, volume = fixture
    seen = []

    def statvfs(path):
        seen.append(path)
        return mount(os.ST_NOEXEC | os.ST_NOSUID)

    monkeypatch.setattr(worker_home.os, "statvfs", statvfs)
    if refused:
        with pytest.raises(worker_home.BootstrapError) as error:
            worker_home.bootstrap(request(templates, volume, profiles=profiles, accounts={}))
        assert str(error.value) == "execution root is mounted noexec, so the codex hook wrapper cannot run"
        assert seen == [volume]
        assert list(volume.iterdir()) == []
    else:
        assert worker_home.bootstrap(request(templates, volume, profiles=profiles, accounts={}))["reused"] is False


def test_an_exec_volume_with_other_flags_admits_codex(fixture, monkeypatch):
    templates, volume = fixture
    monkeypatch.setattr(worker_home.os, "statvfs", lambda path: mount(os.ST_NOSUID | os.ST_NODEV))
    record = worker_home.bootstrap(request(templates, volume, profiles={"codex": "fixture-codex"}, accounts={}))
    assert record["homes"] == {"codex": "homes/codex"}


def test_restarting_an_accepted_attempt_changes_nothing(fixture):
    templates, volume = fixture
    first = worker_home.bootstrap(request(templates, volume))
    attempt = volume / "attempt-1"
    before = {p: (p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in attempt.rglob("*")}
    second = worker_home.bootstrap(request(templates, volume))
    assert second == {**first, "reused": True}
    assert {p: (p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in attempt.rglob("*")} == before


def test_interrupted_bootstrap_restarts_without_duplicate_entries(fixture, tmp_path, monkeypatch):
    templates, volume = fixture
    clean = tmp_path / "clean"
    clean.mkdir()
    worker_home.bootstrap(request(templates, clean))
    render = worker_home.render

    def crash(attempt, target):
        render(attempt, target)
        if target == "codex":
            raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(worker_home, "render", crash)
        with pytest.raises(KeyboardInterrupt):
            worker_home.bootstrap(request(templates, volume))
    assert (volume / "attempt-1" / worker_home.PENDING).is_file()
    assert not (volume / "attempt-1" / worker_home.RECORD).exists()

    record = worker_home.bootstrap(request(templates, volume))
    assert record["reused"] is False
    assert comparable(volume) == comparable(clean)
    docs = comparable(volume)
    commands = hook_commands(docs["claude"])
    assert len(docs["claude"]["hooks"]["SessionStart"]) == 2
    assert len(commands) == len(hook_commands(comparable(clean)["claude"]))
    assert list(docs["claude"]["enabledPlugins"]) == ["fixture@market"]
    assert sorted(docs["codex"]["mcp_servers"]) == ["agentihooks", "fixture-codex-mcp"]
    assert [len(groups) for groups in docs["codex_hooks"]["hooks"].values()] == [1] * len(docs["codex_hooks"]["hooks"])


def test_a_different_request_cannot_replace_an_accepted_attempt(fixture):
    templates, volume = fixture
    worker_home.bootstrap(request(templates, volume))
    before = snapshot(volume)
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume, endpoints={}))
    assert str(error.value) == "attempt attempt-1 was accepted from a different request"
    assert snapshot(volume) == before


def test_a_foreign_folder_at_the_attempt_path_is_refused(fixture):
    templates, volume = fixture
    foreign = volume / "attempt-1"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("not a bootstrap home")
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume))
    assert str(error.value) == "attempt-1 holds files bootstrap did not write"
    assert (foreign / "notes.txt").read_text() == "not a bootstrap home"


def test_cli_prints_the_accepted_record(fixture, capsys):
    templates, volume = fixture
    argv = [
        "bootstrap",
        f"--root={volume}",
        "--attempt=attempt-1",
        f"--templates={templates}",
        f"--interpreter={sys.executable}",
        f"--uid={os.geteuid()}",
        f"--gid={os.getegid()}",
        "--profile=claude=fixture-claude",
        "--profile=codex=fixture-codex",
        "--account=claude=AH_CC_TOKEN_POOL_A",
        "--endpoint=BRAIN_URL=https://brain.swarm.svc",
    ]
    assert worker_home.main(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["profiles"] == {"claude": "fixture-claude", "codex": "fixture-codex"}
    assert printed["accounts"] == {"claude": "AH_CC_TOKEN_POOL_A"}
    assert printed["endpoints"] == {"BRAIN_URL": "https://brain.swarm.svc"}


def test_cli_reports_a_refusal(fixture, capsys):
    templates, volume = fixture
    argv = [
        "bootstrap",
        f"--root={volume}",
        "--attempt=../x",
        f"--templates={templates}",
        "--profile=claude=fixture-claude",
    ]
    assert worker_home.main(argv) == 1
    assert capsys.readouterr().err == "ERROR: invalid attempt id: ../x\n"
