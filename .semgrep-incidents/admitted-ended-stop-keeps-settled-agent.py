from typing import Any


# ruleid: admitted-ended-stop-keeps-settled-agent
def _stop_ended(slug: str, agent: Any, stop_admitted: Any) -> list[str]:
    outcome = stop_admitted(slug, agent, "ended")
    if outcome == "sent":
        return [f"sent the stop for {agent.name}"]
    if outcome.startswith("failed"):
        return [f"could not send the stop for {agent.name}"]
    return []


# ruleid: admitted-ended-stop-keeps-settled-agent
def stop_when_ended(slug: str, agent: Any, reconciler: Any) -> list[str]:
    answer = reconciler.stop_admitted(slug, agent, "ended")
    return [answer] if answer == "sent" else []


# ok: admitted-ended-stop-keeps-settled-agent
def _stop_ended_settled(judging: Any, agent: Any, stop_admitted: Any, settled_admitted: Any, tick: Any) -> list[str]:
    outcome = stop_admitted(judging.slug, agent, "ended")
    if outcome == "sent":
        return [f"sent the stop for {agent.name}"]
    if outcome in {"ended", "unknown"} and settled_admitted(judging.slug, agent):
        return tick._retire_ended(judging.slug, agent)
    return []


# ok: admitted-ended-stop-keeps-settled-agent
def stop_ended_through_hub(judging: Any, agent: Any, reconciler: Any, tick: Any) -> list[str]:
    outcome = reconciler.stop_admitted(judging.slug, agent, "ended")
    if outcome != "sent" and reconciler.settled_admitted(judging.slug, agent):
        return tick._retire_ended(judging.slug, agent)
    return []


# ok: admitted-ended-stop-keeps-settled-agent
def _stop_hive(slug: str, agent: Any, stop_admitted: Any) -> str:
    return stop_admitted(slug, agent, "dropped")
