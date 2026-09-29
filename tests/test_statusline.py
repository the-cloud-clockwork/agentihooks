import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).parent.parent


def _run_statusline(account: str = "") -> str:
    env = dict(os.environ)
    env.pop("AGENTIHOOKS_ROUTE_ACCOUNT", None)
    if account:
        env["AGENTIHOOKS_ROUTE_ACCOUNT"] = account
    result = subprocess.run(
        [sys.executable, "-m", "hooks.statusline"],
        input=json.dumps(
            {
                "session_id": "statusline-account",
                "rate_limits": {
                    "five_hour": {"used_percentage": 19},
                    "seven_day": {"used_percentage": 27},
                },
            }
        ),
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
    )
    assert result.returncode == 0
    return result.stdout


def test_statusline_shows_routed_account_slug():
    output = _run_statusline("alpha")

    assert "session:" in output
    assert "weekly:" in output
    assert "account:" in output
    assert "alpha" in output


def test_statusline_shows_default_for_direct_authentication():
    output = _run_statusline()

    assert "account:" in output
    assert "default" in output
