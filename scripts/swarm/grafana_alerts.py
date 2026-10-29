"""Grafana swarm bottleneck alert state history in, standing stage findings out, read through the gateway Grafana tools.

Only numbers and known names are taken from an alert; its free text never reaches the swarm.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts.swarm.health import standing
from scripts.swarm.langfuse_read import gateway_client

if TYPE_CHECKING:
    from redis import Redis

URL_ENV = "LANGFUSE_MCP_URL"
TOOL = "grafana-get_annotations"
TIMEOUT_S = 10
WINDOW_MS = 24 * 3_600_000
LIMIT = 1000
UNIT = "task hours in the window"
STAGES = (
    "seat_wait",
    "review",
    "merge_queue",
    "intent_fix",
    "launch_failure",
    "ci_queue",
    "quota_hold",
    "host_hold",
)
LABELS = re.compile(r"\{([^{}]*)\}")


async def _annotations(since_ms: int, timeout: float) -> list:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {
        "Authorization": f"Bearer {os.environ['MCP_KEY_GATEWAY']}",
        "Host": "llm.homeofanton.com",
        "x-mcp-servers": "grafana",
    }
    async with asyncio.timeout(timeout):
        async with streamablehttp_client(
            os.environ[URL_ENV], headers=headers, timeout=timeout, httpx_client_factory=gateway_client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(TOOL, {"Type": "alert", "From": since_ms, "Limit": LIMIT})
                if result.isError:
                    raise RuntimeError("Grafana annotations tool failed")
                body = json.loads(next(item.text for item in result.content if item.type == "text"))
                return (body.get("Payload") or []) if isinstance(body, dict) else []


def read_annotations(now_ms: int, timeout: float = TIMEOUT_S) -> list:
    return asyncio.run(_annotations(now_ms - WINDOW_MS, timeout))


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _labels(text) -> dict:
    found = LABELS.search(str(text))
    return dict(part.partition("=")[::2] for part in found.group(1).split(", ")) if found else {}


def _order(note: dict) -> tuple:
    return _number(note.get("time")), _number(note.get("id"))


def _latest(annotations: list, slug: str) -> dict:
    notes = [note for note in annotations if isinstance(note, dict) and None not in _order(note)]
    latest = {}
    for note in sorted(notes, key=_order):
        labels = _labels(note.get("text"))
        if labels.get("swarm") == "bottleneck" and labels.get("ledger") == slug and labels.get("stage") in STAGES:
            latest[labels["stage"]] = (note, labels)
    return latest


def _reading(note: dict, labels: dict):
    data = note.get("data") if isinstance(note.get("data"), dict) else {}
    values = data.get("values") if isinstance(data.get("values"), dict) else {}
    hours, threshold = _number(values.get("hours")), _number(labels.get("threshold_hours"))
    if hours is None or threshold is None:
        return None
    episode = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_number(note["time"]) / 1000))
    return standing.Reading(hours, threshold, UNIT, episode)


def apply(redis: Redis, slug: str, annotations: list, now_ms: int) -> list[str]:
    actions = []
    for stage, (note, labels) in _latest(annotations, slug).items():
        state = str(note.get("newState"))
        reading = _reading(note, labels) if state.startswith("Alerting") else None
        if reading:
            standing.fire(redis, slug, stage, reading, now_ms)
            actions.append(f"fired {stage} on {slug}")
        elif state.startswith("Normal"):
            row = standing.read(redis, slug).get(stage)
            if row and row.get("unit") == UNIT and standing.clear(redis, slug, stage):
                actions.append(f"cleared {stage} on {slug}")
    return actions


def refresh(redis: Redis, slug: str, now_ms: int, read: Callable[[int], list] = read_annotations) -> list[str]:
    if not (os.environ.get(URL_ENV) and os.environ.get("MCP_KEY_GATEWAY")):
        return []
    try:
        annotations = read(now_ms)
    except Exception:
        return []
    return apply(redis, slug, annotations, now_ms)
