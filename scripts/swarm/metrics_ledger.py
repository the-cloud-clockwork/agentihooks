import hashlib
import json
from collections import Counter

from scripts.swarm import metrics_outbox
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm_ledger.api.resources import node_state
from scripts.swarm_ledger.repository import hierarchy

PATH_KEYS = ("plan", "phase", "slice", "task")
EMPTY_PATH = dict.fromkeys(PATH_KEYS, "")
STATES = ("open", "claimed", "blocked", "pr", "done", "out_of_scope")
EVENTS = metrics_outbox.Table(
    "ledger_events",
    (
        ("revision", "Int64"),
        ("kind", "String"),
        ("by", "String"),
        ("target", "String"),
        ("lane", "String"),
        ("state", "String"),
        ("catch_up", "Int64"),
        ("first_missed", "Int64"),
        ("last_missed", "Int64"),
        ("payload", "String"),
    ),
)
SNAPSHOT_MS = 5 * 60 * 1000
SNAPSHOTS = metrics_outbox.Table(
    "ledger_snapshots",
    (("measure", "String"), ("lane", "String"), ("state", "String"), ("item", "String"), ("value", "Float64")),
)
BIRTHS = (
    "CREATE TABLE IF NOT EXISTS ledger_metric_births (slug TEXT, target TEXT, at INTEGER, PRIMARY KEY (slug, target))"
)
READ_BIRTHS = "SELECT target, at FROM ledger_metric_births WHERE slug=?"
SAVE_BIRTHS = "INSERT OR IGNORE INTO ledger_metric_births VALUES (?, ?, ?)"
CHECKPOINT = "CREATE TABLE IF NOT EXISTS ledger_metrics (slug TEXT PRIMARY KEY, revision INTEGER, snapshot_ms INTEGER)"
READ_CHECKPOINT = "SELECT revision, snapshot_ms FROM ledger_metrics WHERE slug=?"
SAVE_CHECKPOINT = "INSERT OR REPLACE INTO ledger_metrics VALUES (?, ?, ?)"
PATHS = "CREATE TABLE IF NOT EXISTS ledger_metric_paths (slug TEXT, node TEXT, value TEXT, PRIMARY KEY (slug, node))"
READ_PATHS = "SELECT node, value FROM ledger_metric_paths WHERE slug=?"
SAVE_PATHS = "INSERT OR REPLACE INTO ledger_metric_paths VALUES (?, ?, ?)"


def paths(nodes: list, tasks: list) -> dict:
    parents = {row["node"]: row for row in nodes}
    lanes = {f"tasks/{task['id']}": task.get("lane", "eng") for task in tasks}
    result = {}
    for row in nodes:
        path, node = EMPTY_PATH.copy(), row["node"]
        for _ in PATH_KEYS:
            ancestor = parents.get(node)
            if ancestor is None:
                break
            path[ancestor["kind"]] = node
            node = ancestor["parent"]
        result[row["node"]] = {**path, "lane": lanes.get(row["node"], "")}
    return result


def base(slug: str, ts_ms: int, identity: str, path: dict) -> dict:
    return {"event_id": identity, "ledger": slug, "ts_ms": ts_ms, **EMPTY_PATH, **path}


def event_row(slug: str, event: dict, path: dict, catch_up: bool, ordinal: int) -> dict:
    payload = json.dumps(event, sort_keys=True)
    identity = hashlib.sha256(payload.encode()).hexdigest()
    state = event["kind"].removeprefix("task ")
    return {
        **base(slug, event["at"], f"ledger:{slug}:{ordinal}:{identity}", path),
        "revision": event["rev"],
        "kind": event["kind"],
        "by": event["by"],
        "target": event["target"],
        "lane": path.get("lane", ""),
        "state": state if state in STATES else "",
        "catch_up": int(catch_up),
        "first_missed": 0,
        "last_missed": 0,
        "payload": payload,
    }


def event_rows(slug: str, events: list, known: dict, cursor: int | None, now_ms: int, lost: int | None) -> list:
    rows, positions = [], Counter()
    if cursor is not None and lost is not None and lost > cursor:
        first, last = cursor + 1, lost
        gap = {"rev": last, "at": now_ms, "by": "metrics", "kind": "history gap", "target": ""}
        row = event_row(slug, gap, {}, False, 0)
        rows.append({**row, "event_id": f"gap:{slug}:{first}:{last}", "first_missed": first, "last_missed": last})
    for event in events:
        ordinal = positions[event["rev"]]
        positions[event["rev"]] += 1
        if cursor is None or event["rev"] > cursor:
            rows.append(event_row(slug, event, known.get(event["target"], {}), cursor is None, ordinal))
    return rows


def snapshot_row(slug: str, now_ms: int, measure: str, item: str, path: dict, value: float) -> dict:
    fields = {"measure": measure, "item": item, "state": path.get("state", ""), "lane": path.get("lane", "")}
    identity = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    return {**base(slug, now_ms, f"snapshot:{slug}:{now_ms}:{identity}", path), **fields, "value": float(value)}


def count_rows(slug: str, now_ms: int, nodes: list, known: dict) -> list:
    groups = {"": Counter(), **{row["node"]: Counter() for row in nodes if row["kind"] != "task"}}
    lanes = {"", "eng", "ci", "plan"}
    for node in nodes:
        if node["kind"] != "task":
            continue
        path = known[node["node"]]
        lane, state = path["lane"], node["state"]
        lanes.add(lane)
        for group in ("", *(path[key] for key in PATH_KEYS[:3] if path[key])):
            groups[group][lane, state] += 1
    rows = []
    for item, counts in groups.items():
        for lane in sorted(lanes):
            for state in STATES:
                value = counts[lane, state] if lane else sum(n for (_, found), n in counts.items() if found == state)
                path = {**known.get(item, {}), "lane": lane, "state": state}
                rows.append(snapshot_row(slug, now_ms, "tasks", item, path, value))
    return rows


def age_rows(slug: str, now_ms: int, doc: dict, known: dict, births: dict) -> list:
    rows = []
    for collection, measure in (("priorities", "priority"), ("questions", "question"), ("followups", "followup")):
        for item in doc.get(collection, []):
            answered = any(not answer.get("deleted") for answer in item.get("answers", []))
            if item.get("out_of_scope") or item.get("done") or answered:
                continue
            target = item["item"] if collection == "priorities" else f"{collection}/{item['id']}"
            at = item.get("at") or births.get(target)
            age = max(0, now_ms - at) / 1000 if at is not None else -1.0
            rows.append(snapshot_row(slug, now_ms, measure, target, known.get(target, {}), age))
    return rows


def snapshot_rows(slug: str, now_ms: int, doc: dict, nodes: list, known: dict, births: dict) -> list:
    rows = count_rows(slug, now_ms, nodes, known)
    rows += [
        snapshot_row(slug, now_ms, "nodes", node["node"], {**known[node["node"]], "state": node["state"]}, 1)
        for node in nodes
    ]
    rows += age_rows(slug, now_ms, doc, known, births)
    minutes = doc.get("time_left_minutes")
    rows.append(snapshot_row(slug, now_ms, "time_left", "", {}, -1.0 if minutes is None else minutes))
    for freeze in doc.get("freezes", []):
        target = freeze["target"]
        path = {**known.get(target, {}), "state": freeze["verb"]}
        if target.startswith("lane:"):
            path["lane"] = target.removeprefix("lane:")
        rows.append(snapshot_row(slug, now_ms, "freeze", target, path, 1))
    return rows


def snapshot_nodes(doc: dict) -> list:
    projected, _ = hierarchy.project(doc)
    items = {f"{collection}/{item['id']}": item for collection in hierarchy.KINDS for item in doc.get(collection, [])}
    return [
        {"node": node, "kind": kind, "parent": parent, "state": node_state(kind, items[node], doc)}
        for node, (kind, parent, _) in projected.items()
    ]


def record(box: metrics_outbox.Outbox, slug: str, now_ms: int, ledger: LedgerClient) -> None:
    doc = ledger.state(slug)
    nodes = snapshot_nodes(doc)
    box.db.execute(CHECKPOINT)
    box.db.execute(PATHS)
    box.db.execute(BIRTHS)
    checkpoint = box.db.execute(READ_CHECKPOINT, (slug,)).fetchone()
    cursor, snapshot = (None, None) if checkpoint is None else checkpoint
    known = {node: json.loads(value) for node, value in box.db.execute(READ_PATHS, (slug,))}
    current = paths(nodes, doc.get("tasks", []))
    known.update(current)
    events = doc["_meta"]["events"]
    births = dict(box.db.execute(READ_BIRTHS, (slug,)))
    for event in events:
        if event["kind"] == "added":
            births.setdefault(event["target"], event["at"])
    box.append(EVENTS, event_rows(slug, events, known, cursor, now_ms, doc["_meta"].get("events_trimmed")))
    if snapshot is None or now_ms - snapshot >= SNAPSHOT_MS:
        box.append(SNAPSHOTS, snapshot_rows(slug, now_ms, doc, nodes, current, births))
        snapshot = now_ms
    with box.db:
        box.db.executemany(SAVE_PATHS, [(slug, node, json.dumps(path)) for node, path in current.items()])
        box.db.executemany(SAVE_BIRTHS, [(slug, node, at) for node, at in births.items()])
        box.db.execute(SAVE_CHECKPOINT, (slug, doc["_meta"]["rev"], snapshot))
    if any(event["rev"] > doc["_meta"].get("events_ack", -1) for event in events):
        ledger.ack_events(slug, doc["_meta"]["rev"])
