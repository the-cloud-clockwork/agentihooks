import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).parent.parent


def test_native_rate_limits_render_requested_banner(monkeypatch, tmp_path):
    import hooks.context.quota_usage as quota_usage

    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(quota_usage, "QUOTA_USAGE_TOOL_CALLS", 5)
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
    assert "Shown every 5 tool calls" in banner
    assert "used" not in banner


def test_banner_renders_requested_cadence_after_injection_tests_patch_it(tmp_path):
    leaker = "tests/context/test_injection_kinds.py::test_tool_call_blocks_rank_by_kind"
    banner_test = f"tests/test_quota_usage.py::{test_native_rate_limits_render_requested_banner.__name__}"
    script = (
        "import sys, pytest\n"
        "args = ['-q', '-p', 'no:xdist', '-p', 'no:cacheprovider']\n"
        "codes = [int(pytest.main([*args, node])) for node in sys.argv[1:]]\n"
        "print('CODES', codes)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"} | {"AGENTIHOOKS_HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", script, leaker, banner_test],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert "CODES [0, 0]" in result.stdout, result.stdout[-3000:] + result.stderr[-2000:]


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
    monkeypatch.setattr(quota_usage, "QUOTA_USAGE_STALE_SEC", 300)
    for name in [name for name in os.environ if name.startswith("AH_CC_TOKEN_")]:
        monkeypatch.delenv(name)
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


_REPEAT_HOOK = """
import io, json, os, sys
from hooks.hook_manager import main

class Exit(BaseException):
    pass

def fake_exit(code):
    raise Exit(code)

os._exit = fake_exit
raw, times = sys.stdin.read(), int(sys.argv[1])
out, results = sys.stdout, []
for _ in range(times):
    sys.stdin, sys.stdout = io.StringIO(raw), io.StringIO()
    try:
        main()
    except Exit as done:
        results.append({"returncode": done.args[0], "stdout": sys.stdout.getvalue()})
out.write(json.dumps(results))
"""


def _run_hook_times(payload, env, times):
    proc = subprocess.run(
        [sys.executable, "-c", _REPEAT_HOOK, str(times)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
        check=True,
    )
    return [SimpleNamespace(**result) for result in json.loads(proc.stdout)]


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
    assert "Shown every 5 tool calls" in result.stdout
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

    results = _run_hook_times(payload, env, 5)

    assert len(results) == 5

    assert all(result.returncode == 0 for result in results)
    assert all("ATTENTION TO QOUTA USAGE" not in result.stdout for result in results[:4])
    assert "ATTENTION TO QOUTA USAGE" in results[4].stdout
    assert "5H REMAINING: 65%" in results[4].stdout
    assert "7D REMAINING: 52%" in results[4].stdout
    assert "Shown every 5 tool calls" in results[4].stdout
    assert "used" not in results[4].stdout


def _limits(five: float, week: float) -> dict:
    return {
        "five_hour": {"used_percentage": five, "resets_at": 5_000},
        "seven_day": {"used_percentage": week, "resets_at": 9_000},
    }


def test_an_interactive_session_snapshot_names_its_account(monkeypatch, tmp_path):
    import hooks.context.quota_usage as quota_usage

    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    quota_usage.record_rate_limits("one", _limits(10, 20), {"AH_ROUTE_INTERACTIVE_main": "1"})
    quota_usage.record_rate_limits("two", _limits(10, 20), {"AH_ROUTE_INTERACTIVE_": "1", "AH_CC_TOKEN_main": "t"})
    quota_usage.record_rate_limits(
        "three", _limits(10, 20), {"AH_ROUTE_INTERACTIVE_a": "1", "AH_ROUTE_INTERACTIVE_b": "1"}
    )
    quota_usage.record_rate_limits("four", _limits(10, 20), {"AH_ROUTE_INTERACTIVE_main": ""})
    saved = {path.stem: json.loads(path.read_text()) for path in (tmp_path / "quota_usage").glob("*.json")}
    assert saved["one"]["account"] == "main"
    assert ["account" in saved[name] for name in ("two", "three", "four")] == [False, False, False]


def test_account_snapshot_is_the_freshest_fresh_snapshot_of_that_account(monkeypatch, tmp_path):
    import hooks.context.quota_usage as quota_usage

    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(quota_usage, "QUOTA_USAGE_STALE_SEC", 300)
    for session, at, limits, environ in (
        ("old", 1_000.0, _limits(1, 2), {"AH_ROUTE_INTERACTIVE_main": "1"}),
        ("new", 1_200.0, _limits(30, 40), {"AH_ROUTE_INTERACTIVE_main": "1"}),
        ("other", 1_250.0, _limits(50, 60), {"AH_ROUTE_INTERACTIVE_spare": "1"}),
        ("plain", 1_260.0, _limits(70, 80), {}),
    ):
        monkeypatch.setattr(quota_usage.time, "time", lambda at=at: at)
        quota_usage.record_rate_limits(session, limits, environ)
    folder = tmp_path / "quota_usage"
    for name, text in (
        ("broken", "{"),
        ("listed", "[]"),
        ("empty", "{}"),
        ("unset", '{"updated_at": null, "account": "main", "rate_limits": {}}'),
        ("shape", '{"updated_at": 1400, "account": "main", "rate_limits": []}'),
    ):
        (folder / f"{name}.json").write_text(text, encoding="utf-8")
    (folder / "folder.json").mkdir()
    expected = {
        name: {**window, "used_percentage": float(window["used_percentage"])}
        for name, window in _limits(30, 40).items()
    }
    assert quota_usage.account_snapshot("main", 1_500.0) == (1_200.0, expected)
    assert quota_usage.account_snapshot("main", 1_501.0) is None
    assert quota_usage.account_snapshot("spare", 1_300.0)[0] == 1_250.0
    assert quota_usage.account_snapshot("absent", 1_300.0) is None


def test_account_snapshot_picks_one_of_two_snapshots_written_at_the_same_second(monkeypatch, tmp_path):
    import hooks.context.quota_usage as quota_usage

    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(quota_usage.time, "time", lambda: 1_000.0)
    quota_usage.record_rate_limits("left", _limits(10, 20), {"AH_ROUTE_INTERACTIVE_main": "1"})
    quota_usage.record_rate_limits("right", _limits(30, 40), {"AH_ROUTE_INTERACTIVE_main": "1"})
    at, limits = quota_usage.account_snapshot("main", 1_000.0)
    assert at == 1_000.0
    assert limits["seven_day"]["used_percentage"] in (20.0, 40.0)


def test_latest_snapshot_is_the_newest_of_that_account_however_old(monkeypatch, tmp_path):
    import hooks.context.quota_usage as quota_usage

    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    for session, at, limits in (
        ("old", 10.0, _limits(1, 2)),
        ("left", 20.0, _limits(10, 20)),
        ("right", 20.0, _limits(30, 40)),
    ):
        monkeypatch.setattr(quota_usage.time, "time", lambda at=at: at)
        quota_usage.record_rate_limits(session, limits, {"AH_ROUTE_INTERACTIVE_main": "1"})
    at, limits = quota_usage.latest_snapshot("main")
    assert at == 20.0
    assert limits["seven_day"]["used_percentage"] in (20.0, 40.0)
    assert quota_usage.latest_snapshot("absent") is None


def test_the_statusline_snapshot_reads_the_session_environment(monkeypatch, tmp_path):
    import hooks.context.quota_usage as quota_usage

    monkeypatch.setattr(quota_usage, "AGENTIHOOKS_HOME", tmp_path)
    for name in [name for name in os.environ if name.startswith("AH_ROUTE_INTERACTIVE_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AH_ROUTE_INTERACTIVE_main", "1")
    quota_usage.record_rate_limits("live", _limits(10, 20))
    assert json.loads((tmp_path / "quota_usage" / "live.json").read_text())["account"] == "main"
