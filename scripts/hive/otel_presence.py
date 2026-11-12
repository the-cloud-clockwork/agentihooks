"""Hive presence read back from the collector's Prometheus exposition: each hive seen within FRESH_S with its name,
headed flag, release and agent processes by harness."""

import re
import time
import urllib.request
from collections.abc import Callable, Mapping
from typing import TypedDict

URL_ENV = "AGENTIHOOKS_PRESENCE_URL"
FRESH_S = 180
TIMEOUT_S = 3
CACHE_S = 5
HIVE = "agentihooks_presence_hive"
HIVE_AGENTS = "agentihooks_presence_hive_agents"
SAMPLE = re.compile(r"(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)\{(?P<labels>[^}]*)\}\s+(?P<value>\S+)(?:\s+\d+)?")
LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')
_cache: dict[str, tuple[float, str | None]] = {}


class Hive(TypedDict):
    id: str
    name: str
    headed: bool
    release: str
    agents: dict[str, int]


def samples(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Every labelled sample in a Prometheus text exposition, skipping comments and unparsable values."""
    found: list[tuple[str, dict[str, str], float]] = []
    for line in text.splitlines():
        match = SAMPLE.fullmatch(line.strip())
        if match is None:
            continue
        try:
            value = float(match["value"])
        except ValueError:
            continue
        labels: dict[str, str] = dict(LABEL.findall(match["labels"]))
        found.append((match["name"], labels, value))
    return found


def hives(text: str, now_s: float) -> list[Hive]:
    """One record per hive whose seen gauge is within FRESH_S of now, with its agents summed per harness."""
    parsed = samples(text)
    fresh: dict[str, Hive] = {
        labels["hive_id"]: Hive(
            id=labels["hive_id"],
            name=labels.get("hive_name") or labels["hive_id"],
            headed=labels.get("hive_headed") == "yes",
            release=labels.get("hive_release", ""),
            agents={},
        )
        for name, labels, value in parsed
        if name == HIVE and "hive_id" in labels and now_s - value <= FRESH_S
    }
    for name, labels, value in parsed:
        hive = fresh.get(labels.get("hive_id", ""))
        if name == HIVE_AGENTS and hive is not None and "harness" in labels:
            hive["agents"][labels["harness"]] = hive["agents"].get(labels["harness"], 0) + int(value)
    return sorted(fresh.values(), key=lambda hive: hive["id"])


def fetch(url: str) -> str | None:
    """The exposition body, or None when the collector does not answer."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
            return response.read().decode()
    except (OSError, ValueError):
        return None


def read(environ: Mapping[str, str], now_s: float, get: Callable[[str], str | None] = fetch) -> list[Hive] | None:
    """Fresh hives from the configured exposition, fetched at most once per CACHE_S; None when unset or silent."""
    url = environ.get(URL_ENV, "")
    if not url:
        return None
    cached = _cache.get(url)
    if cached is None or time.monotonic() - cached[0] >= CACHE_S:
        cached = (time.monotonic(), get(url))
        _cache[url] = cached
    return None if cached[1] is None else hives(cached[1], now_s)
