import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("registration", ["valid", "missing", "malformed", "wrong-cwd"])
def test_codex_profile_probe_finishes_on_valid_registration_only(tmp_path, registration):
    home = tmp_path / "home"
    home.mkdir()
    command = tmp_path / "codex"
    command.write_text(
        f"#!{sys.executable}\n"
        "import json, os, time\n"
        "from pathlib import Path\n"
        "home = Path(os.environ['HOME'])\n"
        "(home / 'pid').write_text(str(os.getpid()))\n"
        "path = home / '.agentihooks' / 'active-sessions.json'\n"
        "path.parent.mkdir()\n"
        "path.write_text('{')\n"
        "time.sleep(0.1)\n"
        f"mode = {registration!r}\n"
        "if mode == 'valid':\n"
        "    (home / 'registered').write_text(str(time.monotonic()))\n"
        "    path.write_text(json.dumps({'session': {'cwd': os.getcwd()}}))\n"
        "elif mode == 'missing':\n"
        "    path.write_text('{}')\n"
        "elif mode == 'wrong-cwd':\n"
        "    path.write_text(json.dumps({'session': {'cwd': '/wrong'}}))\n"
        "time.sleep(20)\n"
    )
    command.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/swarm_node/codex_probe.py"), str(home), "1"],
        cwd=tmp_path,
        env=os.environ | {"PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    ended = time.monotonic()
    assert result.returncode == (0 if registration == "valid" else 1), result.stderr
    assert int((home / "codex-run.exit").read_text()) == -9
    assert int((home / "codex-run.started").read_text()) < time.time_ns()
    if registration == "valid":
        assert ended - float((home / "registered").read_text()) < 2
        assert json.loads(result.stdout)["registration_to_exit_seconds"] < 2
    else:
        assert "Codex session registration missing" in result.stderr
    with pytest.raises(ProcessLookupError):
        os.kill(int((home / "pid").read_text()), 0)
