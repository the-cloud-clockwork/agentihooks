import io
import json
import os
import re
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
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)

    assert "session:" in output
    assert "weekly:" in output
    assert "account:" in output
    assert "alpha" in output
    assert "weekly:27% | account:alpha" in plain
    assert "  |  " not in plain


def test_statusline_shows_default_for_direct_authentication():
    output = _run_statusline()

    assert "account:" in output
    assert "default" in output


def test_statusline_names_selected_run_profile(monkeypatch, capsys):
    from hooks import statusline

    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "zz-selected")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "statusline-profile"})))
    statusline.main()
    plain = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)

    assert "agentihooks: zz-selected  settings:zz-selected" in plain


@pytest.mark.parametrize(
    "channels,enabled,shown",
    [
        (("brain", "amygdala"), True, "overlay:brain"),
        (("amygdala",), True, "overlay:none"),
        (("brain",), False, "overlay:none"),
    ],
)
def test_statusline_names_the_brain_overlay(monkeypatch, capsys, channels, enabled, shown):
    from hooks import config, statusline

    monkeypatch.setattr(config, "BASE_CHANNELS", channels)
    monkeypatch.setattr(config, "BRAIN_ENABLED", enabled)
    monkeypatch.setattr(config, "BRAIN_CHANNEL", "brain")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "statusline-brain"})))
    statusline.main()
    plain = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)

    assert f"  {shown}  channels:{','.join(channels)}" in plain
