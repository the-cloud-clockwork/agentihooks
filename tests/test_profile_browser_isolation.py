import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("order", ["alone", "reconnect", "detached-client"])
def test_cached_browser_session_is_independent_of_mcp_import_state(tmp_path, order):
    cached = "tests/test_profile_browser.py::test_a_session_that_only_lists_cached_tools_never_launches_the_browser"
    reconnect = "tests/inbox/test_channel_reconnect.py::test_channel_reconnect_backoff_caps_at_thirty_seconds"
    nodes = [reconnect, cached] if order == "reconnect" else [cached]
    prelude = ""
    if order == "detached-client":
        prelude = "import mcp.client.stdio\ndel mcp.client\n"
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ | {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            prelude + "import pytest, sys\nsys.exit(pytest.main(sys.argv[1:]))",
            *nodes,
            "-n",
            "0",
            "-v",
            "--tb=short",
            "-p",
            "no:cacheprovider",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
