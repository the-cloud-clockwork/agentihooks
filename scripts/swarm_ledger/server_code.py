"""The ledger server's code folders and the code it loaded: a modification stamp and a digest of the content."""

import hashlib
import json
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
ROOT = CODE_DIR.parents[1]
CODE_DIRS = (
    CODE_DIR,
    *(ROOT / "scripts" / name for name in ("inbox", "swarm", "swarm_v2", "handoff", "doctor", "gates", "hive")),
    ROOT / "hooks",
)
SUFFIXES = (".py", ".html", ".js", ".css")
RECORD = ".server.code"


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def code_stamp(code_dirs: tuple[Path, ...] = CODE_DIRS) -> int:
    return max((_mtime(p) for d in code_dirs for p in d.rglob("*") if p.suffix in SUFFIXES), default=0)


def code_digest(code_dirs: tuple[Path, ...] = CODE_DIRS) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for d in code_dirs for p in d.rglob("*") if p.suffix in SUFFIXES):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest.update(f"{path}\0{len(content)}\0".encode())
        digest.update(content)
    return digest.hexdigest()


def loaded_code(code_dirs: tuple[Path, ...] = CODE_DIRS) -> dict:
    return {"stamp": code_stamp(code_dirs), "digest": code_digest(code_dirs)}


def same(held: dict, code_dirs: tuple[Path, ...]) -> bool:
    """Whether the code on disk has the content held names; file times that moved alone move held's stamp."""
    stamp = code_stamp(code_dirs)
    if stamp == held.get("stamp"):
        return True
    if code_digest(code_dirs) != held.get("digest"):
        return False
    held["stamp"] = stamp
    return True


def record(folder: Path, pid: int, held: dict, code_dirs: tuple[Path, ...] = CODE_DIRS) -> None:
    written = folder / f"{RECORD}.{pid}"
    code = {"stamp": held["stamp"], "digest": held["digest"]}
    written.write_text(json.dumps({"pid": pid, **code, "dirs": [str(d) for d in code_dirs]}))
    written.replace(folder / RECORD)


def loaded(folder: Path) -> dict | None:
    try:
        held = json.loads((folder / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return held if isinstance(held, dict) else None


def recorded(folder: Path, pid: int) -> bool:
    held = loaded(folder)
    return held is not None and held.get("pid") == pid


def stale(folder: Path, pid: int) -> bool:
    held = loaded(folder)
    if held is None or held.get("pid") != pid:
        return True
    dirs = tuple(Path(d) for d in held.get("dirs", ()))
    stamp = held.get("stamp")
    if not same(held, dirs):
        return True
    if held["stamp"] != stamp:
        record(folder, pid, held, dirs)
    return False
