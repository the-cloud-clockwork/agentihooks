"""Seeds a real Redis swarm store and a fake ledger at production size for the tick scale gate."""

import json
from dataclasses import asdict
from typing import Any

from scripts.swarm import fencing
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick.adapters.records_redis import RedisRecords
from tests.swarm.test_tick import FakeLedger

SLUG = "sw"
EXECUTIONS = 20_000
FREEZES = 2_000
TASKS = 6_000
PHASES = 60
OPEN = 60
SEATS = 40
CI_SEATS = 10
CHUNK = 1_000
LIVE = 30
CLAIM_MS = 3_600_000
STAMP = "2026-11-10T00:00:00Z"
DESCRIPTION = (
    "Launch the tick pass against the production sized store, keep every step inside its budget, record the proof "
    "on the ledger and close the task once the pull request merges into dev and the promotion run is green. "
) * 6


def archived(index: int, started_ms: int) -> dict[str, Any]:
    """Return the stored row of archived execution ``index``, one ended attempt of a Kubernetes engineer."""
    record = AgentRecord(
        name=f"engineer@seed00-{index:05d}",
        lane="eng",
        task=f"t-{index % TASKS:05d}",
        pane_id=f"w1:p{index}",
        harness="claude",
        account="acct",
        started_at=started_ms - index * 1_000,
        state="ended",
        model="opus",
        effort="high",
        seat=f"eng-{index % SEATS + 1}@{SLUG}",
        execution_id=f"exe-{index:032x}",
        generation=index // SEATS + 1,
        runtime_backend="kubernetes",
        runtime_target={"pod_namespace": "swarm", "pod_name": f"worker-{index}", "pod_uid": f"uid-{index:08d}"},
        launch_timings={"placed_s": 1.5, "ready_s": 9.0},
        hive="swarm-hive-001",
    )
    return asdict(record)


def _stored(value: object) -> str:
    """Return ``value`` as one fenced record body at revision one."""
    return json.dumps({"r": 1, "v": value}, separators=(",", ":"))


def seed_archive(store: RedisStore, started_ms: int) -> None:
    """Write ``EXECUTIONS`` archived executions and their name index straight into the swarm's records hashes."""
    port = RedisRecords(store.redis)
    archive, names = port.key(SLUG, fencing.ARCHIVE), port.key(SLUG, fencing.ARCHIVED_NAMES)
    for start in range(0, EXECUTIONS, CHUNK):
        rows = [archived(index, started_ms) for index in range(start, min(start + CHUNK, EXECUTIONS))]
        pipe = store.redis.pipeline(transaction=False)
        pipe.hset(archive, mapping={row["execution_id"]: _stored(row) for row in rows})
        pipe.hset(names, mapping={row["name"]: _stored(row["execution_id"]) for row in rows})
        pipe.execute()
    store.redis.hset(names, fencing.INDEXED, _stored(None))


def seed_store(redis: Any, started_ms: int) -> RedisStore:
    """Return a fresh swarm store on ``redis`` on the per record layout, holding the seeded execution archive."""
    redis.flushdb()
    store = RedisStore(redis)
    store.create(SwarmConfig(SLUG, "/repo", max_eng=SEATS - CI_SEATS, max_ci=CI_SEATS))
    fencing.migrate_records(store, SLUG, writers_upgraded=True)
    seed_archive(store, started_ms)
    return store


def seed_live(store: RedisStore, ledger: "ScaleLedger", started_ms: int) -> list[str]:
    """Start ``LIVE`` Kubernetes executions that hold the first open tasks, so every pass reaps, verifies and
    updates real executions, and return their names."""
    names = []
    for index in range(LIVE):
        task = ledger.rows[f"t-{index:05d}"]
        lane = task["lane"]
        seat = f"{lane}-{index + 1}@{SLUG}"
        target = {"pod_namespace": "swarm", "pod_name": f"live-{index}", "pod_uid": f"live-uid-{index:04d}"}
        agent = AgentRecord(
            store.next_name(SLUG, lane),
            lane,
            task["id"],
            pane_id=f"w1:l{index}",
            harness="claude",
            account="acct",
            started_at=started_ms,
            seat=seat,
            runtime_backend="kubernetes",
            runtime_target=target,
            hive="swarm-hive-001",
        )
        started = store.start_execution(SLUG, agent)
        store.claim(SLUG, task["id"], started.name, CLAIM_MS)
        task.update(state="claimed", claimed_by=started.name)
        names.append(started.name)
    return names


def task_row(index: int) -> dict[str, Any]:
    """Return ledger task ``index``: the first ``OPEN`` open and claimable, the next ``FREEZES`` open and frozen,
    the rest done."""
    state = "open" if index < OPEN + FREEZES else "done"
    return {
        "id": f"t-{index:05d}",
        "title": f"Scale task {index}",
        "description": DESCRIPTION,
        "lane": "ci" if index % 4 == 3 else "eng",
        "phase": f"p-{index % PHASES:02d}",
        "state": state,
        "kind": "code",
        "rank": "normal",
        "done": state == "done",
        "comments_count": index % 7,
    }


def freezes() -> list[dict[str, Any]]:
    """Return ``FREEZES`` freeze records, one per open task after the claimable ones."""
    return [
        {
            "id": f"fz-{index:05d}",
            "verb": "freeze",
            "target": f"tasks/t-{OPEN + index:05d}",
            "by": "operator",
            "at": STAMP,
        }
        for index in range(FREEZES)
    ]


class ScaleLedger(FakeLedger):
    """The fake ledger holding a full size view: every task, its phases and the freeze records."""

    def __init__(self) -> None:
        super().__init__([task_row(index) for index in range(TASKS)])
        self.phases = [{"id": f"p-{index:02d}", "title": f"Phase {index}"} for index in range(PHASES)]
        self.freezes = freezes()
        self.said: list[tuple[str, str | None]] = []

    def metadata(self, slug: str) -> dict[str, Any]:
        """Return the ledger overview metadata."""
        return {"overview": "Project intent", "_meta": {}}

    def say(self, slug: str, text: str, by: str | None = None) -> None:
        """Record one chat line the swarm said."""
        self.said.append((text, by))

    def relay(self, slug: str, text: str, by: str, op_id: str = "", reply_to: str = "") -> None:
        """Record one relayed chat line."""
        self.said.append((text, by))

    def chat(self, slug: str) -> list[dict[str, Any]]:
        """Return the ledger chat, one old operator line."""
        return [{"id": "old", "by": "operator", "at": 50, "text": "old talk"}]

    def state(self, slug: str) -> dict[str, Any]:
        """Return the ledger state with its freeze records."""
        return {**super().state(slug), "freezes": self.freezes}
