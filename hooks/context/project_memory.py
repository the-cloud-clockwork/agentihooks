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
    revision: str = ""


class ScopedFeedRefused(RuntimeError):
    pass


LEGACY_FEED_KEYS = ("hot_arcs", "inject_blocks", "entries")


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


def _own_rows(found: dict, kind: str, project_id: str) -> list[dict]:
    rows = found.get(kind)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("projects") == [project_id]]


def _kernel_memory(identity: ProjectIdentity, found: dict) -> ProjectMemory:
    if found.get("scope") != "project" or found.get("project_id") != identity.project_id:
        raise ScopedFeedRefused("project feed answered outside the requested project")
    arcs = [
        {
            "id": row["ref"][4:],
            "heat": row.get("heat", 0),
            "status": row.get("status", ""),
            "title": row.get("title", ""),
        }
        for row in _own_rows(found, "arcs", identity.project_id)
        if str(row.get("ref")).startswith("arc:")
    ]
    lessons = [str(row.get("content", "")).strip() for row in _own_rows(found, "lessons", identity.project_id)]
    return ProjectMemory(
        identity.project, arcs, [lesson for lesson in lessons if lesson], str(found.get("revision", ""))
    )


class KernelProjectSource:
    def __init__(self, entries: list[BrainEntry]):
        self.entries = entries
        self.fell_back = False

    def fetch(self, identity: ProjectIdentity) -> ProjectMemory:
        if identity.project_id == "unknown":
            return ProjectMemory(identity.project)
        from hooks._brain_http import get

        found = get("/feed", params={"project": identity.project_id}, surface_http_errors=True)
        if found is None:
            raise BrainSourceUnavailable("project feed unavailable")
        status = found.get("__http_status__")
        if status in (401, 403):
            raise ScopedFeedRefused(f"project feed refused with {status}")
        if status:
            raise BrainSourceUnavailable(f"project feed answered {status}")
        if "scope" not in found and any(key in found for key in LEGACY_FEED_KEYS):
            self.fell_back = True
            return VaultProjectSource(self.entries).fetch(identity)
        return _kernel_memory(identity, found)
