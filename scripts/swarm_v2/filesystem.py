import json
import posixpath
import re
import shutil
import stat
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

LAYOUTS = (
    Path(__file__).resolve().parents[2] / "docker" / "swarm-node" / "layout.json",
    Path("/opt/swarm-node/layout.json"),
)
ROOTS = ("home", "runtime", "checkout", "worktree", "spool", "scratch", "seed")
SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SOCKET_BYTES = 107
MEMBERS = (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE)
FAILURES = {"execution_path_validation_failures": 0}


class LayoutError(ValueError):
    pass


def _refuse(message: str) -> NoReturn:
    FAILURES["execution_path_validation_failures"] += 1
    raise LayoutError(message)


@dataclass(frozen=True)
class Layout:
    version: int
    roots: dict[str, str]
    immutable: tuple[str, ...]


@dataclass(frozen=True)
class Execution:
    root: Path
    layout: Layout

    def path(self, name: str) -> Path:
        return self.root / self.layout.roots[name]


def _read_v1(document: dict) -> Layout:
    roots = document.get("roots")
    if not isinstance(roots, dict) or sorted(roots) != sorted(ROOTS):
        _refuse(f"layout must name exactly the roots {', '.join(ROOTS)}")
    for name, folder in roots.items():
        if not isinstance(folder, str) or not SEGMENT.fullmatch(folder):
            _refuse(f"layout root {name} must be one relative folder name")
    if len(set(roots.values())) != len(roots):
        _refuse("layout roots must not share a folder")
    immutable = document.get("immutable")
    if not isinstance(immutable, list) or not set(immutable) <= set(roots):
        _refuse("layout immutable roots must be named roots")
    return Layout(1, dict(roots), tuple(immutable))


READERS = {1: _read_v1}


def parse(document: dict) -> Layout:
    reader = READERS.get(document.get("layout_version"))
    if reader is None:
        _refuse(f"layout version {document.get('layout_version')!r} has no reader, so new launches stop")
    return reader(document)


def load(path: Path | None = None) -> Layout:
    found = path or next((p for p in LAYOUTS if p.is_file()), LAYOUTS[-1])
    return parse(json.loads(found.read_text(encoding="utf-8")))


def mapping(layout: Layout) -> dict:
    return {"layout_version": layout.version, "roots": dict(layout.roots), "immutable": list(layout.immutable)}


def contain(root: Path, candidate: Path | str) -> Path:
    base = root.resolve(strict=True)
    resolved = (base / candidate).resolve()
    if not resolved.is_relative_to(base):
        _refuse(f"path resolves outside its execution root: {candidate}")
    return resolved


def allocate(base: Path, attempt: str, layout: Layout) -> Execution:
    if not SEGMENT.fullmatch(attempt):
        _refuse(f"invalid attempt id: {attempt}")
    if base.is_symlink() or not base.is_dir():
        _refuse(f"execution base not found: {base}")
    root = base / attempt
    if root.is_symlink():
        _refuse(f"execution root is a link: {attempt}")
    root.mkdir(mode=0o700, exist_ok=True)
    execution = Execution(root, layout)
    for name in layout.roots:
        path = execution.path(name)
        if path.is_symlink():
            _refuse(f"layout root {name} is a link")
        path.mkdir(mode=0o700, exist_ok=True)
    return execution


def restore(base: Path, attempt: str, recorded: dict) -> Execution:
    return allocate(base, attempt, parse(recorded))


def _links(tree: Path, inside: Path) -> None:
    for path in [tree, *tree.rglob("*")]:
        if path.is_symlink():
            contain(inside, path)


def seal(tree: Path) -> None:
    for path in [tree, *tree.rglob("*")]:
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.lstat().st_mode) & ~0o222)


def remove(tree: Path) -> None:
    for path in [tree, *tree.rglob("*")]:
        if path.is_dir() and not path.is_symlink():
            path.chmod(stat.S_IMODE(path.lstat().st_mode) | 0o700)
    shutil.rmtree(tree)


def seed(execution: Execution, source: Path, name: str) -> Path:
    if not SEGMENT.fullmatch(name):
        _refuse(f"invalid profile name: {name}")
    _links(source, source)
    copy = execution.path("seed") / name
    shutil.copytree(source, copy, symlinks=True)
    try:
        _links(copy, copy)
    except LayoutError:
        shutil.rmtree(copy)
        raise
    seal(copy)
    return copy


def _check_member(member: tarfile.TarInfo) -> None:
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts:
        _refuse(f"archive member leaves its destination: {member.name}")
    if member.type not in MEMBERS:
        _refuse(f"archive member is not a file, folder or relative link: {member.name}")
    joined = posixpath.normpath(posixpath.join(str(name.parent), member.linkname))
    if member.issym() and (member.linkname.startswith("/") or joined == ".." or joined.startswith("../")):
        _refuse(f"archive member leaves its destination: {member.name}")


def extract(execution: Execution, archive: Path, destination: Path) -> Path:
    target = contain(execution.root, destination)
    if target.exists():
        _refuse(f"extraction destination already exists: {destination}")
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        for member in members:
            _check_member(member)
        staging = execution.path("scratch") / f"extract-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            bundle.extractall(staging, members=members, filter="data")
            _links(staging, staging)
        except tarfile.FilterError as error:
            remove(staging)
            _refuse(f"archive member leaves its destination: {error.tarinfo.name}")
        except LayoutError:
            remove(staging)
            raise
    staging.replace(target)
    return target


def socket(execution: Execution, name: str) -> Path:
    if not SEGMENT.fullmatch(name):
        _refuse(f"invalid socket name: {name}")
    path = execution.path("runtime") / name
    if len(str(path).encode()) > SOCKET_BYTES:
        _refuse(f"socket path exceeds {SOCKET_BYTES} bytes: {name}")
    return path
