import json

from scripts.swarm.store import RedisStore


def rows(store: RedisStore, slug: str) -> dict[str, dict]:
    found = {}
    for execution_id, raw in store.redis.hgetall(store.key(slug, "attempt-recoveries")).items():
        decision = json.loads(raw)
        fence = json.loads(store.redis.hget(store.key(slug, "attempt-fences"), execution_id))
        offered = store.redis.hget(store.key(slug, "attempt-checkpoint-options"), execution_id)
        checkpoint = json.loads(offered) if offered else {}
        outcome = store.redis.hget(store.key(slug, "attempt-restore-outcomes"), decision["replacement"])
        restored = json.loads(outcome) if outcome else None
        if fence["seat"] in found and found[fence["seat"]]["generation"] > decision["generation"]:
            continue
        continuation = checkpoint.get("continuation")
        native = continuation != "context" and bool(fence["native_session"])
        reason = decision["reason"]
        if checkpoint:
            mode = "native resume" if native else "context continuation"
            reason += f"; {mode} from {checkpoint['checkpoint_id']}"
            if "watermark" in checkpoint:
                reason += f" at record {checkpoint['watermark']}"
        found[fence["seat"]] = {
            "generation": decision["generation"],
            "at": fence["fenced_at_ms"],
            "reason": reason,
            "checkpoint": checkpoint.get("checkpoint_id", ""),
            "resume_available": bool(checkpoint),
            "resume_label": "Resume native" if native else "Continue from context",
            "continuity": restored["recovery"] if restored else "pending",
        }
    return found
