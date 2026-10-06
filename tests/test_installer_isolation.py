import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _run_probe(tmp_path, body, preload=""):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "conftest.py").write_text((REPO / "tests" / "conftest.py").read_text())
    (suite / "test_probe.py").write_text(preload + "\ndef test_probe():\n" + body)
    sentinel = tmp_path / "operator"
    sentinel.mkdir()
    env = dict(os.environ, HOME=str(sentinel), PYTHONPATH=f"{REPO}:{REPO / 'scripts'}")
    for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_HOME_DIR", "AGENTIHOOKS_CLAUDE_HOME", "AGENTIHOOKS_HOME"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(suite), "-q", "--confcutdir", str(suite)],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, sentinel


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
