import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from scripts.swarm.ledger_client import LedgerClient
from scripts.tick.deadline import Deadline
from scripts.tick.ports.tools import Tools

MARK = "doc-items-digest"
REVISION_MARK = "doc-items-copy-revision"
COPY_STAMP = "watchdog_copy"


def task_ids(tag: str) -> list[str]:
    tasks = []
    for part in tag.split(","):
        part = part.strip()
        span = re.fullmatch(r"t(\d+) to t(\d+)", part)
        if span and int(span[1]) <= int(span[2]):
            tasks.extend(f"t{number}" for number in range(int(span[1]), int(span[2]) + 1))
        elif re.fullmatch(r"t[\w-]+", part):
            tasks.append(part)
        else:
            raise ValueError(f"Invalid Ledger tag: {tag}")
    return tasks


def parse(document: str) -> dict[str, list[str]]:
    mapping = {}
    for line in document.splitlines():
        prefix, separator, tag = line.strip().partition("[Ledger:")
        item = re.fullmatch(r"(\d+)\. \[open\] (.+)", prefix.rstrip())
        if item and separator and tag.endswith("]"):
            text = f"{item[1]} {item[2].strip()}"
            for task in task_ids(tag[:-1]):
                links = mapping.setdefault(task, [])
                if text not in links:
                    links.append(text)
    return mapping


def changes(tasks: list[dict[str, Any]], mapping: dict[str, list[str]]) -> dict[str, list[str]]:
    """Return the doc items of each task in ``tasks`` that ``mapping`` adds to, refusing tasks the ledger lacks."""
    known = {task["id"]: task for task in tasks}
    missing = sorted(mapping.keys() - known.keys())
    if missing:
        raise ValueError(f"Unknown ledger tasks: {', '.join(missing)}")
    changed = {}
    for tid, items in mapping.items():
        previous = known[tid].get("doc_items") or []
        merged = list(dict.fromkeys([*previous, *items]))
        if merged != previous:
            changed[tid] = merged
    return changed


def sync(ledger: LedgerClient, slug: str, mapping: dict[str, list[str]]) -> list[str]:
    """Write the doc items ``mapping`` adds onto the tasks of swarm ``slug`` and return the updated task ids."""
    changed = changes(ledger.tasks(slug), mapping)
    for tid, merged in changed.items():
        ledger.update_task(slug, tid, {"doc_items": merged})
    return list(changed)


def digest(document: str) -> str:
    """Return the digest that tells one watchdog document copy from another."""
    return hashlib.sha256(document.encode()).hexdigest()


def map_changed(doc: dict[str, Any], tools: Tools, deadline: Deadline) -> list[str]:
    """Read the watchdog copy from the ledger only when the stamp of its last store in the held ledger view moved
    since the last read, map it with ``map_copy`` and record that stamp revision through the coordination port."""
    stamps: dict[str, dict[str, Any]] = doc.get("_meta", {}).get("stamps", {})
    revision = str((stamps.get(COPY_STAMP) or {}).get("rev", ""))
    if not revision or revision == tools.coordination.marked(REVISION_MARK, deadline=deadline):
        return []
    lines = map_copy(doc["tasks"], tools, deadline)
    tools.coordination.mark(REVISION_MARK, revision, deadline=deadline)
    return lines


def map_copy(tasks: list[dict[str, Any]], tools: Tools, deadline: Deadline) -> list[str]:
    """Map the newest watchdog document copy on the ledger onto ``tasks`` when its digest differs from the last
    mapped one, write the new items through the ledger port and record the digest through the coordination port;
    return the line naming the run."""
    text = tools.ledger.watchdog_copy(deadline=deadline).get("text") or ""
    if not text:
        return []
    found = digest(text)
    if found == tools.coordination.marked(MARK, deadline=deadline):
        return []
    try:
        mapping = parse(text)
        changed = changes(tasks, mapping)
    except ValueError as exc:
        tools.coordination.mark(MARK, found, deadline=deadline)
        return [f"refused watchdog doc items of the ledger copy: {exc}"]
    for tid, merged in changed.items():
        tools.ledger.update_task(tid, {"doc_items": merged}, deadline=deadline)
    tools.coordination.mark(MARK, found, deadline=deadline)
    items = sum(len(links) for links in mapping.values())
    updated = ", ".join(changed) or "none"
    return [f"mapped watchdog doc items of the ledger copy: {items} items on {len(mapping)} tasks, updated {updated}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Map watchdog document items to their ledger tasks")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--document", type=Path, required=True)
    args = parser.parse_args(argv)
    mapping = parse(args.document.read_text())
    changed = sync(LedgerClient(), args.slug, mapping)
    print(
        json.dumps(
            {
                "slug": args.slug,
                "tasks": len(mapping),
                "items": sum(len(items) for items in mapping.values()),
                "updated": changed,
                "mapping": mapping,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
