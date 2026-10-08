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
    "stamp,shown",
    [
        ({"chain": ["engineer", "brain"], "overlays": ["brain"]}, "overlay:brain"),
        ({"chain": ["anton", "brain", "router"], "overlays": ["brain", "router"]}, "overlay:brain,router"),
        ({"chain": ["engineer"], "overlays": []}, "overlay:none"),
        ({"chain": ["engineer", "brain"]}, "overlay:none"),
        (None, "overlay:none"),
    ],
)
def test_statusline_names_the_overlays_rendered_into_the_home(monkeypatch, capsys, tmp_path, stamp, shown):
    from hooks import config, statusline

    if stamp is not None:
        (tmp_path / ".agentihooks-render.json").write_text(json.dumps(stamp))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "BASE_CHANNELS", ("brain", "amygdala"))
    monkeypatch.setattr(config, "BRAIN_ENABLED", True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "statusline-overlay"})))
    statusline.main()
    plain = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)

    assert f"  {shown}  channels:brain,amygdala" in plain


MODEL_PAYLOAD = {
    "session_id": "statusline-model",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
    "effort": {"level": "high"},
}


@pytest.mark.parametrize("effort, shown", [({"level": "high"}, "| Opus 5.5 high\n"), (None, "| Opus 5.5\n")])
def test_statusline_shows_the_effort_beside_the_model(monkeypatch, capsys, effort, shown):
    from hooks import statusline

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({**MODEL_PAYLOAD, "effort": effort})))
    statusline.main()
    plain = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)

    assert shown in plain


@pytest.mark.parametrize(
    "model, effort, reported",
    [
        ({"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, {"level": "high"}, ("claude-opus-5-5", "high")),
        ({"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, None, ("claude-opus-5-5", None)),
        ({}, {"level": "high"}, (None, "high")),
    ],
)
def test_the_statusline_reports_the_live_model_and_effort_to_the_swarm(monkeypatch, capsys, model, effort, reported):
    from hooks import statusline
    from hooks.context import swarm_heartbeat

    seen = []
    monkeypatch.setattr(swarm_heartbeat, "report", lambda *args: seen.append(args))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({**MODEL_PAYLOAD, "model": model, "effort": effort})))
    statusline.main()

    assert seen == [reported]


@pytest.mark.parametrize("branch", ["coverage-branch", ""])
def test_statusline_branch_display_ignores_working_directory(monkeypatch, tmp_path, capsys, branch):
    from hooks import statusline

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(statusline, "_git_branch", lambda: branch)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    statusline.main()

    output = capsys.readouterr().out
    if branch:
        assert f"{statusline._MAGENTA}{branch}{statusline._RESET}\n" in output
    else:
        assert "coverage-branch" not in output
