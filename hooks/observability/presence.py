"""Presence over OpenTelemetry: every hook event marks its agent session seen in a seen record that the hive daemon
exports, and every hive interval marks the hive seen with its agent processes by harness, as gauges the collector
exposes to resource monitors. A host with no live hive daemon export sends the agent gauge from the hook, at most once
per interval per session. Seen gauges carry Unix seconds, so a reader judges freshness from the value alone."""

import json
import os
import socket
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from hooks.observability import otel

AGENT = "agentihooks.presence.agent"
HIVE = "agentihooks.presence.hive"
HIVE_AGENTS = "agentihooks.presence.hive.agents"
INTERVAL_S = 60
FRESH_S = 180
DAEMON_LIVE_S = 30
PRUNE_S = 86_400
BEAT = "daemon.beat"


def hive_id(environ: Mapping[str, str]) -> str:
    """The hive this process runs on, resolved as the hive daemon resolves its own id."""
    explicit = environ.get("AGENTIHOOKS_HIVE_ID") or environ.get("SWARM_HIVE_ID")
    if explicit:
        return explicit
    from scripts.hive.registry import seeded_id

    return seeded_id() or socket.gethostname()


def seen_dir() -> Path:
    """The folder holding one seen record per agent session and the beat of the hive daemon that exports them."""
    from hooks import config

    return config.AGENTIHOOKS_HOME / "telemetry" / "presence"


def daemon_live(now_s: float) -> bool:
    """True when a hive daemon on this home exported the seen records within its live window."""
    beat_s = _read(seen_dir() / BEAT).get("beat_s")
    return isinstance(beat_s, int | float) and now_s - beat_s <= DAEMON_LIVE_S


def agent(session_id: str, environ: Mapping[str, str], now_s: float) -> None:
    """Marks one agent session seen now with its agent, swarm, lane, task, hive, account and harness in its seen
    record, and sends the seen gauge itself only while no hive daemon exports this home, at most once per interval."""
    if not session_id:
        return
    from hooks.observability.agent_trace import identity_from_env
    from hooks.observability.signals import safe_name

    identity = identity_from_env(session_id, environ)
    attributes = {
        "session.id": session_id,
        "agent": identity.agent,
        "swarm": identity.swarm,
        "lane": identity.lane,
        "task": identity.task,
        "account": identity.account,
        "hive": hive_id(environ),
        "harness": environ.get("AGENTIHOOKS_TARGET", "").strip().lower() or "claude",
    }
    path = seen_dir() / f"{safe_name(session_id)}.json"
    record = _read(path)
    sent_s = record.get("sent_s")
    due = not isinstance(sent_s, int | float) or now_s - sent_s >= INTERVAL_S
    if due and not daemon_live(now_s):
        otel.record_gauge(AGENT, now_s, attributes)
        sent_s = now_s
    _write(path, {**record, "attributes": attributes, "seen_s": now_s, "sent_s": sent_s})


def mark(payload: Mapping[str, object]) -> None:
    """Marks the session a hook payload names seen now, under this process's environment; never raises."""
    try:
        agent(str(payload.get("session_id") or ""), os.environ, time.time())
    except (OSError, ValueError):
        return


def hive(hive: str, name: str, headed: bool, release: str, agents: Mapping[str, int], now_s: float) -> None:
    """Marks one hive seen now and records its agent processes per harness under the same hive attributes."""
    attrs = {"hive.id": hive, "hive.name": name, "hive.headed": "yes" if headed else "no", "hive.release": release}
    otel.record_gauge(HIVE, now_s, attrs)
    for harness, count in agents.items():
        otel.record_gauge(HIVE_AGENTS, count, {**attrs, "harness": harness})


def export_agents(now_s: float) -> None:
    """Sends the seen gauge for every agent session seen within the fresh window with its recorded seen seconds and
    attributes, removes records seen over a day ago or unreadable, then beats so hooks on this home stop sending."""
    folder = seen_dir()
    for path in sorted(folder.glob("*.json")):
        record = _read(path)
        seen_s = record.get("seen_s")
        seen = seen_s if isinstance(seen_s, int | float) else 0.0
        attributes = record.get("attributes")
        if now_s - seen > PRUNE_S:
            path.unlink()
        elif now_s - seen <= FRESH_S and isinstance(attributes, dict):
            otel.record_gauge(AGENT, seen, cast(dict[str, str], attributes))
    _write(folder / BEAT, {"beat_s": now_s})


def _read(path: Path) -> dict[str, object]:
    try:
        loaded: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}


def _write(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(handle, "w") as stream:
        json.dump(record, stream)
    os.replace(temporary, path)
