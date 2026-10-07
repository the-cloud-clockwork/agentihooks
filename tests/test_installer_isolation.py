import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _run_probe(tmp_path, body, preload="", basetemp=None):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "conftest.py").write_text((REPO / "tests" / "conftest.py").read_text())
    (suite / "test_probe.py").write_text(preload + "\ndef test_probe():\n" + body)
    sentinel = tmp_path / "operator"
    sentinel.mkdir()
    if basetemp is not None:
        basetemp.parent.mkdir(parents=True)
    env = dict(os.environ, HOME=str(sentinel), PYTHONPATH=f"{REPO}:{REPO / 'scripts'}")
    for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_HOME_DIR", "AGENTIHOOKS_CLAUDE_HOME", "AGENTIHOOKS_HOME"):
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(suite),
            "-q",
            "--confcutdir",
            str(suite),
            *([] if basetemp is None else ["--basetemp", str(basetemp)]),
        ],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, sentinel


@pytest.mark.parametrize("seed_state", [False, True])
def test_external_render_fixture_requires_home_isolation(tmp_path, seed_state):
    suite = tmp_path / "external"
    suite.mkdir()
    (suite / "test_render.py").write_text(
        "from tests.test_profile_render import world\n\ndef test_render(world):\n    assert world['bundle'].is_dir()\n"
    )
    operator = tmp_path / "operator"
    state_dir = operator / ".agentihooks"
    state_dir.mkdir(parents=True)
    state = state_dir / "state.json"
    backup = state_dir / "state.json.bak"
    if seed_state:
        state.write_bytes(b'{"bundle":{"path":"operator-bundle"}}\n')
        backup.write_bytes(b'{"previous":"operator-state"}\n')
    before = {path.name: path.read_bytes() for path in state_dir.iterdir()}
    env = dict(os.environ, HOME=str(operator), PYTHONPATH=f"{REPO}:{REPO / 'scripts'}")
    for key in (
        "AGENTIHOOKS_HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_HOME_DIR",
        "AGENTIHOOKS_CLAUDE_HOME",
        "CODEX_HOME",
        "COPILOT_HOME",
    ):
        env.pop(key, None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(suite),
            "-q",
            "--confcutdir",
            str(suite),
            "--basetemp",
            str(tmp_path / "pytest"),
        ],
        env=env,
        cwd=suite,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert {path.name: path.read_bytes() for path in state_dir.iterdir()} == before
    assert list(operator.iterdir()) == [state_dir]
    assert result.returncode == 1, result.stdout + result.stderr
    assert "fixture '_isolate_real_user_paths' not found" in result.stdout


@pytest.mark.parametrize(
    "preload", ["import install", "import scripts.install", "import install\nimport scripts.install"]
)
def test_installer_imports_write_only_isolated_paths(tmp_path, preload):
    result, sentinel = _run_probe(
        tmp_path,
        "    import install\n"
        "    import scripts.install\n"
        "    for installer in (install, scripts.install):\n"
        "        installer._save_state({'isolated': True})\n"
        "        installer.save_json(installer.CLAUDE_HOME / 'probe.json', {'isolated': True})\n"
        "        assert installer.STATE_JSON.exists()\n"
        "        assert installer._ENV_FILE_DST.parent == installer.AGENTIHOOKS_STATE_DIR\n"
        "        with installer._sync_lock():\n"
        "            assert installer._SYNC_LOCK_FILE.parent == installer.AGENTIHOOKS_STATE_DIR\n",
        preload,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(sentinel.iterdir()) == []


@pytest.mark.parametrize("operation", ["state", "target", "direct"])
def test_installer_guard_refuses_write_before_mutation(tmp_path, monkeypatch, operation):
    body = (
        "    import os\n"
        "    from pathlib import Path\n"
        "    import scripts.install as installer\n"
        "    operator = Path(os.environ['OPERATOR_HOME'])\n"
    )
    if operation == "state":
        body += "    installer.STATE_JSON = operator / '.agentihooks' / 'state.json'\n    installer._save_state({})\n"
    elif operation == "direct":
        body += "    (operator / '.claude').mkdir()\n"
    else:
        body += "    os.environ['CODEX_HOME'] = str(operator / '.codex')\n    installer.get_adapter('codex').write_settings({})\n"
    monkeypatch.setenv("OPERATOR_HOME", str(tmp_path / "operator"))
    result, sentinel = _run_probe(tmp_path, body, "import scripts.install")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "refusing installer write outside the test directory" in result.stdout
    assert list(sentinel.iterdir()) == []


@pytest.mark.parametrize("operation", ["os.ftruncate(stream.fileno(), 0)", "os.fchmod(stream.fileno(), 0)"])
def test_guard_refuses_mutation_through_an_open_descriptor(tmp_path, monkeypatch, operation):
    monkeypatch.setenv("OPERATOR_HOME", str(tmp_path / "operator"))
    preload = (
        "import os\n"
        "from pathlib import Path\n"
        "stream = (Path(os.environ['OPERATOR_HOME']) / '.claude.json').open('w+')\n"
        "stream.write('sentinel')\n"
        "stream.flush()\n"
    )
    result, sentinel = _run_probe(tmp_path, f"    {operation}\n", preload)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "refusing installer write outside the test directory" in result.stdout
    assert (sentinel / ".claude.json").read_text() == "sentinel"
    assert (sentinel / ".claude.json").stat().st_mode & 0o600 == 0o600


def test_installer_fixture_allows_scratchpad_home(tmp_path):
    scratchpad = tmp_path / "operator" / "scratchpad"
    result, sentinel = _run_probe(
        tmp_path,
        "    from pathlib import Path\n"
        "    import scripts.install as installer\n"
        "    assert 'scratchpad' in [parent.name for parent in Path.home().parents]\n"
        "    installer._save_state({'isolated': True})\n"
        "    import json\n"
        "    assert json.loads(installer.STATE_JSON.read_text()) == {'isolated': True}\n",
        basetemp=scratchpad / "tests",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(sentinel.iterdir()) == [scratchpad]


@pytest.mark.parametrize(
    "relative",
    [
        ".claude",
        ".claude.json",
        ".codex",
        ".copilot",
        ".agentihooks",
        ".agents",
        ".bashrc",
        ".local/bin",
        ".config/systemd/user",
    ],
)
@pytest.mark.parametrize("descendant", [False, True])
def test_home_resolver_refuses_live_install_paths(tmp_path, monkeypatch, relative, descendant):
    live = tmp_path / "operator" / relative
    if descendant:
        live /= "probe"
    monkeypatch.setenv("LIVE_INSTALL", str(live))
    preload = (
        "import os\n"
        "from pathlib import Path\n"
        "import scripts.claude_config\n"
        "scripts.claude_config.claude_home = lambda: Path(os.environ['LIVE_INSTALL'])\n"
    )
    result, sentinel = _run_probe(tmp_path, "    raise AssertionError('test body must not run')\n", preload)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "still resolves to a live install path" in result.stdout
    assert list(sentinel.iterdir()) == []


def test_home_resolver_refuses_symlink_to_live_install(tmp_path, monkeypatch):
    alias = tmp_path / "scratchpad" / "home"
    alias.parent.mkdir()
    alias.symlink_to(tmp_path / "operator" / ".claude", target_is_directory=True)
    monkeypatch.setenv("LIVE_INSTALL", str(alias))
    preload = (
        "import os\n"
        "from pathlib import Path\n"
        "import scripts.claude_config\n"
        "scripts.claude_config.claude_home = lambda: Path(os.environ['LIVE_INSTALL'])\n"
    )
    result, sentinel = _run_probe(tmp_path, "    raise AssertionError('test body must not run')\n", preload)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "still resolves to a live install path" in result.stdout
    assert list(sentinel.iterdir()) == []


def test_installer_guard_preserves_live_state_and_backup(tmp_path, monkeypatch):
    operator = tmp_path / "operator"
    monkeypatch.setenv("OPERATOR_HOME", str(operator))
    preload = (
        "import os\n"
        "from pathlib import Path\n"
        "state_dir = Path(os.environ['OPERATOR_HOME']) / '.agentihooks'\n"
        "state_dir.mkdir()\n"
        "(state_dir / 'state.json').write_text('{\"bundle\":\"operator\"}\\n')\n"
        "(state_dir / 'state.json.bak').write_text('{\"previous\":\"operator\"}\\n')\n"
    )
    result, sentinel = _run_probe(
        tmp_path,
        "    import scripts.install as installer\n"
        "    installer.STATE_JSON = state_dir / 'state.json'\n"
        "    installer._save_state({'isolated': True})\n",
        preload,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "refusing installer write outside the test directory" in result.stdout
    assert (sentinel / ".agentihooks" / "state.json").read_text() == '{"bundle":"operator"}\n'
    assert (sentinel / ".agentihooks" / "state.json.bak").read_text() == '{"previous":"operator"}\n'
