import hashlib
import json
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime

from hooks.classifier import decision_log
from scripts.gates import log as gate_log
from scripts.swarm import capacity, host_budget, metrics_outbox
from scripts.swarm.store import RedisStore

IDENTITY = ("agent", "lane", "harness", "account", "model", "effort", "execution_id")
EVENT_COLUMNS = (*((key, "String") for key in IDENTITY), ("kind", "String"), ("reason", "String"))
AGENTS = metrics_outbox.Table("agent_events", EVENT_COLUMNS)
DELIVERY = metrics_outbox.Table(
    "delivery_events", (*EVENT_COLUMNS, ("pull_request", "String"), ("claim_to_merge_seconds", "Float64"))
)
HOST = metrics_outbox.Table(
    "host_samples",
    (
        ("host", "String"),
        ("available_mb", "Int64"),
        ("load_per_cpu", "Float64"),
        ("live_agents", "Int64"),
        ("held_spawns", "Int64"),
        ("reason", "String"),
    ),
)
QUOTA = metrics_outbox.Table(
    "quota_samples",
    (
        ("harness", "String"),
        ("account", "String"),
        ("state", "String"),
        ("five_left", "Float64"),
        ("five_known", "Int64"),
        ("week_left", "Float64"),
        ("week_known", "Int64"),
        ("sessions", "Int64"),
    ),
)
CLASSIFIERS = metrics_outbox.Table(
    "classifier_calls",
    (
        ("host", "String"),
        ("definition", "String"),
        ("backend", "String"),
        ("latency_ms", "Int64"),
        ("verdict", "String"),
    ),
)


@dataclass(frozen=True)
class TickInput:
    store: RedisStore
    doc: dict
    findings: list
    view: Callable[[str], object]


def _base(slug: str, at: int, identity: object, task: dict) -> dict:
    return {
        "event_id": hashlib.sha256(json.dumps([slug, identity], sort_keys=True).encode()).hexdigest(),
        "ledger": slug,
        "ts_ms": at,
        "plan": task.get("plan_url", ""),
        "phase": task.get("phase", ""),
        "slice": task.get("plan_slice", ""),
        "task": task.get("id", ""),
    }


def _task(doc: dict, task_id: str) -> dict:
    return next((task for task in doc.get("tasks", []) if task["id"] == task_id), {"id": task_id})


def _owner(agents: list, task_id: str, at: int, name: str = "") -> dict:
    named = [agent for agent in agents if name and agent["name"] == name]
    candidates = named or [
        agent for agent in agents if agent["task"] == task_id and agent["started_at"] <= at <= agent.get("ended_at", at)
    ]
    return max(candidates, key=lambda agent: agent["started_at"], default={})


def _event(slug: str, task: dict, agent: dict, at: int, kind: str, identity: object, reason: str = "") -> dict:
    return {
        **_base(slug, at, [kind, identity], task),
        **{key: agent.get("name" if key == "agent" else key, "") for key in IDENTITY},
        "kind": kind,
        "reason": reason,
    }


def agent_rows(slug: str, doc: dict, agents: list) -> list[dict]:
    rows = []
    for agent in agents:
        task = _task(doc, agent["task"])
        life = [agent["name"], agent["started_at"]]
        if agent["started_at"]:
            rows.append(_event(slug, task, agent, agent["started_at"], "spawn", life))
        if agent.get("ended_at"):
            rows.append(_event(slug, task, agent, agent["ended_at"], "retire", life, agent["reason"]))
    for source in doc.get("_meta", {}).get("events", []):
        if source["kind"] != "task claimed":
            continue
        task_id = source["target"].removeprefix("tasks/")
        agent = _owner(agents, task_id, source["at"], source.get("by", ""))
        rows.append(_event(slug, _task(doc, task_id), agent, source["at"], "claim", source))
    return rows


def _delivery(row: dict, task: dict, duration: float = -1.0) -> dict:
    return {**row, "pull_request": task.get("pr_url", ""), "claim_to_merge_seconds": duration}


def delivery_rows(slug: str, doc: dict, agents: list, pulls: dict) -> list[dict]:
    rows, claims = [], {}
    kinds = {"task pr": "pull_request_opened", "review round": "review_round"}
    for source in doc.get("_meta", {}).get("events", []):
        task_id = source["target"].removeprefix("tasks/")
        if source["kind"] == "task claimed":
            claims[task_id] = min(claims.get(task_id, source["at"]), source["at"])
        if source["kind"] not in kinds:
            continue
        task = _task(doc, task_id)
        agent = _owner(agents, task_id, source["at"], source.get("by", ""))
        rows.append(_delivery(_event(slug, task, agent, source["at"], kinds[source["kind"]], source), task))
    for task in doc.get("tasks", []):
        pull = pulls.get(task.get("pr_url"))
        if pull is None or pull.state != "MERGED" or pull.merged_at is None:
            continue
        at = pull.merged_at
        claimed = claims.get(task["id"])
        duration = (at - claimed) / 1000 if claimed is not None else -1.0
        agent = _owner(agents, task["id"], at)
        rows.append(_delivery(_event(slug, task, agent, at, "merge", task["pr_url"]), task, duration))
    return rows


def gate_rows(slug: str, doc: dict, agents: list, gates: list) -> tuple[list, list]:
    rows, deliveries = [], []
    for source in gates:
        task = _task(doc, source["task"])
        agent = _owner(agents, source["task"], source["at"], source["agent"])
        if source["kind"] == "deny":
            rows.append(_event(slug, task, agent, source["at"], "gate_deny", source, source["reason"]))
        elif source["gate"] == "idle-ticks" and source["kind"] == "count":
            rows.append(_event(slug, task, agent, source["at"], "idle", source, source["reason"]))
        elif source["gate"] == "reruns" and source["kind"] == "count":
            deliveries.append(
                _delivery(_event(slug, task, agent, source["at"], "rerun", source, source["reason"]), task)
            )
    return rows, deliveries


def signal_rows(slug: str, doc: dict, agents: list, handoffs: list, found: list) -> list[dict]:
    rows = []
    for source in handoffs:
        task = _task(doc, source["task"])
        agent = _owner(agents, source["task"], source["at"], source["predecessor"])
        rows.append(_event(slug, task, agent, source["at"], "handoff", source["id"], source["reason"]))
    for source in found:
        at, subject = source["seen_at"], source["subject"]
        agent = _owner(agents, subject, at, subject)
        task = _task(doc, agent.get("task", subject if any(t["id"] == subject for t in doc.get("tasks", [])) else ""))
        rows.append(_event(slug, task, agent, at, "health_finding", [source["id"], at], source["summary"]))
    return rows


def host_row(slug: str, now_ms: int, sample: host_budget.HostSample, quota: dict) -> dict:
    return {
        **_base(slug, now_ms, ["host", now_ms], {}),
        "host": socket.gethostname(),
        "available_mb": sample.available_mb,
        "load_per_cpu": sample.load1 / sample.cpus,
        "live_agents": sample.agents,
        "held_spawns": quota.get("held_spawns", 0),
        "reason": quota.get("reason", ""),
    }


def quota_rows(slug: str, now_ms: int, quota: dict) -> list[dict]:
    return [
        {
            **_base(slug, now_ms, ["quota", now_ms, source["harness"], source["name"]], {}),
            "harness": source["harness"],
            "account": source["name"],
            "state": source["state"],
            "five_left": float(source["five_left"]) if source["five_left"] is not None else -1.0,
            "five_known": int(source["five_left"] is not None),
            "week_left": float(source["week_left"]) if source["week_left"] is not None else -1.0,
            "week_known": int(source["week_left"] is not None),
            "sessions": source["sessions"],
        }
        for source in quota.get("accounts", [])
    ]


def classifier_rows(calls: list, host: str) -> list[dict]:
    return [
        {
            **_base(
                f"host:{host}",
                int(datetime.fromisoformat(source["ts"]).timestamp() * 1000),
                ["classifier", index, source],
                {},
            ),
            "host": host,
            "definition": source.get("definition", source["purpose"]),
            "backend": source["source"] or "",
            "latency_ms": source["latency_ms"],
            "verdict": json.dumps(source["answers"], sort_keys=True),
        }
        for index, source in enumerate(calls)
    ]


def read_review_events(slug: str) -> list[dict]:
    path = gate_log.gates_dir(slug) / "intent" / "history.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        source = json.loads(line)
        state = json.loads(source["classifier_input"])["state"]
        for review in state.get("reviewer_findings", {}).get("reviews", []):
            if not review.get("submittedAt"):
                continue
            events.append(
                {
                    "kind": "review round",
                    "target": f"tasks/{source['task']}",
                    "at": int(datetime.fromisoformat(review["submittedAt"]).timestamp() * 1000),
                    "by": source["agent"],
                    "review_id": review["id"],
                }
            )
    return events


def read_classifier_calls() -> list[dict]:
    return decision_log.read()


def _held_spawns(store: RedisStore, slug: str, doc: dict, quota: dict, agents: list) -> int:
    _, ready = capacity.ready_work(slug, store, doc)
    busy = {lane: sum(agent["lane"] == lane for agent in agents) for lane in capacity.LANES}
    return sum(
        max(
            0,
            min(len(ready[lane]), max(0, quota.get("configured", {}).get(lane, 0) - busy[lane]))
            - len(quota.get("placements", {}).get(lane, [])),
        )
        for lane in capacity.LANES
    )


def record_pass(
    box: metrics_outbox.Outbox, slug: str, now_ms: int, store: RedisStore, doc: dict, found: list, pulls: dict
) -> None:
    agents = [json.loads(row) for row in store.redis.lrange(store.key(slug, "history"), 0, -1)]
    agents += [asdict(agent) for agent in store.agents(slug)]
    handoffs = [json.loads(row) for row in store.redis.hgetall(store.key(slug, "transfers")).values()]
    gates, reruns = gate_rows(slug, doc, agents, gate_log.recent(slug, limit=None))
    quota = capacity.read(store, slug)
    quota["held_spawns"] = _held_spawns(store, slug, doc, quota, [asdict(agent) for agent in store.agents(slug)])
    review_doc = {**doc, "_meta": {"events": [*doc.get("_meta", {}).get("events", []), *read_review_events(slug)]}}
    batches = (
        (AGENTS, agent_rows(slug, doc, agents) + gates + signal_rows(slug, doc, agents, handoffs, found)),
        (DELIVERY, delivery_rows(slug, review_doc, agents, pulls) + reruns),
        (HOST, [host_row(slug, now_ms, host_budget.read_host(), quota)]),
        (QUOTA, quota_rows(slug, now_ms, quota)),
        (CLASSIFIERS, classifier_rows(read_classifier_calls(), socket.gethostname())),
    )
    for table, rows in batches:
        box.append(table, [row for row in rows if row["ts_ms"] >= now_ms - metrics_outbox.DAY_MS])
