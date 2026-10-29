"""Each agent's execution identity as the ledger page shows it, projected from the published status and the terminal
views at read time. The page only displays a card; it never derives authority from one."""

from collections.abc import Iterable, Mapping

LOCAL, KUBERNETES = "local", "kubernetes"
SCHEDULING, QUOTA, INPUT = "scheduling", "quota", "input"
UNOBSERVED, UNREADABLE, UNKNOWN = "unobserved", "unreadable", "unknown"
TERMINAL_SOURCE = "terminal"


def _ms(seconds) -> int:
    return int(round(float(seconds) * 1000)) if seconds else 0


def _seen_at(observation: Mapping) -> tuple[int, int | None]:
    sources = observation.get("sources") or {}
    times = {name: _ms(entry.get("observed_at")) for name, entry in sources.items() if name != TERMINAL_SOURCE}
    return max(times.values(), default=0), times.get("heartbeat")


def _observed(observation: Mapping) -> str:
    state = observation.get("state") or UNOBSERVED
    if state != "unclassified":
        return state
    beat = (observation.get("sources") or {}).get("heartbeat") or {}
    return beat.get("value") or UNOBSERVED


def _wait(agent: Mapping, observation: Mapping) -> str:
    failure, state = observation.get("failure"), observation.get("state")
    if failure == "pod_scheduling" or state == "pending":
        return SCHEDULING
    if failure == "provider_wait" or state == "waiting_quota":
        return QUOTA
    return INPUT if agent.get("status") == "waiting" else ""


def card(agent: Mapping, terminal: Mapping | None) -> dict:
    observation = agent.get("observation") or {}
    backend = agent.get("runtime_backend") or LOCAL
    seen_at, heartbeat_at = _seen_at(observation)
    checked_at = int((terminal or {}).get("checked_at") or 0)
    return {
        "agent": agent["name"],
        "backend": backend,
        "generation": int(agent.get("generation") or 0),
        "pool": (agent.get("profile") or "") if backend == KUBERNETES else "",
        "observed": _observed(observation),
        "wait": _wait(agent, observation),
        "seen_at": seen_at,
        "terminal_behind": bool(terminal) and bool(heartbeat_at) and checked_at < heartbeat_at,
    }


def cards(agents: Iterable[Mapping], terminals: Mapping[str, Mapping]) -> tuple[dict[str, dict], int]:
    built, errors = {}, 0
    for agent in agents:
        name = agent.get("name") if isinstance(agent, Mapping) else None
        if not name:
            errors += 1
            continue
        try:
            built[name] = card(agent, terminals.get(name))
        except (AttributeError, TypeError, ValueError):
            errors += 1
            built[name] = {
                **card({"name": name}, None),
                "backend": str(agent.get("runtime_backend") or UNKNOWN),
                "observed": UNREADABLE,
            }
    return built, errors
