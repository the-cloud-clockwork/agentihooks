import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from hooks.context.brain_adapter import BrainEntry, BrainSourceUnavailable, _parse_frontmatter
from hooks.context.project_identity import ProjectIdentity
from hooks.context.project_sessions import lookup


@dataclass
class ProjectMemory:
    project: str
    arcs: list[dict] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)


class ProjectMemorySource(Protocol):
    def fetch(self, identity: ProjectIdentity) -> ProjectMemory: ...


def _arc_rows(entries: list[BrainEntry]) -> list[dict]:
    from hooks.config import BRAIN_HOT_ARCS_TOP_N

    rows = {}
    for entry in entries:
        if not entry.id.startswith("hot-arcs"):
            continue
        for line in entry.content.splitlines():
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if len(cells) < 5 or not cells[1].isdigit():
                continue
            rows[cells[0]] = {"id": cells[0], "heat": int(cells[1]), "status": cells[3], "title": cells[4]}
    return sorted(rows.values(), key=lambda arc: (-arc["heat"], arc["id"]))[:BRAIN_HOT_ARCS_TOP_N]


def _read_arc(row: dict) -> dict | None:
    from hooks._brain_http import get

    found = get("/vault/search", params={"q": f"cluster_id: {row['id']}", "limit": 10, "context_lines": 0})
    if found is None:
        raise BrainSourceUnavailable("project arc lookup unavailable")
    for hit in found.get("results", []):
        path = hit.get("path", "")
        if path.rsplit("/", 1)[-1] != row["id"] + ".md":
            continue
        result = get("/vault/read", params={"path": path})
        if result is None:
            raise BrainSourceUnavailable("project arc read unavailable")
        attrs, _ = _parse_frontmatter(result.get("content", ""))
        if attrs.get("cluster_id") == row["id"]:
            return attrs
    return None


def _lessons(identity: ProjectIdentity) -> list[str]:
    from hooks._brain_http import get

    lessons = []
    today = datetime.now(timezone.utc).date()
    days = int(os.getenv("BRAIN_STALE_LESSON_DAYS", "14"))
    for age in range(max(0, days)):
        path = f"left/reference/lessons-{today - timedelta(days=age)}.md"
        result = get("/vault/read", params={"path": path})
        if not result:
            continue
        sections = re.split(r"^## ", result.get("content", ""), flags=re.MULTILINE)[1:]
        for section in reversed(sections):
            header, _, body = section.partition("\n")
            sessions = re.findall(r"`([^`]+)`", header)
            owner = lookup(sessions[-1]) if sessions else None
            if (
                owner
                and owner.repo == identity.repo
                and (not owner.remote or not identity.remote or owner.remote == identity.remote)
                and body.strip()
            ):
                lessons.append(body.strip())
    return lessons


class VaultProjectSource:
    def __init__(self, entries: list[BrainEntry]):
        self.entries = entries

    def fetch(self, identity: ProjectIdentity) -> ProjectMemory:
        memory = ProjectMemory(identity.project)
        for row in _arc_rows(self.entries):
            attrs = _read_arc(row)
            if attrs and attrs.get("project") in {identity.project, identity.repo}:
                memory.arcs.append(
                    {**row, **{key: attrs.get(key, "") for key in ("title", "summary", "status", "updated", "created")}}
                )
        memory.lessons = _lessons(identity)
        return memory
