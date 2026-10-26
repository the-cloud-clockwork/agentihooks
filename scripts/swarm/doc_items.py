import argparse
import json
import re
from pathlib import Path

from scripts.swarm.ledger_client import LedgerClient


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


def sync(ledger: LedgerClient, slug: str, mapping: dict[str, list[str]]) -> list[str]:
    tasks = {task["id"]: task for task in ledger.tasks(slug)}
    missing = sorted(mapping.keys() - tasks.keys())
    if missing:
        raise ValueError(f"Unknown ledger tasks: {', '.join(missing)}")
    changed = []
    for tid, items in mapping.items():
        previous = tasks[tid].get("doc_items") or []
        merged = list(dict.fromkeys([*previous, *items]))
        if merged != previous:
            ledger.update_task(slug, tid, {"doc_items": merged})
            changed.append(tid)
    return changed


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
