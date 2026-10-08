"""Each tick sends the ledger the live slots and the CI median it computes time left from."""

from scripts.swarm import capacity, ci_speed


def slots(config, decision: dict) -> int:
    if not decision:
        return config.max_eng + config.max_ci + config.max_plan
    lanes = decision["configured"]
    running = sum(decision["effective"][lane] - sum(decision["allocation"][lane].values()) for lane in lanes)
    return min(sum(lanes.values()), running + sum(decision["placeable"].values()))


def refresh(slug, config, store, ledger) -> list[str]:
    writer = getattr(ledger, "time_left", None)
    if writer is None:
        return []
    cached = ci_speed.get(store.redis, slug) or {}
    writer(slug, slots(config, capacity.read(store, slug)), cached.get("minutes"))
    return []
