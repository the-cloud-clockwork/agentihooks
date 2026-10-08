import json
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from scripts.handoff.check import HEADINGS, MARKER
from scripts.handoff.transfers import list_transfers
from scripts.inbox.seats import SeatMemory, SeatRegistry, SwarmCulture, of_swarm
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore

from .chunker import _sections, chunk_body
from .models import RecallRecord


def _records(slug: str, ref: str, kind: str, source: dict, parent: str = "") -> list[RecallRecord]:
    metadata = source.get("metadata", {})
    prefix = json.dumps(metadata, sort_keys=True) + "\n\n" if metadata else ""
    return [
        RecallRecord(
            key=f"swarm/{slug}/{ref}#{index}",
            ledger_slug=slug,
            swarm_slug=slug,
            kind=kind,
            ref=ref,
            parent_ref=parent,
            author=source.get("author", ""),
            time=source.get("at", 0),
            title=source.get("title", kind),
            text=prefix + chunk,
            chunk_index=index,
        )
        for index, chunk in enumerate(chunk_body(source.get("text", "")) or [""])
    ]


def _handoff(slug: str, ref: str, row: dict) -> list[RecallRecord]:
    sections = []
    for part in _sections(row.get("handoff", "")):
        title = part.split("\n", 1)[0].removeprefix("## ").strip()
        if part.startswith("## ") and title in HEADINGS:
            sections.append((title, part))
        elif sections:
            heading, body = sections[-1]
            sections[-1] = (heading, body + part)
    if not sections:
        sections = [("Body", row.get("handoff", ""))]
    metadata = {key: value for key, value in row.items() if key != "handoff"}
    result = []
    for heading, body in sections:
        source = {
            "author": row.get("predecessor", ""),
            "at": row.get("at", 0),
            "title": heading,
            "text": body.replace(MARKER, "").strip(),
            "metadata": metadata,
        }
        section_ref = heading.lower().replace(" ", "-")
        result.extend(_records(slug, f"{ref}/{section_ref}", "handoff", source, ref))
    return result


def _current_handoffs(slug: str, store: RedisStore) -> list[RecallRecord]:
    prefix = store.key(slug, "handoff") + ":"
    result = []
    for key in sorted(store.redis.scan_iter(match=prefix + "*")):
        task = key[len(prefix) :]
        envelope = store.handoff_envelope(slug, task) or {}
        row = {
            "task": task,
            "seat": store.handoff_seat(slug, task),
            "predecessor": envelope.get("agent", ""),
            "successor": "",
            "at": int(datetime.fromisoformat(envelope["time"]).timestamp() * 1000) if envelope.get("time") else 0,
            "envelope": envelope,
            "handoff": store.handoff(slug, task),
        }
        result.extend(_handoff(slug, f"tasks/{task}/handoff", row))
    return result


def _seat_records(slug: str, registry: SeatRegistry) -> tuple[list[RecallRecord], set[str]]:
    prefix = registry.key("")
    addresses = {key[len(prefix) :].split(":", 1)[0] for key in registry.swarm_keys(slug) if key.startswith(prefix)}
    memory = SeatMemory(registry.redis)
    result, members = [], set(registry.agent_names(slug))
    for address in sorted(addresses):
        members.add(address)
        members.update(row["occupant"] for row in registry.history(address))
        for kind, rows in (("recap", memory.recaps(address)), ("learned", memory.learned(address))):
            for row in rows:
                members.add(row["occupant"])
                digest = sha256(row["text"].encode()).hexdigest()
                ref = f"seats/{address}/{kind}/{row['occupant']}/{row['at']}/{digest}"
                source = {
                    "author": row["occupant"],
                    "at": row["at"],
                    "text": row["text"],
                    "metadata": {"seat": address, **{key: value for key, value in row.items() if key != "text"}},
                }
                result.extend(_records(slug, ref, kind, source, f"seats/{address}"))
    return result, members


def _inbox_records(slug: str, registry: SeatRegistry, members: set[str]) -> list[RecallRecord]:
    inbox = InboxStore(registry.redis)
    prefix = inbox.key("item") + ":"
    result = []
    for key in sorted(registry.redis.scan_iter(match=prefix + "*")):
        item = inbox.get(key[len(prefix) :])
        if not any(
            address in members or of_swarm(address, slug, inbox.names) for address in (item.sender, item.address)
        ):
            continue
        source = {
            "author": item.sender,
            "at": item.created_at,
            "text": item.text,
            "metadata": {key: value for key, value in asdict(item).items() if key != "text"},
        }
        parent = f"tasks/{item.task}" if item.task else ""
        result.extend(_records(slug, f"inbox/{item.id}", "inbox", source, parent))
    return result


def _task_records(slug: str, document: dict, root: Path) -> list[RecallRecord]:
    folders = {path.name: path for path in root.iterdir() if path.is_dir()} if root.is_dir() else {}
    for task in document.get("tasks", []):
        folders[task["id"]] = Path(task["workspace"]) if task.get("workspace") else root / task["id"]
    result = []
    for task, folder in sorted(folders.items()):
        for kind in ("steering", "progress", "proof"):
            path = folder / f"{kind}.md"
            if path.is_file():
                source = {"text": path.read_text(), "at": path.stat().st_mtime_ns // 1_000_000}
                result.extend(_records(slug, f"tasks/{task}/{kind}", kind, source, f"tasks/{task}"))
    return result


def extract_swarm(
    slug: str,
    store: RedisStore,
    document: dict | None = None,
    *,
    task_root: Path | None = None,
) -> list[RecallRecord]:
    registry = SeatRegistry(store.redis)
    result, members = _seat_records(slug, registry)
    for row in list_transfers(store, slug):
        members.update(row.get(key, "") for key in ("seat", "predecessor", "successor"))
        result.extend(_handoff(slug, f"transfers/{row['id']}", row))
    members.update(store.execution_occupants(slug))
    result.extend(_current_handoffs(slug, store))
    culture = SwarmCulture(store.redis).get(slug)
    if culture:
        result.extend(_records(slug, "culture", "culture", {"text": culture}))
    result.extend(_inbox_records(slug, registry, members))
    root = task_root if task_root is not None else Path.home() / ".agentihooks" / "swarm" / slug / "tasks"
    result.extend(_task_records(slug, document or {}, root))
    return result
