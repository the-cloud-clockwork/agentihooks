from scripts.hive import registry
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
            "heartbeat_age_s": (now_ms - hive["heartbeat_at"]) / 1000 if hive["heartbeat_at"] > 0 else -1.0,
        }
        for hive in registry.hives(store.redis)
    ]


def record(box: metrics_outbox.Outbox, store, slug: str, now_ms: int) -> None:
    box.append(PROBES, probe_rows(store, slug))
    box.append(HIVES, hive_rows(store, slug, now_ms))
