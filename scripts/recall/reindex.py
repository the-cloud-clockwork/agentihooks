import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from .ledger import COLLECTIONS, THREADS, extract_ledger
from .store import RecallStore


def ledger_dir(environ: Mapping[str, str]) -> Path:
    if environ.get("LEDGER_DIR"):
        return Path(environ["LEDGER_DIR"]).expanduser()
    return Path.home() / "development-ledger"


def binned(folder: Path) -> set[str]:
    try:
        found = json.loads((folder / ".bin.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {slug for slug, at in found.items() if isinstance(at, int)} if isinstance(found, dict) else set()


def _walk(item: dict, ref: str) -> list[str]:
    if item.get("deleted"):
        return [ref]
    return [
        found
        for thread, _ in THREADS
        for child in item.get(thread, [])
        for found in _walk(child, f"{ref}/{thread}/{child['id']}")
    ]


def deleted_refs(document: dict) -> list[str]:
    return [
        found
        for collection, _ in COLLECTIONS
        for item in document.get(collection, [])
        for found in _walk(item, f"{collection}/{item['id']}")
    ]


def reindex(store: RecallStore, folder: Path, home: Path, slugs: list[str], include_binned: bool) -> dict:
    hidden = set() if include_binned else binned(folder)
    result = {"indexed": {}, "skipped_binned": [], "unreadable": []}
    for slug in slugs:
        if slug in hidden:
            result["skipped_binned"].append(slug)
            continue
        swarm_slug = slug if (home / "swarm" / slug).is_dir() else ""
        try:
            document = json.loads((folder / f"{slug}.json").read_text(encoding="utf-8"))
            records = extract_ledger(slug, document, swarm_slug=swarm_slug)
            deleted = deleted_refs(document)
        except (OSError, ValueError, AttributeError, KeyError, TypeError):
            result["unreadable"].append(slug)
            continue
        result["indexed"][slug] = asdict(store.sync(f"ledger/{slug}", records, deleted))
    return result


def all_slugs(folder: Path) -> list[str]:
    return sorted(path.stem for path in folder.glob("*.json") if not path.name.startswith("."))
