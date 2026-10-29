LIMIT = 1000


def find(meta: dict, operation_id: str) -> tuple[str | None, dict | None]:
    for slot, record in meta.get("api_operations", {}).items():
        if record.get("operation_id", slot) == operation_id:
            return slot, record
    return None, None


def slots(stored: dict) -> dict:
    if all("operation_id" in record for record in stored.values()):
        return stored
    legacy = list(stored.items())[-LIMIT:]
    return {str(seq): {"seq": seq, "operation_id": key, **record} for seq, (key, record) in enumerate(legacy)}


def store(meta: dict, operation_id: str, digest: str, results: dict) -> None:
    ring = meta["api_operations"] = slots(meta.get("api_operations", {}))
    slot, record = find(meta, operation_id)
    if record is None:
        seq = max((record["seq"] for record in ring.values()), default=-1) + 1
        slot, record = str(seq % LIMIT), {"seq": seq, "operation_id": operation_id}
    ring[slot] = {**record, "digest": digest, "results": results}
