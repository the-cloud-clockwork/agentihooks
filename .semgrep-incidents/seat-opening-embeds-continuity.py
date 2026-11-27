from typing import Any


def handoff_lines(task: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    # ruleid: seat-opening-embeds-continuity
    lines += ["Handoff document:", task["handoff"]]
    return lines


def priming_lines(task: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    # ruleid: seat-opening-embeds-continuity
    lines.append(task["handoff"])
    return lines


def priming(slug: str, transfer: dict[str, Any]) -> str:
    # ruleid: seat-opening-embeds-continuity
    return f"Confirm the handoff for {slug}\n{transfer['handoff']}"


def render(task: dict[str, Any]) -> str:
    # ok: seat-opening-embeds-continuity
    return f"Handoff:\n{task['handoff']}"


def compact(task: dict[str, Any]) -> list[str]:
    # ok: seat-opening-embeds-continuity
    return [task["handoff_envelope"], "Run agentihooks seat brief"]
