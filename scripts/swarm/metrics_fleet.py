"""The fleet dashboard sample, read each tick from the sources this tick can reach, exported as panel series."""

import json
from collections.abc import Callable
from pathlib import Path

from hooks.context import project_cache
from scripts.swarm import metrics_outbox
from scripts.swarm.store import RedisStore
from scripts.swarm_v2 import fleet_dashboard, quota
from scripts.swarm_v2.api.health import Health

PANELS = metrics_outbox.Table(
    "fleet_panels",
    (("panel", "String"), ("series", "String"), ("subject", "String"), ("value", "Float64")),
)
SOURCES = metrics_outbox.Table("fleet_sources", (("source", "String"), ("available", "Int64"), ("readings", "Int64")))
UNREAD = ("pools", "checkpoints", "brain_denials")
VIEWER = fleet_dashboard.Viewer("metrics", frozenset())


def feed_path() -> Path:
    return project_cache.feed_file()


def feed_at_ms() -> int:
    path = feed_path()
    return int(path.stat().st_mtime * 1000) if path.exists() else 0


def _attempts(store: RedisStore, slug: str) -> tuple[list, list]:
    beats = {key: json.loads(raw) for key, raw in store.redis.hgetall(store.key(slug, "heartbeats")).items()}
    active = [
        {
            "execution_id": agent.execution_id,
            "seat": seat,
            "task": agent.task,
            "phase": fleet_dashboard.ACTIVE,
            "terminal": "attached" if agent.pane_id else "detached",
            "authority": beats[agent.execution_id]["state"] if agent.execution_id in beats else "local",
        }
        for seat, agent in sorted(store.execution_occupants(slug).items())
    ]
    pending = [
        {
            "execution_id": "",
            "seat": "",
            "task": task,
            "phase": fleet_dashboard.PENDING,
            "requested": json.loads(raw)["resources"],
            "terminal": "detached",
            "authority": "pending",
        }
        for task, raw in sorted(store.redis.hgetall(store.key(slug, "pending-admission")).items())
    ]
    return active, pending


def _quota(store: RedisStore, now_ms: int) -> list[dict]:
    found = []
    for harness in sorted(quota.HARNESSES):
        for account in sorted(quota.latest_all(store.redis, harness)):
            until = int(store.redis.get(quota.wait_key(account, harness)) or now_ms)
            waiting = until > now_ms
            found.append(
                {
                    "account": account,
                    "state": "waiting" if waiting else "allowed",
                    "since_ms": until - quota.WAIT_MS if waiting else now_ms,
                    "resets_at_ms": until,
                }
            )
    return found


def sample(store: RedisStore, slug: str, now_ms: int, feed_at: int) -> dict:
    active, pending = _attempts(store, slug)
    archive = Health(store, slug, clock=lambda: now_ms / 1000).diagnose()["pipeline"]["archive"]
    return {
        "seq": now_ms,
        "at_ms": now_ms,
        "pools": [],
        "attempts": active + pending,
        "quota": _quota(store, now_ms),
        "archive": archive,
        "checkpoints": [],
        "brain": {"feed_at_ms": feed_at, "denials": []},
    }


def _values(panels: dict) -> list[tuple[str, str, str, float]]:
    found = []
    for panel, series in panels.items():
        if not isinstance(series, dict):
            continue
        for key, item in series.items():
            value = item["value"] if isinstance(item, dict) else item
            if isinstance(value, dict):
                found += [(panel, key, subject, float(count)) for subject, count in value.items()]
            elif isinstance(value, bool | int | float):
                found.append((panel, key, "", float(value)))
    return found


def panel_rows(slug: str, now_ms: int, panels: dict, fresh_feed: bool) -> list[dict]:
    return [
        {
            "event_id": f"fleet:{slug}:{now_ms}:{panel}:{series}:{subject}",
            "ledger": slug,
            "ts_ms": now_ms,
            **dict.fromkeys(("plan", "phase", "slice", "task"), ""),
            "panel": panel,
            "series": series,
            "subject": subject,
            "value": value,
        }
        for panel, series, subject, value in _values(panels)
        if fresh_feed or panel != "knowledge" or series == "cross_scope_denials"
    ]


def source_rows(slug: str, now_ms: int, found: dict) -> list[dict]:
    active = [row for row in found["attempts"] if row["phase"] == fleet_dashboard.ACTIVE]
    counts = {
        "executions": (1, len(active)),
        "pending_admissions": (1, len(found["attempts"]) - len(active)),
        "quota": (1, len(found["quota"])),
        "archive": (1, len(found["archive"])),
        "brain_feed": (1, 1) if found["brain"]["feed_at_ms"] else (0, 0),
        **dict.fromkeys(UNREAD, (0, 0)),
    }
    return [
        {
            "event_id": f"fleet-source:{slug}:{now_ms}:{source}",
            "ledger": slug,
            "ts_ms": now_ms,
            **dict.fromkeys(("plan", "phase", "slice", "task"), ""),
            "source": source,
            "available": available,
            "readings": readings,
        }
        for source, (available, readings) in counts.items()
    ]


def record(
    box: metrics_outbox.Outbox, store: RedisStore, slug: str, now_ms: int, feed: Callable[[], int] = feed_at_ms
) -> dict:
    found = sample(store, slug, now_ms, feed())
    panels = fleet_dashboard.dashboard(found, VIEWER, fleet_dashboard.definition())["panels"]
    box.append(PANELS, panel_rows(slug, now_ms, panels, bool(found["brain"]["feed_at_ms"])))
    box.append(SOURCES, source_rows(slug, now_ms, found))
    return found
