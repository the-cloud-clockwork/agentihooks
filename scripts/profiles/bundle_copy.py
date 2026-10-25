from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

READ_SECONDS = 10
COPY_SECONDS = 300
_ACTIVE: ContextVar[tuple[Path, Path, str] | None] = ContextVar("recorded_bundle", default=None)


def current() -> Path | None:
    active = _ACTIVE.get()
    return active[1] if active is not None else None


def identity(path: Path) -> Path:
    active = _ACTIVE.get()
    if active is not None and path.is_relative_to(active[1]):
        return active[0] / path.relative_to(active[1])
    return path


def attach(root: Path) -> None:
    active = _ACTIVE.get()
    if active is not None:
        destination = root / "bundle" / active[0].name
        shutil.copytree(active[1], destination, symlinks=True)
        _ACTIVE.set((active[0], destination, active[2]))


def reuse(root: Path) -> bool:
    active = _ACTIVE.get()
    if active is None:
        return True
    destination = root / "bundle" / active[0].name
    if not destination.is_dir():
        return False
    git = ["git", "-C", str(destination)]
    head = _read([*git, "rev-parse", "HEAD"])
    status = _read([*git, "status", "--porcelain", "--untracked-files=all"])
    if head != active[2] or status != "":
        return False
    _ACTIVE.set((active[0], destination, active[2]))
    return True


def _read(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=READ_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    return None if done.returncode else done.stdout.strip()


def _git(bundle: Path, revision: str, argv: list[str], timeout: int = READ_SECONDS) -> str:
    recorded = f"the launch recorded bundle commit {revision}, but"
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{recorded} git did not answer within {timeout} seconds for the bundle at {bundle}") from exc
    if done.returncode:
        raise ValueError(f"{recorded} git cannot read the bundle at {bundle}: {done.stderr.strip()}")
    return done.stdout.strip()


@contextmanager
def recorded(bundle: Path | None, revision: str, root: Path) -> Iterator[None]:
    if bundle is None:
        raise ValueError(f"the launch recorded bundle commit {revision}, but no bundle is linked")
    commit = _git(
        bundle,
        revision,
        ["git", "-C", str(bundle), "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
    )
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bundle-", dir=root) as temporary:
        copy = Path(temporary) / "recorded"
        copy.mkdir()
        git = ["git", "-C", str(copy)]
        for args in (
            ["init", "--quiet"],
            ["fetch", "--quiet", "--depth=1", "--no-tags", str(bundle), commit],
            ["update-ref", "HEAD", "FETCH_HEAD"],
            ["read-tree", "HEAD"],
            ["checkout-index", "--all"],
        ):
            _git(bundle, revision, [*git, *args], COPY_SECONDS)
        token = _ACTIVE.set((bundle, copy, commit))
        try:
            yield
        finally:
            _ACTIVE.reset(token)
