from pathlib import Path

from scripts.gates.base import Call
from scripts.gates.build import NO_VALUE, SERENA, _area
from scripts.swarm import naming


class MissingRepository:
    def touched(self, call: Call, folder: Path) -> list[tuple[str, str]]:
        if call.tool.startswith(SERENA):
            scope = call.tool_input.get("relative_path") or "**"
            # ruleid: build-gate-serena-repository
            return [] if call.tool_input.get("dry_run") else [(NO_VALUE, _area(scope))]
        return []


class EmptyRepository:
    def touched(self, call: Call, folder: Path) -> list[tuple[str, str]]:
        if call.tool.startswith(SERENA):
            scope = call.tool_input.get("relative_path") or "**"
            # ruleid: build-gate-serena-repository
            return [("", _area(scope))]
        return []


class ResolvedRepository:
    def touched(self, call: Call, folder: Path) -> list[tuple[str, str]]:
        if call.tool.startswith(SERENA):
            scope = call.tool_input.get("relative_path") or "**"
            repository = naming.repo_name(folder)
            # ok: build-gate-serena-repository
            return [] if call.tool_input.get("dry_run") else [(repository, _area(scope))]
        return []
