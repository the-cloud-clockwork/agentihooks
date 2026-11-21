from typing import Any

Slot: Any = None


def job_record(unit: Any) -> dict[str, Any]:
    # ruleid: lab-manifest-in-redis
    return {"kind": unit.kind, "key": unit.key, "manifest": unit.manifest}


def held_slot(unit: Any) -> Any:
    # ruleid: lab-manifest-in-redis
    return Slot(unit.kind, unit.key, list(unit.members), manifest=unit.manifest)


def restored_job(client: Any, job: str, manifest: str) -> None:
    # ruleid: lab-manifest-in-redis
    client.hset(job, "manifest", manifest)


def mapped_job(client: Any, job: str, manifest: str) -> None:
    # ruleid: lab-manifest-in-redis
    client.hset(job, mapping={"kind": "test", "manifest": manifest})


def slot_document(kind: str, text: str) -> dict[str, str]:
    # ruleid: lab-manifest-in-redis
    return {"kind": kind, "manifest": text}


def placed_slot(unit: Any) -> Any:
    # ok: lab-manifest-in-redis
    return Slot(unit.kind, unit.key, list(unit.members))


def leased_job(client: Any, job: str) -> None:
    # ok: lab-manifest-in-redis
    client.hdel(job, "manifest")


def bare_record(unit: Any) -> dict[str, Any]:
    # ok: lab-manifest-in-redis
    return {"kind": unit.kind, "key": unit.key}


def mapped_bare_job(client: Any, job: str) -> None:
    # ok: lab-manifest-in-redis
    client.hset(job, mapping={"kind": "test", "state": "pending"})
