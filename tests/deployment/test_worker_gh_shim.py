import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("foreign_scripts", [False, True])
def test_gh_shim_hands_gh_the_renewed_token_file_over_an_expired_launch_token(tmp_path, foreign_scripts):
    receipt = tmp_path / "receipt"
    gh = tmp_path / "gh"
    gh.write_text(f'#!/usr/bin/env bash\nprintf %s "$GH_TOKEN $GITHUB_TOKEN $*" > {receipt}\n')
    gh.chmod(0o755)
    shim = tmp_path / "shim"
    shim.write_text(
        (ROOT / "deploy" / "worker" / "gh")
        .read_text()
        .replace("/opt/venv/bin/python3", sys.executable)
        .replace("/usr/bin/gh", str(gh))
    )
    home = tmp_path / "agentihooks"
    home.mkdir()
    (home / "github-token").write_text("fresh\n")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    if foreign_scripts:
        (checkout / "scripts").mkdir()
        (checkout / "scripts" / "__init__.py").write_text("")
    env = {
        **{k: v for k, v in os.environ.items() if k != "GITHUB_APP_PRIVATE_KEY"},
        "HIVE_GITHUB_AUTH_MODE": "app",
        "AGENTIHOOKS_HOME": str(home),
        "GH_TOKEN": "expired",
        "GITHUB_TOKEN": "expired",
        "PYTHONPATH": os.pathsep.join((str(ROOT), os.environ.get("PYTHONPATH", ""))),
    }
    result = subprocess.run(
        ["bash", str(shim), "pr", "list"], cwd=checkout, env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert receipt.read_text() == "fresh fresh pr list"
