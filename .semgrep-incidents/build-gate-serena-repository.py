from pathlib import Path

from scripts.gates.base import Call
from scripts.gates.build import NO_VALUE, SERENA, _area
from scripts.swarm import naming


def missing_repository(call: Call, folder: Path) -> list[tuple[str, str]]:
    if call.tool.startswith(SERENA):
        scope = call.tool_input.get("relative_path") or "**"
        # ruleid: build-gate-serena-repository
        return [] if call.tool_input.get("dry_run") else [(NO_VALUE, _area(scope))]
    return []


def empty_repository(call: Call, folder: Path) -> list[tuple[str, str]]:
    if call.tool.startswith(SERENA):
        scope = call.tool_input.get("relative_path") or "**"
        # ruleid: build-gate-serena-repository
        return [("", _area(scope))]
    return []


def resolved_repository(call: Call, folder: Path) -> list[tuple[str, str]]:
    if call.tool.startswith(SERENA):
        scope = call.tool_input.get("relative_path") or "**"
        repository = naming.repo_name(folder)
        # ok: build-gate-serena-repository
        return [] if call.tool_input.get("dry_run") else [(repository, _area(scope))]
    return []
