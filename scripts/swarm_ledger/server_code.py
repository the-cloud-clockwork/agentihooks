"""The ledger server's code folders and the code stamp it loaded."""

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

CODE_DIR = Path(__file__).resolve().parent
ROOT = CODE_DIR.parents[1]
CODE_DIRS = (
    CODE_DIR,
    *(
        ROOT / "scripts" / name
        for name in (
            "inbox",
            "swarm",
            "swarm_v2",
            "handoff",
            "doctor",
            "gates",
            "hive",
            "codex_session",
            "scheduler",
            "events",
            "hub",
            "workflows",
        )
    ),
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
        digest.update(str(path).encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def record(
    folder: Path,
    pid: int,
    stamp: int,
    code_dirs: tuple[Path, ...] = CODE_DIRS,
    digest: str | None = None,
    files: dict[str, str] | None = None,
) -> None:
    written = folder / f"{RECORD}.{pid}"
    held = {"pid": pid, "stamp": stamp, "dirs": [str(d) for d in code_dirs], "digest": digest or code_digest(code_dirs)}
    if files is not None:
        held["files"] = files
        held["digest"] = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    written.write_text(json.dumps(held))
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
    if "files" in held:
        return any(file_digest(Path(path)) != digest for path, digest in held["files"].items())
    dirs = tuple(Path(d) for d in held.get("dirs", ()))
    return code_stamp(dirs) != held.get("stamp") and code_digest(dirs) != held.get("digest")


def file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class LoadedCode:
    def __init__(self, root: Path = ROOT, modules: Mapping[str, ModuleType | None] = sys.modules) -> None:
        self.root = root.resolve()
        self.modules = modules
        self.files: dict[str, str] = {}
        self.capture()

    def capture(self) -> None:
        for module in tuple(self.modules.values()):
            name = getattr(module, "__file__", None)
            if name is None:
                continue
            path = Path(name).resolve()
            if path.suffix != ".py" or not path.is_relative_to(self.root) or str(path) in self.files:
                continue
            digest = file_digest(path)
            if digest is not None:
                self.files[str(path)] = digest

    def changed(self) -> bool:
        self.capture()
        return any(file_digest(Path(path)) != digest for path, digest in self.files.items())
