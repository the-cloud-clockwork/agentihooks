"""Events launch smoke fixtures: a hub answering admissions, hive spokes and a scratch swarm on the events engine,
all on a real nats-server and the Redis service container.

The smoke runs only with AGENTIHOOKS_EVENTS_SMOKE=1 and without the unit suite's conftest, which refuses Redis:
python -m pytest tests/events_smoke --confcutdir=tests/events_smoke
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import redis

from scripts.events import nats
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.hive import buckets, lanes
from scripts.hive.spoke import Spoke, SpokeConfig
from scripts.hub import admission, slots
from scripts.swarm import readiness
from scripts.swarm.store import EVENTS_ENGINE, RedisStore, SwarmConfig
from scripts.swarm_v2.runtime.base import Outcome, Status
from tests.events import nats_server
from tests.swarm.test_readiness import doc, task

ENABLED = "AGENTIHOOKS_EVENTS_SMOKE"
REDIS = "AGENTIHOOKS_EVENTS_SMOKE_REDIS_URL"
FOLDER = Path(__file__).resolve().parent
SLUG = "smoke"
ORIGIN = "https://github.com/o/r"
PULL = f"{ORIGIN}/pull/1"
POLICY = QueuePolicy(ack_wait_ms=1_000)
LAUNCH = {"harness": "codex", "profile": "engineer", "account": "work", "model": "gpt-5", "effort": "high"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the smoke unless its NATS server and Redis service are declared."""
    if os.environ.get(ENABLED) == "1":
        return
    skip = pytest.mark.skip(reason=f"the events smoke runs only with {ENABLED}=1 against its NATS and Redis")
    for item in items:
        if FOLDER in Path(item.path).parents:
            item.add_marker(skip)


def reading() -> dict[str, int]:
    """A headroom sample with room for every launch."""
    return {"cpus": 32, "load5": 0, "available_mb": 60000, "total_mb": 64000}


@dataclass
class Ledger:
    """The ledger the hub reads and claims through: one document whose rows the admission marks."""

    rows: dict[str, dict[str, Any]]
    rev: int = 1
    writes: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def metadata(self, slug: str) -> dict[str, Any]:
        """The document revision the readiness source caches its export by."""
        return {"_meta": {"rev": self.rev}}

    def state(self, slug: str) -> dict[str, Any]:
        """The ledger document with every row as it stands now."""
        return doc([dict(row) for row in self.rows.values()])

    def update_task(
        self,
        slug: str,
        task_id: str,
        fields: dict[str, Any],
        if_state: tuple[str, ...] = (),
        if_match: dict[str, Any] | None = None,
        by: str = "swarm",
    ) -> dict[str, Any]:
        """Applies a guarded row write the way the ledger server does and returns the row."""
        row = self.rows[task_id]
        matched = all(
            row.get(key, "" if key == "claimed_by" else 0) == value for key, value in (if_match or {}).items()
        )
        if (not if_state or row["state"] in if_state) and matched:
            if fields.get("claimed_by", row.get("claimed_by", "")) != row.get("claimed_by", ""):
                row["claim_generation"] = row.get("claim_generation", 0) + 1
            row.update(fields)
            self.rev += 1
            self.writes.append((slug, task_id, dict(fields)))
        return dict(row)

    def comment(self, *args: Any, **kwargs: Any) -> None:
        """Accepts the claim cap comment a spent task would get."""


@dataclass
class Smoke:
    """The running hub, its store and ledger, and a way to start hive spokes and offer tasks on the real bus."""

    url: str
    bus: nats.NatsBus
    store: RedisStore
    ledger: Ledger
    admitter: admission.Admission
    opened: list[nats.NatsBus]

    def spoke(self, hive: str, lane: str) -> SimpleNamespace:
        """A hive lane spoke on its own bus connection, asking this hub, with a runtime that launches at once."""
        bus = nats.open_bus(BusConfig(backend="nats", url=self.url, provision=False))
        self.opened.append(bus)
        runtime = Mock()
        runtime.spawn.side_effect = lambda request: Outcome(
            "spawn", Status.OK, "local", SimpleNamespace(pane_id=f"{hive}:{request.name}", **LAUNCH)
        )
        tasks = lanes.LaneTasks(bus, hive, self.store.config)
        config = SpokeConfig(self.store.config(SLUG), lane, hive, POLICY)
        return SimpleNamespace(spoke=Spoke(config, bus, runtime, tasks, reading, view=lambda: None), runtime=runtime)

    def offer(self, task_id: str) -> None:
        """Publishes the ready offer readiness would publish for an open row."""
        row = self.ledger.rows[task_id]
        generation = readiness.generation(row)
        queue = self.bus.queue(readiness.queue_name(SLUG, row["lane"]), POLICY)
        queue.offer(
            Envelope(
                f"ready:{SLUG}:{task_id}:{generation}",
                "task.ready",
                SLUG,
                "task",
                task_id,
                generation,
                1,
                "readiness",
                {"lane": row["lane"], "generation": generation},
            )
        )


@pytest.fixture
def smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Smoke]:
    """A hub serving admissions for a running scratch swarm on the events engine over a real NATS and Redis."""
    url = os.environ.get(REDIS, "")
    assert url, f"{REDIS} must name the Redis service"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "agentihooks"))
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES", "0")
    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
    client = redis.Redis.from_url(url, decode_responses=True)
    client.flushdb()
    store = RedisStore(client)
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create(SwarmConfig(SLUG, str(repo), 2, 2, engine=EVENTS_ENGINE, repo_origin=ORIGIN))
    store.update(SLUG, state="running", snapshot_minutes=0)
    ledger = Ledger(
        {
            "t1": task("t1"),
            "t2": task("t2"),
            "t-pr": task("t-pr", lane="ci", pr_url=PULL),
        }
    )
    admitter = admission.Admission(store, ledger, readiness.Source(store, ledger))
    with nats_server.running(tmp_path / "nats") as nats_url:
        bus = nats.open_bus(BusConfig(backend="nats", url=nats_url))
        opened = [bus]
        try:
            buckets.provision(bus)
            slots.serve(bus, admitter=admitter)
            yield Smoke(nats_url, bus, store, ledger, admitter, opened)
        finally:
            for each in reversed(opened):
                each.close()
            client.flushdb()
            client.close()
