"""The agentienv block in the shell rc must load under zsh as well as bash."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import install  # noqa: I001

FAKE_FIRST, FAKE_REST = "fake-first-line-0002", "fake-rest=line-0002"


def test_the_multiline_check_uses_no_bash_only_syntax():
    assert "compgen" not in install._AGENTIENV_MULTILINE
    assert "${!" not in install._AGENTIENV_MULTILINE


def test_zsh_sources_the_block_and_names_a_multiline_value(tmp_path):
    zsh = shutil.which("zsh")
    if zsh is None:
        if os.environ.get("CI"):
            pytest.fail("zsh is missing on this CI runner, so the zsh agentienv check cannot run")
        pytest.skip("zsh is not installed")
    bashrc = tmp_path / ".bashrc"
    env_file = tmp_path / ".agentihooks" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("GOOD_NAME_0002=ok\n")
    with patch.object(install, "_BASHRC", bashrc), patch.object(install, "_ENV_FILE_DST", env_file):
        install._update_bashrc_block()
    r = subprocess.run(
        [zsh, "-f", "-c", f'. "{bashrc}"; echo "good=$GOOD_NAME_0002"'],
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", ""),
            "FAKE_ZSH_0002": FAKE_FIRST + "\n" + FAKE_REST,
        },
        capture_output=True,
        text=True,
    )
    assert "good=ok" in r.stdout
    assert "command not found" not in r.stderr and "bad substitution" not in r.stderr, r.stderr
    assert "FAKE_ZSH_0002" in r.stderr
    assert FAKE_FIRST not in r.stdout + r.stderr and FAKE_REST not in r.stdout + r.stderr
