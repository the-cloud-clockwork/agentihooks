import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from hooks import config
from hooks.observability import otel, presence
from scripts.hive import daemon

ENV = {
    "AGENTIHOOKS_AGENT_NAME": "engineer@1-2",
    "AGENTIHOOKS_SWARM": "rig",
    "AGENTIHOOKS_SWARM_LANE": "eng",
    "AGENTIHOOKS_SWARM_TASK": "t1",
    "AGENTIHOOKS_HIVE_ID": "hive-003",
    "AH_CC_TOKEN_work": "x",
}
ATTRS = {
    "session.id": "sess-1",
    "agent": "engineer@1-2",
    "swarm": "rig",
    "lane": "eng",
    "task": "t1",
    "account": "work",
    "hive": "hive-003",
    "harness": "claude",
}


def emitted(monkeypatch, shown):
    calls = []
    redis: Any = object()

    def show(client, hive):
        assert client is redis and hive == "hive-9"
        return shown

    monkeypatch.setattr(daemon.registry, "show", show)
    monkeypatch.setattr(daemon.account_sessions, "local_processes", lambda: {"claude": {1: "a", 2: "b"}, "codex": {}})
    monkeypatch.setattr(daemon.updater, "installed_version", lambda: "7.7.7")
    monkeypatch.setattr(presence, "hive", lambda *args: calls.append(args))
    daemon.emit_presence(redis, "hive-9", 12_500)
    return calls


def test_a_headed_hive_emits_its_registered_name_release_and_agents(monkeypatch):
    calls = emitted(monkeypatch, {"name": "automation", "ui": "yes"})
    assert calls == [("hive-9", "automation", True, "7.7.7", {"claude": 2, "codex": 0}, 12.5)]


def test_an_unregistered_hive_emits_under_its_id_as_headless(monkeypatch):
    calls = emitted(monkeypatch, None)
    assert calls == [("hive-9", "hive-9", False, "7.7.7", {"claude": 2, "codex": 0}, 12.5)]


def recorded(monkeypatch):
    calls = []
    monkeypatch.setattr(otel, "record_gauge", lambda name, value, attrs: calls.append((name, value, attrs)))
    return calls


def daemon_pass(monkeypatch, now_ms):
    redis: Any = object()
    monkeypatch.setattr(daemon.registry, "show", lambda client, hive: None)
    monkeypatch.setattr(daemon.account_sessions, "local_processes", lambda: {})
    monkeypatch.setattr(daemon.updater, "installed_version", lambda: "7.7.7")
    monkeypatch.setattr(presence, "hive", lambda *args: None)
    daemon.emit_presence(redis, "hive-9", now_ms)


def seen_folder():
    return config.AGENTIHOOKS_HOME / "telemetry" / "presence"


@pytest.mark.feature("presence-agent-hive-daemon")
def test_a_daemon_pass_sends_each_fresh_session_seen_gauge_with_its_attributes_and_last_seen_seconds(monkeypatch):
    calls = recorded(monkeypatch)
    presence.agent("sess-1", ENV, 1_000.0)
    presence.agent("sess-1", ENV, 1_001.0)
    calls.clear()
    daemon_pass(monkeypatch, 1_181_000)
    assert calls == [("agentihooks.presence.agent", 1_001.0, ATTRS)]


@pytest.mark.feature("presence-agent-hive-daemon")
def test_a_daemon_pass_skips_a_stale_session_and_removes_records_seen_over_a_day_ago_or_unreadable(monkeypatch):
    calls = recorded(monkeypatch)
    presence.agent("sess-stale", ENV, 100_000.0)
    presence.agent("sess-kept", ENV, 13_781.0)
    presence.agent("sess-gone", ENV, 13_780.0)
    (seen_folder() / "sess-bad.json").write_text("{")
    (seen_folder() / "sess-list.json").write_text("[]")
    (seen_folder() / "sess-bare.json").write_text(json.dumps({"seen_s": 100_100.0}))
    calls.clear()
    daemon_pass(monkeypatch, 100_181_000)
    assert calls == []
    assert sorted(path.name for path in seen_folder().glob("*.json")) == [
        "sess-bare.json",
        "sess-kept.json",
        "sess-stale.json",
    ]


@pytest.mark.feature("presence-agent-hive-daemon")
def test_a_session_sends_its_own_seen_gauge_only_once_the_daemon_beat_is_older_than_its_live_window(monkeypatch):
    calls = recorded(monkeypatch)
    daemon_pass(monkeypatch, 1_000_000)
    presence.agent("sess-1", ENV, 1_030.0)
    presence.agent("sess-1", ENV, 1_031.0)
    assert calls == [("agentihooks.presence.agent", 1_031.0, ATTRS)]


@pytest.mark.feature("presence-agent-hive-daemon")
def test_a_dispatcher_mark_after_a_daemon_pass_never_starts_the_otel_worker_or_sdk(monkeypatch):
    daemon_pass(monkeypatch, int(time.time() * 1000))
    probe = (
        "import json, threading\n"
        "from hooks.observability import otel, presence\n"
        "presence.mark({'session_id': 'sess-hook'})\n"
        "worker = any(thread.name == 'otel-worker' for thread in threading.enumerate())\n"
        "print(json.dumps({'initialized': otel._initialized, 'worker': worker}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, **ENV, "AGENTIHOOKS_HOME": str(config.AGENTIHOOKS_HOME)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert json.loads(done.stdout.splitlines()[-1]) == {"initialized": False, "worker": False}
    assert (seen_folder() / "daemon.beat").exists()
    assert (seen_folder() / "sess-hook.json").exists()
