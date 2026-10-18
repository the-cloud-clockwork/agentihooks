import hashlib
import json
import os
from pathlib import Path


def write(path: Path, document: dict) -> None:
    temporary = path.with_suffix(".pending")
    with temporary.open("w") as stream:
        json.dump(document, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def decode(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def read(path: Path) -> dict:
    try:
        return decode(path.read_bytes())
    except OSError:
        return {}


def context() -> tuple[Path, dict]:
    root = Path(os.environ["SWARM_SUPERVISION_DIR"])
    return root, read(root / "context.json")


def acknowledge(role: str, **fields: object) -> None:
    root, scope = context()
    write(root / f"{role}.json", {**scope, **fields})


def matches(value: dict, scope: dict) -> bool:
    return all(value.get(key) == expected for key, expected in scope.items())


def checkpoint(attempt: Path, acknowledgement: dict, scope: dict) -> str | None:
    if not matches(acknowledgement, scope) or acknowledgement.get("status") != "complete":
        return None
    relative = acknowledgement.get("manifest")
    if not isinstance(relative, str):
        return None
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        return None
    path = (attempt / relative).resolve()
    if not path.is_relative_to(attempt / "checkpoints") or not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != acknowledgement.get("sha256"):
        return None
    manifest = decode(raw)
    if not matches(manifest, scope) or manifest.get("status") != "complete":
        return None
    identifier = manifest.get("checkpoint_id")
    return identifier if isinstance(identifier, str) and identifier else None
