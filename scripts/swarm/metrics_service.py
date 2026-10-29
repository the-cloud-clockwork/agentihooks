from scripts.hive import headroom, registry
from scripts.swarm import ledger_probe, metrics_outbox

PATH = dict.fromkeys(("plan", "phase", "slice", "task"), "")
PROBES = metrics_outbox.Table(
    "ledger_probes",
    (
        ("read_s", "Float64"),
        ("write_s", "Float64"),
        ("read_failure", "String"),
        ("write_failure", "String"),
        ("wall_s", "Float64"),
    ),
)
HIVES = metrics_outbox.Table(
    "hive_samples",
    (("hive", "String"), ("name", "String"), ("live", "Int64"), ("heartbeat_age_s", "Float64")),
)


ADMISSION = metrics_outbox.Table("admission_samples", (("outcome", "String"), ("total", "Int64")))
HEADROOM = metrics_outbox.Table(
    "hive_headroom",
    (
        ("hive", "String"),
        ("name", "String"),
        ("scope", "String"),
        ("resource", "String"),
        ("available", "Float64"),
        ("total", "Float64"),
        ("measured_at_ms", "Int64"),
    ),
)
RESOURCE_FIELDS = (
    ("host", "cpu", "cpus", "load5"),
    ("host", "memory", "total_mb", "available_mb"),
    ("pod", "cpu", "pod_cpus", "pod_load"),
    ("pod", "memory", "pod_total_mb", "pod_available_mb"),
)


def admission_rows(store, slug: str, now_ms: int) -> list[dict]:
    return [
        {
            "event_id": f"admission:{slug}:{now_ms}:{outcome}",
            "ledger": slug,
            "ts_ms": now_ms,
            **PATH,
            "outcome": outcome,
            "total": int(total),
        }
        for outcome, total in sorted(store.redis.hgetall(store.key(slug, "pending-admission-total")).items())
    ]


def headroom_rows(store, slug: str, now_ms: int) -> list[dict]:
    rows = []
    for hive in registry.hives(store.redis):
        reading = headroom.read(store.redis, hive["id"], now_ms)
        if reading is None:
            continue
        for scope, resource, total_key, value_key in RESOURCE_FIELDS:
            if total_key not in reading or value_key not in reading:
                continue
            total = float(reading[total_key])
            available = float(reading[value_key])
            if resource == "cpu":
                available = total - available
            rows.append(
                {
                    "event_id": f"headroom:{slug}:{hive['id']}:{now_ms}:{scope}:{resource}",
                    "ledger": slug,
                    "ts_ms": now_ms,
                    **PATH,
                    "hive": hive["id"],
                    "name": hive["name"],
                    "scope": scope,
                    "resource": resource,
                    "available": available,
                    "total": total,
                    "measured_at_ms": reading["measured_at"],
                }
            )
    return rows


def probe_rows(store, slug: str) -> list[dict]:
    samples = ledger_probe.state(store, slug).get("samples")
    if not samples:
        return []
    last = samples[-1]
    return [
        {
            "event_id": f"probe:{slug}:{last['at']}",
            "ledger": slug,
            "ts_ms": last["at"],
            **PATH,
            "read_s": float(last["read_s"]),
            "write_s": float(last["write_s"]),
            "read_failure": last["read_failure"],
            "write_failure": last["write_failure"],
            "wall_s": float(last["wall_s"]),
        }
    ]


def hive_rows(store, slug: str, now_ms: int) -> list[dict]:
    return [
        {
            "event_id": f"hive:{slug}:{hive['id']}:{now_ms}",
            "ledger": slug,
            "ts_ms": now_ms,
            **PATH,
            "hive": hive["id"],
            "name": hive["name"],
            "live": int(registry.live(hive, now_ms)),
            "heartbeat_age_s": max(now_ms - hive["heartbeat_at"], 0) / 1000 if hive["heartbeat_at"] > 0 else -1.0,
        }
        for hive in registry.hives(store.redis)
    ]


def record(box: metrics_outbox.Outbox, store, slug: str, now_ms: int) -> None:
    box.append(PROBES, probe_rows(store, slug))
    box.append(HIVES, hive_rows(store, slug, now_ms))
    box.append(ADMISSION, admission_rows(store, slug, now_ms))
    box.append(HEADROOM, headroom_rows(store, slug, now_ms))
