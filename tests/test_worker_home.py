import contextlib
import io
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_v2 import filesystem, worker_home

WORKSTATION_PYTHON = "/home/operator/dev/tcc-ecosystem/.venv/bin/python"
ENDPOINTS = {"AGENTIHOOKS_LEDGER_URL": "http://ledger.swarm.svc:8765", "BRAIN_URL": "https://brain.swarm.svc"}
ACCOUNTS = {"claude": "AH_CC_TOKEN_POOL_A", "codex": "AH_CX_TOKEN_POOL_B"}


FIXTURES = Path(__file__).resolve().parents[1] / "docker" / "swarm-node" / "fixtures" / "profiles"


REAL_RENDER = worker_home.render
CODE_ROOT = Path(worker_home.__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def inprocess(monkeypatch):
    import install

    import scripts.install as package_install

    def render(attempt: Path, target: str) -> None:
        home = attempt / "homes" / target
        state = home / ".agentihooks"
        paths = {
            "CLAUDE_HOME": home / ".claude",
            "AGENTIHOOKS_STATE_DIR": state,
            "STATE_JSON": state / "state.json",
            "_CLAUDE_JSON": home / ".claude.json",
            "_SYNC_LOCK_FILE": state / "sync.lock",
            "AGENTIHOOKS_ROOT": CODE_ROOT,
        }
        pending = json.loads((attempt / worker_home.PENDING).read_text())
        with monkeypatch.context() as patch:
            for module in (install, package_install):
                for name, value in paths.items():
                    patch.setattr(module, name, value)
            patch.setattr(Path, "home", classmethod(lambda cls: home))
            patch.setenv("HOME", str(home))
            patch.setenv("AGENTIHOOKS_PYTHON", pending["request"]["interpreter"])
            patch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "stdio")
            with contextlib.redirect_stdout(io.StringIO()):
                worker_home.materialize(attempt, target)

    monkeypatch.setattr(worker_home, "render", render)


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


def test_bootstrap_allocates_the_layout_seals_seeds_and_records_relative_paths(fixture):
    templates, volume = fixture
    record = worker_home.bootstrap(request(templates, volume))
    attempt = volume / "attempt-1"
    layout = filesystem.load()
    assert record["layout"] == filesystem.mapping(layout)
    assert json.loads((attempt / worker_home.RECORD).read_text())["layout"] == filesystem.mapping(layout)
    assert all((attempt / folder).is_dir() for folder in layout.roots.values())
    seeds = [p for p in (attempt / "profiles").rglob("*") if not p.is_symlink()]
    assert seeds and not any(p.lstat().st_mode & 0o222 for p in seeds)
    assert (attempt / "homes" / "claude").stat().st_mode & 0o200


def test_a_template_link_that_leaves_the_seed_copy_fails_bootstrap_and_removes_the_attempt(fixture):
    templates, volume = fixture
    profile = templates / "fixture-claude"
    (profile / "persona.md").symlink_to(profile / "CLAUDE.md")
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume))
    copy = volume / "attempt-1" / "profiles" / "fixture-claude" / "persona.md"
    assert str(error.value) == f"path resolves outside its execution root: {copy}"
    assert not (volume / "attempt-1").exists()


def test_a_missing_layout_fails_bootstrap_and_leaves_no_attempt(fixture, monkeypatch):
    templates, volume = fixture
    monkeypatch.setattr(filesystem, "LAYOUTS", (volume / "layout.json",))
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume))
    assert str(error.value) == "no layout file is installed, so new launches stop"
    assert list(volume.iterdir()) == []


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
        ({"interpreter": Path("/")}, "interpreter cannot run agentihooks: /"),
        ({"interpreter": Path("/bin/false")}, "interpreter cannot run agentihooks: /bin/false"),
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
    assert str(error.value) == f"claude setting hooks leaves the execution root: {WORKSTATION_PYTHON}"
    assert snapshot(volume) == before
    assert snapshot(templates) == sources
    assert not (volume / "attempt-1").exists()


def claude_home(attempt: Path, settings: dict, servers: dict | None = None) -> None:
    home = attempt / "homes" / "claude"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(settings))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers or {}}))


def codex_home(attempt: Path, config: str = "", wrapper: str = "", command: str = "") -> None:
    home = attempt / "homes" / "codex" / ".codex"
    home.mkdir(parents=True)
    (home / "config.toml").write_text(config)
    hooks = {
        "SessionStart": [{"hooks": [{"type": "command", "command": command or str(home / "agentihooks-hook.sh")}]}]
    }
    (home / "hooks.json").write_text(json.dumps({"hooks": hooks}))
    (home / "agentihooks-hook.sh").write_text("#!/usr/bin/env bash\n" + wrapper)


def check(attempt: Path, target: str) -> None:
    roots = [attempt.resolve(), Path(sys.prefix).resolve(), *worker_home.SYSTEM_ROOTS]
    worker_home._check_home(attempt, target, roots, (os.geteuid(), os.getegid()))


def hook(command: str) -> dict:
    return {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}


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
        (f"x={sys.prefix}:///etc/shadow python -m hooks", "/etc/shadow"),
        (f"PYTHONPATH={sys.prefix}://etc/shadow python -m hooks", "/etc/shadow"),
        (f"{sys.prefix}/$X/etc/shadow -m hooks", f"{sys.prefix}/$X/etc/shadow"),
        ("$AGENTIHOOKS_PYTHON -m hooks", "$AGENTIHOOKS_PYTHON"),
        ("~ -m hooks", "~"),
        ("python -m hooks --config=git+file:///etc/x", "/etc/x"),
        ("python -m hooks 'unbalanced /etc/y", "/etc/y"),
    ],
)
def test_hidden_or_relative_paths_in_a_hook_are_refused(tmp_path, command, offending):
    claude_home(tmp_path, hook(command))
    with pytest.raises(worker_home.BootstrapError) as error:
        check(tmp_path, "claude")
    assert str(error.value) == f"claude setting hooks leaves the execution root: {offending}"


@pytest.mark.parametrize(
    "command",
    [
        "python bin/probe.py https://brain.svc/x/y",
        f"cd {sys.prefix} && {sys.prefix}/bin/python -m hooks",
        f"{sys.prefix}/./bin/python",
        "python -m hooks --url=http://ledger.svc:8765/x",
    ],
)
def test_url_arguments_and_contained_paths_are_admitted(tmp_path, command):
    claude_home(tmp_path, hook(command) | {"permissions": {"deny": ["Read(~/.ssh/**)", "Read(/etc/**)"]}})
    check(tmp_path, "claude")


def test_a_workstation_path_in_a_settings_environment_value_is_refused(tmp_path):
    claude_home(tmp_path, {"env": {"PATH": "/home/operator/dev/tcc-ecosystem/.venv/bin:/usr/bin"}})
    with pytest.raises(worker_home.BootstrapError) as error:
        check(tmp_path, "claude")
    assert (
        str(error.value) == "claude setting env leaves the execution root: /home/operator/dev/tcc-ecosystem/.venv/bin"
    )


def test_an_executable_setting_outside_the_root_is_refused(tmp_path):
    claude_home(tmp_path, {"apiKeyHelper": "/home/operator/bin/key.sh"})
    with pytest.raises(worker_home.BootstrapError) as error:
        check(tmp_path, "claude")
    assert str(error.value) == "claude setting apiKeyHelper leaves the execution root: /home/operator/bin/key.sh"


def test_a_workstation_path_in_a_claude_mcp_environment_is_refused(tmp_path):
    claude_home(tmp_path, {}, {"x": {"command": "python", "env": {"VIRTUAL_ENV": "/home/operator/.venv"}}})
    with pytest.raises(worker_home.BootstrapError) as error:
        check(tmp_path, "claude")
    assert str(error.value) == "claude MCP server leaves the execution root: /home/operator/.venv"


@pytest.mark.parametrize(
    ("config", "wrapper", "command", "message"),
    [
        (
            '[mcp_servers.x]\ncommand = "python"\nenv = { VIRTUAL_ENV = "/home/operator/.venv" }\n',
            "",
            "",
            "MCP server leaves the execution root: /home/operator/.venv",
        ),
        (
            'notify = ["/home/operator/.venv/bin/python", "-m", "x"]\n',
            "",
            "",
            "setting notify leaves the execution root: /home/operator/.venv/bin/python",
        ),
        (
            "",
            "export A=\"${A:='/home/operator/.venv'}\"\n",
            "",
            "environment value leaves the execution root: /home/operator/.venv",
        ),
        (
            "",
            "cd /home/operator/agentihooks\n",
            "",
            "hook command leaves the execution root: /home/operator/agentihooks",
        ),
        ("", "", "/home/operator/hook.sh", "hook command leaves the execution root: /home/operator/hook.sh"),
    ],
)
def test_codex_surfaces_outside_the_root_are_refused(tmp_path, config, wrapper, command, message):
    codex_home(tmp_path, config, wrapper, command)
    with pytest.raises(worker_home.BootstrapError) as error:
        check(tmp_path, "codex")
    assert str(error.value) == f"codex {message}"


def test_codex_exports_are_read_as_values_and_other_lines_as_commands():
    script = 'header\nexport A="${A:=\'/opt/x y\'}"\nexport B="${C:=/z}"\nexport D="${D:=/w"\nD="${D:=/v}"\nset -e\n'
    expected = ['export B="${C:=/z}"', 'export D="${D:=/w"', 'D="${D:=/v}"', "set -e"]
    assert worker_home._wrapper(script) == (["/opt/x y"], expected)
    assert worker_home._strings({"a": ["x", {"b": "y"}, 3, None], "c": "z"}) == ["x", "y", "z"]


def test_a_home_file_owned_by_another_user_is_refused(tmp_path, monkeypatch):
    claude_home(tmp_path, {})
    real = Path.lstat
    target = tmp_path / "homes" / "claude" / ".claude.json"

    def lstat(path):
        found = real(path)
        if path == target:
            return SimpleNamespace(st_uid=found.st_uid + 1, st_gid=found.st_gid, st_mode=found.st_mode)
        return found

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(worker_home.BootstrapError) as error:
        check(tmp_path, "claude")
    assert str(error.value) == f"claude home holds a file not owned by {os.geteuid()}:{os.getegid()}"


def test_a_missing_execution_root_is_refused(fixture, tmp_path):
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


def test_tree_digest_follows_link_targets_inside_a_template(tmp_path):
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.md").write_text("b")
    (tmp_path / "link").symlink_to("a.md")
    first = worker_home._tree_digest(tmp_path)
    (tmp_path / "link").unlink()
    (tmp_path / "link").symlink_to("b.md")
    assert worker_home._tree_digest(tmp_path) != first
    worker_home._check_template(tmp_path, "inside")


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
    monkeypatch.setattr(worker_home, "render", REAL_RENDER)
    monkeypatch.setattr(
        worker_home, "child_command", lambda attempt, target: [sys.executable, "-c", "raise SystemExit(3)"]
    )
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume))
    assert str(error.value) == "claude render failed with exit 3"
    assert list(volume.iterdir()) == []


def test_render_runs_the_child_in_the_target_home_with_its_environment(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    for folder in ("run", "homes/codex"):
        (attempt / folder).mkdir(parents=True)
    (attempt / worker_home.PENDING).write_text(json.dumps({"request": {"interpreter": "/opt/venv/bin/python"}}))
    assert worker_home.child_command(attempt, "codex") == [
        sys.executable,
        "-m",
        "scripts.swarm_v2.worker_home",
        "render",
        str(attempt),
        "codex",
    ]
    fields = "{'cwd': os.getcwd(), 'home': os.environ['HOME'], 'python': os.environ['AGENTIHOOKS_PYTHON']}"
    probe = f"import json, os; print(json.dumps({fields}))"
    monkeypatch.setattr(worker_home, "child_command", lambda path, target: [sys.executable, "-c", probe])
    REAL_RENDER(attempt, "codex")
    home = str(attempt / "homes" / "codex")
    logged = json.loads((attempt / "run" / "render-codex.log").read_text())
    assert logged == {"cwd": home, "home": home, "python": "/opt/venv/bin/python"}


def test_materialize_refuses_a_profile_outside_the_execution_scope(tmp_path):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    document = {"request": {"profiles": {"claude": "not-linked"}, "endpoints": {}}}
    (attempt / worker_home.PENDING).write_text(json.dumps(document))
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.materialize(attempt, "claude")
    assert str(error.value) == "profile did not resolve in the execution scope: not-linked"


def test_cli_render_dispatches_to_materialize(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(worker_home, "materialize", lambda attempt, target: calls.append((attempt, target)))
    assert worker_home.main(["render", str(tmp_path), "codex"]) == 0
    assert calls == [(tmp_path, "codex")]


def test_cli_refuses_a_pair_without_a_value(capsys):
    assert worker_home.main(["bootstrap", "--attempt=a", "--profile=claude"]) == 1
    assert capsys.readouterr().err == "ERROR: --profile needs KEY=VALUE: claude\n"


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


def test_request_document_and_record_hold_exact_fields(fixture):
    templates, volume = fixture
    req = request(templates, volume)
    document = {
        "attempt": "attempt-1",
        "profiles": {"claude": "fixture-claude", "codex": "fixture-codex"},
        "interpreter": sys.executable,
        "accounts": ACCOUNTS,
        "endpoints": ENDPOINTS,
        "uid": os.geteuid(),
        "gid": os.getegid(),
    }
    assert worker_home._document(req) == document
    assert worker_home._record(req, "abc", {"fixture-claude": "d1"}, 1.23456) == {
        "schema_version": 1,
        "package": "SV2-IMG-02",
        "attempt": "attempt-1",
        "digest": "abc",
        "profiles": document["profiles"],
        "profile_digests": {"fixture-claude": "d1"},
        "accounts": ACCOUNTS,
        "endpoints": ENDPOINTS,
        "interpreter": sys.executable,
        "homes": {"claude": "homes/claude", "codex": "homes/codex"},
        "worker_profile_materialization_seconds": 1.235,
    }


def test_json_is_indented_sorted_and_ends_with_a_newline():
    assert worker_home._json({"b": 1, "a": [2]}) == '{\n  "a": [\n    2\n  ],\n  "b": 1\n}\n'


def test_digest_does_not_depend_on_dict_order(fixture):
    templates, volume = fixture
    forward = request(templates, volume, profiles={"claude": "fixture-claude", "codex": "fixture-codex"})
    backward = request(templates, volume, profiles={"codex": "fixture-codex", "claude": "fixture-claude"})
    assert worker_home._digest(forward, {"a": "1", "b": "2"}) == worker_home._digest(backward, {"b": "2", "a": "1"})


def test_environment_falls_back_to_the_default_path(monkeypatch):
    monkeypatch.delenv("PATH", raising=False)
    assert worker_home._environment() == {"PATH": os.defpath, "LANG": "C.UTF-8"}


def test_interpreter_probe_runs_from_root_with_a_clean_environment(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=f" {tmp_path} \n")

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(worker_home.subprocess, "run", run)
    assert worker_home.interpreter_prefix(Path("/opt/venv/bin/python")) == tmp_path.resolve()
    assert calls == [
        (
            ["/opt/venv/bin/python", "-c", "import hooks, sys; print(sys.prefix)"],
            {
                "cwd": "/",
                "env": {"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                "capture_output": True,
                "text": True,
                "timeout": 30,
            },
        )
    ]


def test_code_roots_name_the_checkout_and_the_install(monkeypatch, tmp_path):
    import scripts.install as package_install

    monkeypatch.setattr(package_install, "install_root", lambda: tmp_path)
    assert worker_home.code_roots() == [CODE_ROOT, tmp_path.resolve()]


def test_render_log_holds_stderr_and_the_child_gets_its_attempt_and_target(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    for folder in ("run", "homes/claude"):
        (attempt / folder).mkdir(parents=True)
    (attempt / worker_home.PENDING).write_text(json.dumps({"request": {"interpreter": sys.executable}}))
    seen = []

    def command(path, target):
        seen.append((path, target))
        return [sys.executable, "-c", "import sys; print('out'); sys.stdout.flush(); print('err', file=sys.stderr)"]

    monkeypatch.setattr(worker_home, "child_command", command)
    REAL_RENDER(attempt, "claude")
    assert seen == [(attempt, "claude")]
    assert (attempt / "run" / "render-claude.log").read_text() == "out\nerr\n"


def test_an_endpoint_without_a_host_is_refused(fixture):
    templates, volume = fixture
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home.bootstrap(request(templates, volume, endpoints={"BRAIN_URL": "http:///brain"}))
    assert str(error.value) == "invalid service endpoint: BRAIN_URL"


def test_profile_features_install_beside_the_package_features(fixture):
    templates, volume = fixture
    claude = templates / "fixture-claude"
    for subdir, name in (
        ("agents", "fixture-agent.md"),
        ("commands", "fixture-command.md"),
        ("rules", "fixture-rule.md"),
    ):
        (claude / ".claude" / subdir).mkdir(parents=True)
        (claude / ".claude" / subdir / name).write_text(f"# {name}\n")
    skill = claude / ".claude" / "skills" / "fixture-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: fixture-skill\ndescription: fixture\n---\n")
    (claude / "persona-link.md").symlink_to("CLAUDE.md")
    worker_home.bootstrap(request(templates, volume))
    attempt = volume / "attempt-1"
    home = attempt / "homes" / "claude" / ".claude"
    assert (home / "skills" / "fixture-skill").exists() and (home / "skills" / "handoff").exists()
    assert (home / "agents" / "fixture-agent.md").exists()
    assert (home / "commands" / "fixture-command.md").exists()
    assert (home / "rules" / "fixture-rule.md").exists() and (home / "rules" / "agentihooks-toolbelt.md").exists()
    copy = attempt / "profiles" / "fixture-claude" / "persona-link.md"
    assert copy.is_symlink() and os.readlink(copy) == "CLAUDE.md"
    for target in ("claude", "codex"):
        state = attempt / "homes" / target / ".agentihooks"
        assert oct(state.stat().st_mode & 0o777) == "0o700"
        linked = [{"name": n, "path": str(attempt / "profiles" / n)} for n in ("fixture-claude", "fixture-codex")]
        assert json.loads((state / "state.json").read_text())["linked_profiles"] == linked
    for folder in ("run", "tmp"):
        assert oct((attempt / folder).stat().st_mode & 0o777) == "0o700"


def test_materialization_seconds_measure_elapsed_time(fixture, monkeypatch):
    templates, volume = fixture
    ticks = iter([100.0, 102.5])
    monkeypatch.setattr(worker_home, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    record = worker_home.bootstrap(request(templates, volume))
    assert record["worker_profile_materialization_seconds"] == 2.5


def test_a_permissions_rule_naming_an_outside_path_is_admitted(tmp_path):
    claude_home(tmp_path, {"permissions": {"additionalDirectories": ["/home/operator/dev"]}})
    check(tmp_path, "claude")


def test_every_codex_export_is_read_as_a_value():
    script = "header\nexport A=\"${A:='/opt/a'}\"\nexport B=\"${B:='/opt/b'}\"\n"
    assert worker_home._wrapper(script) == (["/opt/a", "/opt/b"], [])


@pytest.mark.parametrize(
    ("word", "pieces"),
    [
        ("http://a://etc", []),
        ("/x=1://etc", ["/x", "1", "/etc"]),
        ("1:///Xa", ["1", "/Xa"]),
    ],
)
def test_pieces_split_a_word_into_candidate_paths(word, pieces):
    assert worker_home._pieces(word) == pieces


def test_quoted_paths_stay_whole():
    assert worker_home._paths("python '/opt/x y'") == ["/opt/x y"]


def test_owner_refusal_names_the_expected_uid_and_gid(tmp_path, monkeypatch):
    claude_home(tmp_path, {})
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_uid=5, st_gid=7, st_mode=0o100644))
    roots = [tmp_path.resolve()]
    with pytest.raises(worker_home.BootstrapError) as error:
        worker_home._check_home(tmp_path, "claude", roots, (5, 8))
    assert str(error.value) == "claude home holds a file not owned by 5:8"


def test_pairs_keep_later_equals_in_the_value():
    assert worker_home._pairs(["URL=https://x?a=b"], "--endpoint") == {"URL": "https://x?a=b"}


def test_parser_defaults_and_types():
    parser = worker_home.build_parser()
    assert parser.prog == "python -m scripts.swarm_v2.worker_home"
    assert vars(parser.parse_args(["bootstrap", "--attempt", "a"])) == {
        "command": "bootstrap",
        "root": Path("/home/worker/attempts"),
        "attempt": "a",
        "templates": Path("/opt/agentihooks/templates"),
        "interpreter": Path("/opt/venv/bin/python"),
        "uid": 10001,
        "gid": 10001,
        "profile": [],
        "account": [],
        "endpoint": [],
    }
    args = parser.parse_args(
        ["bootstrap", "--attempt=a", "--root=/r", "--templates=/t", "--interpreter=/i", "--uid=5", "--gid=6"]
    )
    assert (args.root, args.templates, args.interpreter, args.uid, args.gid) == (
        Path("/r"),
        Path("/t"),
        Path("/i"),
        5,
        6,
    )
    assert vars(parser.parse_args(["render", "/a", "codex"])) == {
        "command": "render",
        "attempt": Path("/a"),
        "target": "codex",
    }


@pytest.mark.parametrize("argv", [[], ["bootstrap"], ["render", "/a", "copilot"]])
def test_parser_refuses_missing_or_unknown_arguments(argv, capsys):
    with pytest.raises(SystemExit) as stop:
        worker_home.build_parser().parse_args(argv)
    assert stop.value.code == 2


@pytest.mark.parametrize("flag", ["--account", "--endpoint"])
def test_cli_names_the_flag_of_a_pair_without_a_value(flag, capsys):
    assert worker_home.main(["bootstrap", "--attempt=a", f"{flag}=claude"]) == 1
    assert capsys.readouterr().err == f"ERROR: {flag} needs KEY=VALUE: claude\n"


def test_cli_prints_the_record_as_sorted_indented_json(fixture, capsys):
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
    ]
    assert worker_home.main(argv) == 0
    record = json.loads((volume / "attempt-1" / worker_home.RECORD).read_text())
    assert capsys.readouterr().out == json.dumps({**record, "reused": False}, indent=2, sort_keys=True) + "\n"
