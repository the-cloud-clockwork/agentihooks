"""Presence over OpenTelemetry: every hook event marks its agent session seen and every hive interval marks the hive
seen with its agent processes by harness, as gauges the collector exposes to resource monitors. Seen gauges carry
Unix seconds, so a reader judges freshness from the value alone."""

import os
import socket
import time
from collections.abc import Mapping

from hooks.observability import otel

AGENT = "agentihooks.presence.agent"
HIVE = "agentihooks.presence.hive"
HIVE_AGENTS = "agentihooks.presence.hive.agents"


def hive_id(environ: Mapping[str, str]) -> str:
    """The hive this process runs on, resolved as the hive daemon resolves its own id."""
    explicit = environ.get("AGENTIHOOKS_HIVE_ID") or environ.get("SWARM_HIVE_ID")
    if explicit:
        return explicit
    from scripts.hive.registry import seeded_id

    return seeded_id() or socket.gethostname()


def agent(session_id: str, environ: Mapping[str, str], now_s: float) -> None:
    """Marks one agent session seen now with its agent, swarm, lane, task, hive, account and harness."""
    if not session_id:
        return
    from hooks.observability.agent_trace import identity_from_env

    identity = identity_from_env(session_id, environ)
    otel.record_gauge(
        AGENT,
        now_s,
        {
            "session.id": session_id,
            "agent": identity.agent,
            "swarm": identity.swarm,
            "lane": identity.lane,
            "task": identity.task,
            "account": identity.account,
            "hive": hive_id(environ),
            "harness": environ.get("AGENTIHOOKS_TARGET", "").strip().lower() or "claude",
        },
    )


def mark(payload: Mapping[str, object]) -> None:
    """Marks the session a hook payload names seen now, under this process's environment."""
    agent(str(payload.get("session_id") or ""), os.environ, time.time())


def hive(hive: str, name: str, headed: bool, release: str, agents: Mapping[str, int], now_s: float) -> None:
    """Marks one hive seen now and records its agent processes per harness under the same hive attributes."""
    attrs = {"hive.id": hive, "hive.name": name, "hive.headed": "yes" if headed else "no", "hive.release": release}
    otel.record_gauge(HIVE, now_s, attrs)
    for harness, count in agents.items():
        otel.record_gauge(HIVE_AGENTS, count, {**attrs, "harness": harness})
