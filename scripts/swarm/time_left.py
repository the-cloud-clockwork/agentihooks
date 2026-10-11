"""Each tick sends the ledger its live slots and CI median, so time left and its refresh age stay current."""

from scripts.swarm import capacity, ci_speed


def slots(decision: dict) -> int | None:
    if not decision:
        return None
    lanes = decision["configured"]
    running = sum(decision["effective"][lane] - sum(decision["allocation"][lane].values()) for lane in lanes)
    return min(sum(lanes.values()), running + sum(decision["placeable"].values()))


def inputs_of(store, slug, runtime) -> dict:
    observed = getattr(runtime, "quota_capacity", None) is not None
    cached = ci_speed.get(store.redis, slug) or {}
    return {"slots": slots(capacity.read(store, slug)) if observed else None, "ci_minutes": cached.get("minutes")}


def refresh(slug, store, ledger, runtime, fix_phase="", watched=None) -> list[str]:
    writer = getattr(ledger, "time_left", None)
    if writer is not None:
        inputs = inputs_of(store, slug, runtime)
        focus = {key: value for key, value in (("fix_phase", fix_phase), ("watched", watched)) if value}
        writer(slug, inputs["slots"], inputs["ci_minutes"], **focus)
    return []
