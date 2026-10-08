import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

KEY_FORMAT = "2"
POLICY_VERSION = "1"
INSTALLATION_FILE = "installation.json"
DEFAULT_PORTS = {"http": 80, "https": 443}
KIND = re.compile(r"[a-z][a-z0-9-]{0,31}")
INSTALLATION_ID = re.compile(r"inst-[0-9a-f]{32}")
NAMESPACED = re.compile(r"k2-[a-z][a-z0-9-]{0,31}-[0-9a-f]{32}\.json")
REBUILDABLE = re.compile(r"k2-(?:feed|project-memory)-[0-9a-f]{32}\.json")
LEGACY = re.compile(r"feed\.json|(?:pending-)?[0-9a-f]{24}\.json")
MARKER_KEY = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class Installation:
    installation_id: str
    created_at: str


@dataclass(frozen=True)
class Namespace:
    installation: str
    brain: str
    project: str = ""
    policy: str = POLICY_VERSION
    generation: str = ""

    def document(self) -> dict:
        return {"format": KEY_FORMAT, **asdict(self)}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def instant(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def installation(home: Path, now: datetime | None = None) -> Installation:
    path = Path(home) / INSTALLATION_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        moment = now or datetime.now(timezone.utc)
        record = {"installation_id": f"inst-{secrets.token_hex(16)}", "created_at": moment.isoformat()}
        temp = path.with_name(f".{INSTALLATION_FILE}.{uuid.uuid4().hex}")
        temp.write_text(_canonical(record))
        try:
            os.link(temp, path)
        except FileExistsError:
            pass
        finally:
            temp.unlink()
    record = json.loads(path.read_text())
    if (
        not isinstance(record, dict)
        or not INSTALLATION_ID.fullmatch(str(record.get("installation_id")))
        or instant(record.get("created_at")) is None
    ):
        raise ValueError("Invalid installation record")
    return Installation(record["installation_id"], record["created_at"])


def brain_identity(url: str = "", path: str = "") -> str:
    if url:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if scheme not in DEFAULT_PORTS or not host:
            raise ValueError("Invalid brain URL")
        port = parts.port
        host = f"[{host}]" if ":" in host else host
        netloc = host if port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{port}"
        return "url-" + _digest(f"{scheme}://{netloc}{parts.path.rstrip('/')}")[:32]
    if path:
        return "file-" + _digest(str(Path(path).expanduser().resolve()))[:32]
    return "none"


def key(namespace: Namespace, kind: str, *parts: str) -> str:
    if not KIND.fullmatch(kind):
        raise ValueError("Invalid key kind")
    return f"k{KEY_FORMAT}-{kind}-{_digest(_canonical([namespace.document(), kind, list(parts)]))[:32]}"


def stamp(namespace: Namespace, kind: str, payload: dict) -> dict:
    return {**payload, "namespace": namespace.document(), "kind": kind}


def admits(document: object, namespace: Namespace, kind: str) -> bool:
    return (
        isinstance(document, dict)
        and document.get("namespace") == namespace.document()
        and document.get("kind") == kind
    )


def current(at: object, record: Installation) -> bool:
    moment = instant(at)
    return moment is not None and moment >= instant(record.created_at)


def legacy_marker_key(session_id: str, marker_type: str, content: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{session_id}-{marker_type}-{content}").hex


def marker_key(namespace: Namespace, session_id: str, marker_type: str, task: str, content: str) -> str:
    raw = _canonical([namespace.document(), "marker", session_id, marker_type, task, content])
    return uuid.uuid5(uuid.NAMESPACE_URL, f"k{KEY_FORMAT}:{raw}").hex


def _files(directory: Path, pattern: re.Pattern, limit: int | None) -> list[Path]:
    if not Path(directory).is_dir():
        return []
    found = sorted(path for path in Path(directory).iterdir() if path.is_file() and pattern.fullmatch(path.name))
    return found if limit is None else found[:limit]


def _remove(paths: list[Path]) -> int:
    removed = 0
    for path in paths:
        with suppress(FileNotFoundError):
            path.unlink()
            removed += 1
    return removed


def sweep_legacy(directory: Path, limit: int = 32) -> int:
    return _remove(_files(directory, LEGACY, limit))


def drop_namespaced(directory: Path) -> int:
    return _remove(_files(directory, REBUILDABLE, None))


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "drop":
        print("usage: python -m scripts.swarm_v2.keyspace drop <cache directory>", file=sys.stderr)
        return 2
    print(drop_namespaced(Path(argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
