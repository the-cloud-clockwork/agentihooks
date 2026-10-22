import hashlib
import json
import socket
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from hooks.classifier import decision_log
from scripts import agent_choice
from scripts.gates import log as gate_log
from scripts.hive import placement_waits
from scripts.swarm import capacity, host_budget, metrics_outbox
from scripts.swarm.store import RedisStore
from scripts.swarm.tick import SPAWN_HOLD

IDENTITY = ("agent", "lane", "harness", "account", "model", "effort", "execution_id")
VERDICTS = ("fail", "pass")
EVENT_COLUMNS = (*((key, "String") for key in IDENTITY), ("kind", "String"), ("reason", "String"))
AGENTS = metrics_outbox.Table("agent_events", (*EVENT_COLUMNS, ("finding_kind", "String")))
DELIVERY = metrics_outbox.Table(
    "delivery_events",
    (*EVENT_COLUMNS, ("pull_request", "String"), ("branch", "String"), ("claim_to_merge_seconds", "Float64")),
)
HOST = metrics_outbox.Table(
    "host_samples",
    (
        ("host", "String"),
        ("available_mb", "Int64"),
        ("load_per_cpu", "Float64"),
        ("live_agents", "Int64"),
        ("workstation_agents", "Int64"),
        ("hive_agents", "Int64"),
        ("swap_used_mb", "Int64"),
        ("swap_total_mb", "Int64"),
        ("agent_mb", "Int64"),
        ("held_spawns", "Int64"),
        ("held_by", "String"),
        ("held_harness", "String"),
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
    ledger: object


@dataclass(frozen=True)
class LogBatch:
    source: str
    position: int
    rows: list


def _log_batch(path: Path, box: metrics_outbox.Outbox) -> LogBatch:
    if not path.exists():
        return LogBatch("", 0, [])
    stat = path.stat()
    source = f"{path}:{stat.st_dev}:{stat.st_ino}"
    with box.db:
        box.db.execute(
            "CREATE TABLE IF NOT EXISTS metrics_offsets (source TEXT PRIMARY KEY, position INTEGER NOT NULL)"
        )
    saved = box.db.execute("SELECT position FROM metrics_offsets WHERE source = ?", (source,)).fetchone()
    position, rows = saved[0] if saved else 0, []
    with path.open("rb") as stream:
        stream.seek(position)
        while line := stream.readline():
            if not line.endswith(b"\n"):
                break
            row = json.loads(line)
            rows.append({**row, "_source_id": f"{source}:{position}"})
            position = stream.tell()
    return LogBatch(source, position, rows)


def _checkpoint(box: metrics_outbox.Outbox, batch: LogBatch) -> None:
    if batch.source:
        with box.db:
            box.db.execute(
                "INSERT INTO metrics_offsets VALUES (?, ?) ON CONFLICT(source) DO UPDATE SET position = MAX(position, excluded.position)",
                (batch.source, batch.position),
            )


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


def _with_finding_kind(row: dict, finding_kind: str = "") -> dict:
    return {**row, "finding_kind": finding_kind}


def agent_rows(slug: str, doc: dict, agents: list) -> list[dict]:
    rows = []
    for agent in agents:
        task = _task(doc, agent["task"])
        life = [agent["name"], agent["started_at"]]
        if agent["started_at"]:
            rows.append(_with_finding_kind(_event(slug, task, agent, agent["started_at"], "spawn", life)))
        if agent.get("ended_at"):
            rows.append(
                _with_finding_kind(_event(slug, task, agent, agent["ended_at"], "retire", life, agent["reason"]))
            )
    for source in doc.get("_meta", {}).get("events", []):
        if source["kind"] != "task claimed":
            continue
        task_id = source["target"].removeprefix("tasks/")
        agent = _owner(agents, task_id, source["at"], source.get("by", ""))
        rows.append(_with_finding_kind(_event(slug, _task(doc, task_id), agent, source["at"], "claim", source)))
    return rows


def _delivery(row: dict, task: dict, duration: float = -1.0) -> dict:
    return {
        **row,
        "pull_request": task.get("pr_url", ""),
        "branch": task.get("branch", ""),
        "claim_to_merge_seconds": duration,
    }


def _split_merges(slug: str, doc: dict, agents: list, pulls: dict) -> list[dict]:
    rows = []
    for task in doc.get("tasks", []):
        for url in task.get("split_pulls") or []:
            pull = pulls.get(url)
            if pull is None or pull.state != "MERGED" or pull.merged_at is None:
                continue
            opened = next(
                (
                    source
                    for source in doc.get("_meta", {}).get("events", [])
                    if source["target"] == f"tasks/{task['id']}" and source.get("pr_url") == url
                ),
                {},
            )
            identity = {**task, "pr_url": url, "branch": opened.get("branch", task.get("branch", ""))}
            agent = _owner(agents, task["id"], pull.merged_at)
            rows.append(_delivery(_event(slug, task, agent, pull.merged_at, "pull_request_merged", url), identity))
    return rows


def delivery_rows(slug: str, doc: dict, agents: list, pulls: dict) -> list[dict]:
    rows, claims = [], {}
    kinds = {
        "task pr": "pull_request_opened",
        "task parked": "parked",
        "task unparked": "unparked",
        "review round": "review_round",
        "intent fail": "intent_failed",
        "intent pass": "intent_passed",
    }
    for source in doc.get("_meta", {}).get("events", []):
        task_id = source["target"].removeprefix("tasks/")
        if source["kind"] == "task claimed":
            claims[task_id] = min(claims.get(task_id, source["at"]), source["at"])
        if source["kind"] not in kinds:
            continue
        task = {**_task(doc, task_id), **{key: source[key] for key in ("pr_url", "branch") if key in source}}
        agent = _owner(agents, task_id, source["at"], source.get("by", ""))
        rows.append(
            _delivery(
                _event(slug, task, agent, source["at"], kinds[source["kind"]], source.get("review_id", source)), task
            )
        )
    for task in doc.get("tasks", []):
        pull = pulls.get(task.get("pr_url"))
        if pull is None or pull.state != "MERGED" or pull.merged_at is None or task.get("state") != "done":
            continue
        at = pull.merged_at
        claimed = claims.get(task["id"])
        duration = (at - claimed) / 1000 if claimed is not None else -1.0
        agent = _owner(agents, task["id"], at)
        rows.append(_delivery(_event(slug, task, agent, at, "merge", task["id"]), task, duration))
    return rows + _split_merges(slug, doc, agents, pulls)


def queue_rows(slug: str, doc: dict, agents: list, pulls: dict, now_ms: int) -> list[dict]:
    rows = []
    for task in doc["tasks"]:
        pull = pulls.get(task.get("pr_url"))
        if pull is None or pull.state != "OPEN" or not pull.queued:
            continue
        agent = _owner(agents, task["id"], now_ms)
        rows.append(_delivery(_event(slug, task, agent, now_ms, "merge_queued", [task["pr_url"], pull.head]), task))
    return rows


def _supersede_merges(box: metrics_outbox.Outbox, rows: list) -> None:
    with box.db:
        box.db.executemany(
            "DELETE FROM spool WHERE tbl = ? AND event_id = ? AND row != ?",
            [(DELIVERY.name, row["event_id"], json.dumps(row)) for row in rows if row["kind"] == "merge"],
        )


def launch_rows(slug: str, doc: dict, launches: list, agents: list) -> list[dict]:
    kinds = {"failed": "launch_failed", "waiting": "launch_waiting"}
    rows = []
    for source in launches:
        if source["state"] not in kinds:
            continue
        agent = {**next((agent for agent in agents if agent["name"] == source["agent"]), {}), "name": source["agent"]}
        identity = [source["agent"], source["at"]]
        event = _event(
            slug, _task(doc, source["task"]), agent, source["at"], kinds[source["state"]], identity, source["error"]
        )
        rows.append(_with_finding_kind(event))
    return rows


def gate_rows(slug: str, doc: dict, agents: list, gates: list) -> tuple[list, list]:
    rows, deliveries = [], []
    for source in gates:
        task = _task(doc, source["task"])
        agent = _owner(agents, source["task"], source["at"], source["agent"])
        if source["kind"] == "deny":
            rows.append(
                _with_finding_kind(_event(slug, task, agent, source["at"], "gate_deny", source, source["reason"]))
            )
        elif source["gate"] == "idle-ticks" and source["kind"] == "count":
            rows.append(_with_finding_kind(_event(slug, task, agent, source["at"], "idle", source, source["reason"])))
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
        rows.append(
            _with_finding_kind(_event(slug, task, agent, source["at"], "handoff", source["id"], source["reason"]))
        )
    for source in found:
        at, subject = source["seen_at"], source["subject"]
        agent = _owner(agents, subject, at, subject)
        task = _task(doc, agent.get("task", subject if any(t["id"] == subject for t in doc.get("tasks", [])) else ""))
        row = _event(slug, task, agent, at, "health_finding", [source["id"], at], source["summary"])
        rows.append(_with_finding_kind(row, source["kind"]))
    return rows


def placement_counts(agents: list[dict]) -> dict:
    from scripts.swarm.commands import hive_id

    here = hive_id()
    remote = sum(
        agent.get("runtime_backend", "local") != "local"
        or (agent.get("runtime_target") or {}).get("host_id", here) not in ("", here)
        for agent in agents
    )
    return {"workstation_agents": len(agents) - remote, "hive_agents": remote}


def host_row(slug: str, now_ms: int, sample: host_budget.HostSample, quota: dict) -> dict:
    return {
        **_base(slug, now_ms, ["host", now_ms], {}),
        "host": socket.gethostname(),
        "available_mb": sample.available_mb,
        "load_per_cpu": sample.load1 / sample.cpus,
        "live_agents": sample.agents,
        "workstation_agents": quota.get("workstation_agents", 0),
        "hive_agents": quota.get("hive_agents", 0),
        "swap_used_mb": sample.swap_used_mb,
        "swap_total_mb": sample.swap_total_mb,
        "agent_mb": sample.agent_mb,
        "held_spawns": quota.get("held_spawns", 0),
        "held_by": quota.get("held_by", ""),
        "held_harness": quota.get("held_harness", ""),
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
                ["classifier", source["_source_id"]],
                {},
            ),
            "host": host,
            "definition": source.get("definition") or source["purpose"],
            "backend": source["source"] or "",
            "latency_ms": source["latency_ms"],
            "verdict": json.dumps(source["answers"], sort_keys=True),
        }
        for source in calls
    ]


def pull_targets(doc: dict, now_ms: int, recent: Sequence[dict] = ()) -> list[tuple[str, bool]]:
    recorded = {row["pull_request"] for row in recent if row["kind"] == "merge"}
    merged = {row["pull_request"] for row in recent if row["kind"] == "pull_request_merged"}
    completed = {
        event["target"].removeprefix("tasks/")
        for event in doc.get("_meta", {}).get("events", [])
        if event["kind"] == "task done" and event["at"] >= now_ms - metrics_outbox.DAY_MS
    }
    return [
        (url, url == task.get("pr_url"))
        for task in doc.get("tasks", [])
        if task.get("state") in ("pr", "claimed") or task.get("id") in completed
        for url in dict.fromkeys([*(task.get("split_pulls") or []), task.get("pr_url")])
        if url and url not in (recorded if url == task.get("pr_url") else merged)
    ]


def pull_rows(box: metrics_outbox.Outbox, now_ms: int, swarm: TickInput) -> dict:
    recent = box.recent("delivery_events", now_ms)
    return {url: swarm.view(url) for url, _ in pull_targets(swarm.doc, now_ms, recent)}


def read_review_events(slug: str, box: metrics_outbox.Outbox) -> LogBatch:
    batch = _log_batch(gate_log.gates_dir(slug) / "intent" / "history.jsonl", box)
    events = []
    for source in batch.rows:
        if source.get("verdict") in VERDICTS:
            events.append(
                {
                    "kind": f"intent {source['verdict']}",
                    "target": f"tasks/{source['task']}",
                    "at": source["at"],
                }
            )
        state = json.loads(source["classifier_input"])["state"]
        for review in state.get("reviewer_findings", {}).get("reviews", []):
            if not review.get("submittedAt"):
                continue
            events.append(
                {
                    "kind": "review round",
                    "target": f"tasks/{source['task']}",
                    "at": int(datetime.fromisoformat(review["submittedAt"]).timestamp() * 1000),
                    "review_id": review["id"],
                }
            )
    return LogBatch(batch.source, batch.position, events)


def read_review_rounds(slug: str, box: metrics_outbox.Outbox) -> LogBatch:
    batch = _log_batch(gate_log.gates_dir(slug) / "reviews" / "rounds.jsonl", box)
    events = [
        {
            "kind": "review round",
            "target": f"tasks/{source['task']}",
            "at": source["at"],
            "review_id": f"swarm:{source['task']}:{source['counter']}:{source['round']}",
        }
        for source in batch.rows
    ]
    return LogBatch(batch.source, batch.position, events)


def read_classifier_calls(box: metrics_outbox.Outbox) -> LogBatch:
    return _log_batch(decision_log.log_path(), box)


def _held_lanes(store: RedisStore, slug: str, doc: dict, quota: dict, agents: list, host_held: bool) -> dict:
    _, ready = capacity.ready_work(slug, store, doc)
    busy = {lane: sum(agent["lane"] == lane for agent in agents) for lane in capacity.LANES}
    return {
        lane: max(
            0,
            min(len(ready[lane]), max(0, quota.get("configured", {}).get(lane, 0) - busy[lane]))
            - (0 if host_held else len(quota.get("placements", {}).get(lane, []))),
        )
        for lane in capacity.LANES
    }


def held_harness(lanes: dict, held: dict) -> str:
    agents = {lanes.get(lane, {}).get("agent") for lane, count in held.items() if count}
    return agents.pop() if len(agents) == 1 and agents.issubset(agent_choice.AGENTS) else ""


def held_pins(lanes: dict, held: dict, reason: str) -> str:
    notes = [
        f"{count} {lane} {'spawn' if count == 1 else 'spawns'} held on the {lane} lane pinned to {agent}"
        for lane, count in sorted(held.items())
        if count and (agent := lanes.get(lane, {}).get("agent")) in agent_choice.AGENTS
    ]
    return "; ".join(part for part in (reason, *notes) if part)


def hive_wait(store: RedisStore, slug: str, held: dict) -> str:
    reasons = sorted(wait["reason"] for wait in placement_waits.read(store, slug).values() if held.get(wait["lane"]))
    return f"hive room: {reasons[0]}" if reasons else ""


def record_pass(
    box: metrics_outbox.Outbox, slug: str, now_ms: int, store: RedisStore, doc: dict, found: list, pulls: dict
) -> None:
    agents = [json.loads(row) for row in store.redis.lrange(store.key(slug, "history"), 0, -1)]
    agents += [asdict(agent) for agent in store.agents(slug)]
    handoffs = [json.loads(row) for row in store.redis.hgetall(store.key(slug, "transfers")).values()]
    gates, reruns = gate_rows(slug, doc, agents, gate_log.recent(slug, limit=None))
    quota = capacity.read(store, slug)
    hold = store.redis.get(store.key(slug, SPAWN_HOLD)) or ""
    live = [asdict(agent) for agent in store.agents(slug)]
    held = _held_lanes(store, slug, doc, quota, live, bool(hold))
    quota["held_spawns"] = sum(held.values())
    room = hold or hive_wait(store, slug, held)
    quota["held_by"] = ("host" if room else "quota") if quota["held_spawns"] else ""
    quota["held_harness"] = "" if room else held_harness(store.config(slug).lanes, held)
    quota["reason"] = room or held_pins(store.config(slug).lanes, held, quota.get("reason", ""))
    quota.update(placement_counts(live))
    reviews, classifiers = read_review_events(slug, box), read_classifier_calls(box)
    rounds = read_review_rounds(slug, box)
    review_doc = {**doc, "_meta": {"events": [*doc.get("_meta", {}).get("events", []), *reviews.rows, *rounds.rows]}}
    batches = (
        (
            AGENTS,
            agent_rows(slug, doc, agents)
            + gates
            + signal_rows(slug, doc, agents, handoffs, found)
            + launch_rows(slug, doc, store.launches(slug), agents),
        ),
        (
            DELIVERY,
            delivery_rows(slug, review_doc, agents, pulls) + reruns + queue_rows(slug, doc, agents, pulls, now_ms),
        ),
        (HOST, [host_row(slug, now_ms, host_budget.read_host(), quota)]),
        (QUOTA, quota_rows(slug, now_ms, quota)),
        (CLASSIFIERS, classifier_rows(classifiers.rows, socket.gethostname())),
    )
    for table, rows in batches:
        recent = [row for row in rows if row["ts_ms"] >= now_ms - metrics_outbox.DAY_MS]
        if table is DELIVERY:
            _supersede_merges(box, recent)
        box.append(table, recent)
    _checkpoint(box, reviews)
    _checkpoint(box, rounds)
    _checkpoint(box, classifiers)
