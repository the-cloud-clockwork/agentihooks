"""Ledger JSON and HTML files left in the ledger folder: each is imported once, after a verified backup, then set aside."""

import fcntl
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

import ledger_core as core

from .sqlite import BEGIN_IMMEDIATE, STORED

BACKUP = ".imported"
REGISTRIES = {"bin": ".bin.json", "restored": ".bin-restored.json"}
FAILED = {}


@contextmanager
def storage_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".ledger-storage.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_state(json_path, seed, core=core):
    if json_path.exists():
        state = core.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("_meta"), dict) or "rev" not in state["_meta"]:
            raise ValueError(f"{json_path} has no ledger _meta")
        meta = state.pop("_meta")
        meta.setdefault("events", [])
        return core.normalize(state), meta, False
    if seed is None:
        raise ValueError(f"{json_path} is missing and the HTML seed is unreadable")
    return fresh(seed, core)


def fresh(seed, core=core):
    """A document and meta as a brand new ledger starts, before its first sync."""
    doc = {k: v for k, v in core.normalize(seed).items() if k not in ("_rev", "notifications")}
    doc["phases"] = [{k: v for k, v in phase.items() if k != "review"} for phase in doc["phases"]]
    return doc, {"rev": 0, "stamps": {}, "events": [], "updated_at": core.now_ms()}, True


def files(directory: Path, slug: str) -> list:
    return [path for path in (directory / f"{slug}.json", directory / f"{slug}.html") if path.exists()]


def backup(directory: Path, name: str, found: list) -> Path:
    folder = directory / BACKUP / f"{name}-{core.now_ms()}-{os.getpid()}"
    folder.mkdir(parents=True)
    for path in found:
        copied = Path(shutil.copy2(path, folder / path.name))
        if copied.read_bytes() != path.read_bytes():
            raise OSError(f"backup of {path.name} does not match its source")
    return folder


def import_files(repository, slug: str, found: list) -> None:
    html_path, json_path = repository.directory / f"{slug}.html", repository.directory / f"{slug}.json"
    page = html_path.read_text(encoding="utf-8") if html_path in found else ""
    doc, meta, created = load_state(json_path, core.parse_seed(page) if page else None)
    if created:
        repository.create_document(slug, doc, meta, core.read_token(page))
    else:
        repository.import_document(slug, {**doc, "_meta": meta}, core.read_token(page))


def adopt_registries(repository) -> None:
    for name, filename in REGISTRIES.items():
        path = repository.directory / filename
        if not path.exists():
            continue
        entries = registry_file(path)
        backup(repository.directory, name, [path])
        with repository.connect() as connection, connection:
            connection.execute(BEGIN_IMMEDIATE)
            current = repository.registry(name, connection)
            repository.save_registry(connection, name, {**entries, **current})
        path.unlink()


def registry_file(path: Path) -> dict:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return entries if isinstance(entries, dict) else {}


def stored(repository, slug: str) -> bool:
    with repository.connect() as connection:
        return connection.execute(STORED, (slug,)).fetchone() is not None


def adopt_bin(repository) -> None:
    """Import the bin and restore mark files left in the ledger folder."""
    if any((repository.directory / filename).exists() for filename in REGISTRIES.values()):
        with storage_lock(repository.directory):
            adopt_registries(repository)


def candidates(directory: Path, slug: str | None) -> list:
    """Legacy ledgers to import: the one named, or every one when None."""
    if slug is not None:
        return [slug] if core.SLUG_RE.match(slug) and files(directory, slug) else []
    names = {path.stem for pattern in ("*.json", "*.html") for path in directory.glob(pattern)}
    return sorted(name for name in names if core.SLUG_RE.match(name))


def adopt(repository, slug: str | None = None) -> None:
    """Import every legacy ledger file present for `slug` (all when None), keeping a verified copy of each."""
    directory = repository.directory
    if not directory.is_dir():
        return
    pending = candidates(directory, slug)
    registries = any((directory / filename).exists() for filename in REGISTRIES.values())
    if not pending and not registries:
        return
    with storage_lock(directory):
        if registries:
            adopt_registries(repository)
        for name in pending:
            found = files(directory, name)
            if not found:
                continue
            try:
                import_once(repository, name, found)
            except Repeated:
                if slug is not None:
                    raise
                continue
            except (OSError, ValueError) as exc:
                if slug is not None:
                    raise
                sys.stderr.write(f"legacy ledger {name} not imported: {exc}\n")
                continue
            for path in found:
                path.unlink()


class Repeated(ValueError):
    pass


def import_once(repository, name: str, found: list) -> None:
    """Back up and import one ledger's files; files that failed once are refused until they change."""
    mark = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in found)
    if mark in FAILED:
        raise Repeated(FAILED[mark])
    try:
        backup(repository.directory, name, found)
        if stored(repository, name):
            sys.stderr.write(f"legacy ledger {name} is already stored; its files were set aside in {BACKUP}\n")
        else:
            import_files(repository, name, found)
    except (OSError, ValueError) as exc:
        FAILED[mark] = f"{name} was refused before: {exc}"
        raise


def create(repository, slug: str, content: dict, size: str = "small") -> bool:
    import new_ledger

    if repository.exists(slug):
        return False
    errors = new_ledger.check(content)
    if errors:
        sys.exit("content rejected:\n  " + "\n  ".join(errors))
    doc = new_ledger.build_doc(content, size)
    core.validate(doc)
    doc, meta, _ = fresh(doc)
    return repository.create_document(slug, doc, meta)
