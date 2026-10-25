"""Complete session evidence comes from catalog rows and verified archive objects; a preview is never complete."""

import hashlib
import os
import re
import tempfile
from pathlib import Path

from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog

LEGACY_PREVIEW_CHARS = 4000
MISSING = "an archived object is missing; the evidence is incomplete and was not returned"
ALTERED = "an archived object does not match its digest or range; the evidence is incomplete and was not returned"
UNCOVERED = "the requested range is not covered by archived objects; the evidence is incomplete and was not returned"
FORBIDDEN = "the grant does not cover this session"
UNAVAILABLE = "the session is not retained"
BAD_NAME = "an object name must be a sha256 hex digest"
BAD_RANGE = "a byte range must start at zero or later and end at or after its start"
DIGEST = re.compile(r"[0-9a-f]{64}")


class EvidenceIntegrityError(Exception):
    pass


class EvidenceRefused(Exception):
    pass


def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ObjectStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, digest: str) -> Path:
        if not DIGEST.fullmatch(digest):
            raise ValueError(BAD_NAME)
        return self.root / digest[:2] / digest

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            Path(temporary).replace(path)
        except BaseException:
            Path(temporary).unlink()
            raise
        _sync(path.parent)
        return digest

    def get(self, digest: str) -> bytes:
        path = self._path(digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise EvidenceIntegrityError(MISSING) from None
        if hashlib.sha256(data).hexdigest() != digest:
            raise EvidenceIntegrityError(ALTERED)
        return data


def _verified(store, entry: dict) -> bytes:
    data = store.get(entry["object_digest"])
    if len(data) != entry["byte_end"] - entry["byte_start"]:
        raise EvidenceIntegrityError(ALTERED)
    return data


def _readable(catalog, grant, domain: str, session_uid: str) -> tuple[dict, list[dict]]:
    if domain != grant.corpus:
        raise EvidenceRefused(FORBIDDEN)
    found = catalog.session(domain, session_uid)
    if found is None or found["state"] != "retained":
        raise EvidenceRefused(UNAVAILABLE)
    sources = []
    for source in catalog.sources(domain, session_uid):
        events = catalog.events(domain, session_uid, source["source_generation"])
        if any(event["project_id"] not in (None, *grant.projects) for event in events):
            raise EvidenceRefused(FORBIDDEN)
        sources.append({**source, "events": events})
    return found, sources


def _counted(catalog, domain: str, read):
    try:
        return read()
    except EvidenceIntegrityError:
        catalog.integrity_failure(domain)
        raise


def _covered(entries: list[dict], events: list[dict]) -> None:
    cursor = 0
    for entry in entries:
        if entry["byte_start"] > cursor:
            raise EvidenceIntegrityError(UNCOVERED)
        cursor = max(cursor, entry["byte_end"])
    if any(event["byte_end"] > cursor for event in events):
        raise EvidenceIntegrityError(UNCOVERED)


def evidence(catalog: Catalog, store: ObjectStore, grant: Grant, domain: str, session_uid: str) -> dict:
    found, sources = _readable(catalog, grant, domain, session_uid)

    def read():
        for source in sources:
            generation = source["source_generation"]
            source["quarantined"] = catalog.quarantined(domain, session_uid, generation)
            source["archive"] = [
                {**entry, "byte_size": len(_verified(store, entry))}
                for entry in catalog.manifest(domain, session_uid, generation)
            ]
            _covered(source["archive"], source["events"])
        return sources

    verified_sources = _counted(catalog, domain, read)
    return {
        "complete": True,
        "access_domain": domain,
        "session_uid": session_uid,
        **found,
        "sources": verified_sources,
    }


def _slice(catalog, store, reference: dict) -> bytes:
    start, end = reference["byte_start"], reference["byte_end"]
    if not 0 <= start <= end:
        raise ValueError(BAD_RANGE)
    scope = (reference["access_domain"], reference["session_uid"], reference["source_generation"])
    parts, cursor = [], start
    for entry in catalog.manifest(*scope):
        if cursor >= end:
            break
        if entry["byte_end"] <= cursor:
            continue
        if entry["byte_start"] > cursor:
            raise EvidenceIntegrityError(UNCOVERED)
        stop = min(end, entry["byte_end"])
        parts.append(_verified(store, entry)[cursor - entry["byte_start"] : stop - entry["byte_start"]])
        cursor = stop
    if cursor < end:
        raise EvidenceIntegrityError(UNCOVERED)
    return b"".join(parts)


def source_range(catalog: Catalog, store: ObjectStore, grant: Grant, reference: dict) -> bytes:
    domain = reference["access_domain"]
    _readable(catalog, grant, domain, reference["session_uid"])
    return _counted(catalog, domain, lambda: _slice(catalog, store, reference))


def preview(found: dict, limit: int = LEGACY_PREVIEW_CHARS) -> dict:
    text = "\n".join(
        event["record"]["text"]
        for source in found["sources"]
        for event in source["events"]
        if "text" in event["record"]
    )
    return {
        "complete": False,
        "access_domain": found["access_domain"],
        "session_uid": found["session_uid"],
        "text": text[:limit],
        "omitted_chars": max(len(text) - limit, 0),
    }
