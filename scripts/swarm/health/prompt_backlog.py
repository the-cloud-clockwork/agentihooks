import json
from pathlib import Path

from scripts.swarm import timing
from scripts.swarm.health.findings import Finding
from scripts.swarm_v2.herdr import deliveries
from scripts.swarm_v2.herdr.endpoints import EndpointRefused

RISES = 3


def read(store, slug: str) -> list[int]:
    raw = store.redis.get(store.key(slug, "prompt-backlog"))
    return json.loads(raw) if raw else []


def observe(store, slug: str, now: int, path: Path | None = None) -> list[int]:
    samples = read(store, slug)
    try:
        pending = len(deliveries.read(path or deliveries.journal(), now)["pending"])
    except EndpointRefused:
        return samples
    if samples and samples[-1] == pending:
        return samples
    samples = [*samples, pending][-(RISES + 1) :]
    timing.keep()
    store.redis.set(store.key(slug, "prompt-backlog"), json.dumps(samples))
    return samples


def findings(store, slug: str) -> list[Finding]:
    samples = read(store, slug)
    if len(samples) <= RISES or any(before >= after for before, after in zip(samples, samples[1:])):
        return []
    trail = " to ".join(str(count) for count in samples)
    return [
        Finding(
            "prompt backlog",
            slug,
            "Remote prompts awaiting an outcome keep growing",
            (f"Pending prompts rose {trail}",),
            "three rises in a row",
            samples[-1],
        )
    ]
