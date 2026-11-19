from typing import Any

from scripts.swarm.live_binding import OBSERVED, assignment


def unguarded(agent: Any, facts: dict[str, Any]) -> dict[str, Any]:
    expected = assignment(agent)
    # ruleid: unobserved-process-unknown
    return {field: expected[field] for field in OBSERVED if expected[field] and not facts.get(field)}


def guarded(agent: Any, facts: dict[str, Any]) -> dict[str, Any]:
    if "process" in facts and facts["process"] is None:
        return {}
    expected = assignment(agent)
    # ok: unobserved-process-unknown
    return {field: expected[field] for field in OBSERVED if expected[field] and not facts.get(field)}
