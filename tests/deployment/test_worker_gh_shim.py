import os
import site
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_gh_shim_without_hive_auth_keeps_workstation_login(tmp_path):
    gh = tmp_path / "gh"
    gh.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        '[[ -z "${GH_TOKEN+x}" && -z "${GITHUB_TOKEN+x}" ]]\n'
        '[[ "$GH_CONFIG_DIR" == "$EXPECTED_CONFIG" ]]\n'
        'printf "%s\\n" "$*"\n'
    )
    gh.chmod(0o755)
    shim = tmp_path / "shim"
    shim.write_text((ROOT / "deploy" / "worker" / "gh").read_text().replace("/usr/bin/gh", str(gh)))
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"HIVE_GITHUB_AUTH_MODE", "GH_TOKEN", "GITHUB_TOKEN"}
    }
    env.update(
        AGENTIHOOKS_HOME=str(tmp_path / "agentihooks"),
        GH_CONFIG_DIR=str(tmp_path / "gh-config"),
        EXPECTED_CONFIG=str(tmp_path / "gh-config"),
    )
    result = subprocess.run(
        ["bash", str(shim), "api", "user"], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "api user\n"


@pytest.mark.parametrize("foreign_scripts", [False, True])
def test_gh_shim_hands_gh_the_renewed_token_file_over_an_expired_launch_token(tmp_path, foreign_scripts):
    receipt = tmp_path / "receipt"
    gh = tmp_path / "gh"
    gh.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$GH_TOKEN $GITHUB_TOKEN $*" >> {receipt}\n')
    gh.chmod(0o755)
    shim = tmp_path / "shim"
    shim.write_text(
        (ROOT / "deploy" / "worker" / "gh")
        .read_text()
        .replace("/opt/venv/bin/python3", f"{sys.executable} -S")
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
        "PYTHONPATH": os.pathsep.join((str(ROOT), *site.getsitepackages())),
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            'bash "$1" pr list && printf "renewed\\n" > "$2.next" && mv "$2.next" "$2" && bash "$1" api user',
            "parent",
            str(shim),
            str(home / "github-token"),
        ],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert receipt.read_text().splitlines() == ["fresh fresh pr list", "renewed renewed api user"]
