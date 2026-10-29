"""Fleet dashboard samples from a recorded scaling canary run, joined with recorded pressure readings."""

from datetime import datetime

from scripts.swarm_v2.fleet_dashboard import ACTIVE, PENDING

OVERLAY_KEYS = ("quota", "archive", "checkpoints", "brain")


def stamp_ms(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp).timestamp() * 1000)


def run_samples(run: dict, overlay: dict, first_seq: int = 1) -> list[dict]:
    """One sample at the canary Pod's creation and one per recorded stage; the record carries no resource shape."""
    group, pod, node = run["classification"]["group"], run["snapshot"]["pod"], run["snapshot"]["node"]
    created, started, ready = stamp_ms(pod["created"]), stamp_ms(pod["started"]), stamp_ms(node["ready"])
    times = sorted({created, *(stamp_ms(stage["at"]) for stage in run["classification"]["stages"])})
    pressure = {key: overlay[key] for key in OVERLAY_KEYS}
    return [
        {
            "seq": seq,
            "at_ms": at,
            "pools": [{"pool": group["name"], "configured_maximum": group["max"], "nodes": int(at >= ready)}],
            "attempts": [
                {
                    "execution_id": run["run"],
                    "seat": "canary",
                    "task": "scaling-canary",
                    "phase": ACTIVE if at >= started else PENDING,
                    "pool": group["name"],
                    "permitted_pools": [group["name"]],
                    "pending_since_ms": created,
                    "protected": False,
                    "idle_since_ms": None,
                    "terminal": "unknown",
                    "authority": "unknown",
                }
            ],
            **pressure,
        }
        for seq, at in enumerate(times, first_seq)
    ]


def record_samples(record: dict, overlay: dict) -> dict[str, list[dict]]:
    """Each recorded run as its own trace, keyed by run name."""
    return {run["run"]: run_samples(run, overlay) for run in record["runs"]}
