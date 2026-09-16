import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).parent.parent


def test_native_rate_limits_render_requested_banner(monkeypatch, tmp_path):
    import hooks.context.quota_usage as quota_usage

    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    now = time.time()
    quota_usage.record_rate_limits(
        "session-1",
        {
            "five_hour": {"used_percentage": 31, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 72, "resets_at": now + 86400},
        },
    )

    banner = quota_usage.quota_banner("session-1")

    assert "ATTENTION TO QOUTA USAGE" in banner
    assert "5H REMAINING: 69%" in banner
    assert "7D REMAINING: 28%" in banner
    assert "used" not in banner


def test_stale_native_snapshot_is_not_injected(monkeypatch, tmp_path):
    import hooks.context.quota_usage as quota_usage

    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(quota_usage, "QUOTA_USAGE_STALE_SEC", 300)
    path = quota_usage._snapshot_path("session-1")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "updated_at": time.time() - 301,
                "rate_limits": {"five_hour": {"used_percentage": 10}},
            }
        )
    )

    assert quota_usage.quota_banner("session-1") is None


def test_router_cache_supplies_first_prompt(monkeypatch, tmp_path):
    import hooks.context.quota_usage as quota_usage

    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setenv("AH_CC_TOKEN_ALPHA", "secret")
    cache = {
        "modes": {
            "normal": {
                "accounts": {
                    "AH_CC_TOKEN_ALPHA": {
                        "observed_at": time.time(),
                        "result": {
                            "five_hour": {"used": 12, "resets_at": time.time() + 1200},
                            "seven_day": {"used": 44, "resets_at": time.time() + 7200},
                        },
                    }
                }
            }
        }
    }
    (tmp_path / "claude-router-cache.json").write_text(json.dumps(cache))

    banner = quota_usage.quota_banner("new-session")

    assert "5H REMAINING: 88%" in banner
    assert "7D REMAINING: 56%" in banner
    assert "used" not in banner


def _hook_env(tmp_path):
    return {
        **os.environ,
        "AGENTIHOOKS_HOME": str(tmp_path),
        "AGENTIHOOKS_DISABLE_BYPASS_LOOKUP": "1",
        "BRAIN_ENABLED": "false",
        "BROADCAST_ENABLED": "false",
        "ENFORCEMENT_INJECTION_ENABLED": "false",
        "QUOTA_USAGE_INJECTION_ENABLED": "true",
        "QUOTA_USAGE_TOOL_CALLS": "5",
    }


def _run_hook(payload, env):
    return subprocess.run(
        [sys.executable, "-m", "hooks"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
    )


def test_statusline_snapshot_reaches_user_prompt_hook(tmp_path):
    env = _hook_env(tmp_path)
    now = time.time()
    statusline = subprocess.run(
        [sys.executable, "-m", "hooks.statusline"],
        input=json.dumps(
            {
                "session_id": "live-session",
                "rate_limits": {
                    "five_hour": {"used_percentage": 21, "resets_at": now + 1800},
                    "seven_day": {"used_percentage": 63, "resets_at": now + 7200},
                },
            }
        ),
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
    )
    assert statusline.returncode == 0

    result = _run_hook(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "live-session",
            "prompt": "continue",
            "cwd": str(_PROJECT_ROOT),
        },
        env,
    )

    assert result.returncode == 0
    assert "ATTENTION TO QOUTA USAGE" in result.stdout
    assert "5H REMAINING: 79%" in result.stdout
    assert "7D REMAINING: 37%" in result.stdout
    assert "used" not in result.stdout


def test_pretool_banner_fires_on_every_fifth_tool_call(tmp_path):
    env = _hook_env(tmp_path)
    now = time.time()
    quota_dir = tmp_path / "quota_usage"
    quota_dir.mkdir()
    (quota_dir / "live-session.json").write_text(
        json.dumps(
            {
                "updated_at": now,
                "rate_limits": {
                    "five_hour": {"used_percentage": 35, "resets_at": now + 1800},
                    "seven_day": {"used_percentage": 48, "resets_at": now + 7200},
                },
            }
        )
    )
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "live-session",
        "tool_name": "Bash",
        "tool_input": {"command": "echo quota-canary"},
        "cwd": str(_PROJECT_ROOT),
    }

    results = [_run_hook(payload, env) for _ in range(5)]

    assert all(result.returncode == 0 for result in results)
    assert all("ATTENTION TO QOUTA USAGE" not in result.stdout for result in results[:4])
    assert "ATTENTION TO QOUTA USAGE" in results[4].stdout
    assert "5H REMAINING: 65%" in results[4].stdout
    assert "7D REMAINING: 52%" in results[4].stdout
    assert "used" not in results[4].stdout
