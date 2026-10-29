from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path

READ_SECONDS = 10
COPY_SECONDS = 300
ROOT_FOLDER = "bundle"
USER_FOLDER = "user-bundle"
_ACTIVE: ContextVar[tuple[Path, Path, str] | None] = ContextVar("recorded_bundle", default=None)
_ACTIVE_USER: ContextVar[tuple[Path, Path, str] | None] = ContextVar("recorded_user_bundle", default=None)
_SLOTS = ((ROOT_FOLDER, _ACTIVE), (USER_FOLDER, _ACTIVE_USER))


def is_pinned() -> bool:
    return _ACTIVE.get() is not None


def current() -> Path | None:
    active = _ACTIVE.get()
    return active[1] if active is not None else None


def current_user() -> Path | None:
    active = _ACTIVE_USER.get()
    return active[1] if active is not None else None


def identity(path: Path) -> Path:
    for _folder, slot in _SLOTS:
        active = slot.get()
        if active is not None and path.is_relative_to(active[1]):
            return active[0] / path.relative_to(active[1])
    return path


def attach(root: Path) -> None:
    for folder, slot in _SLOTS:
        active = slot.get()
        if active is not None:
            destination = root / folder / active[0].name
            shutil.copytree(active[1], destination, symlinks=True)
            slot.set((active[0], destination, active[2]))


def reuse(root: Path) -> bool:
    pinned = [(folder, slot, slot.get()) for folder, slot in _SLOTS if slot.get() is not None]
    moved = []
    for folder, slot, active in pinned:
        destination = root / folder / active[0].name
        if not destination.is_dir():
            return False
        git = ["git", "-C", str(destination)]
        head = _read([*git, "rev-parse", "HEAD"])
        status = _read([*git, "status", "--porcelain", "--untracked-files=all"])
        if head != active[2] or status != "":
            return False
        moved.append((slot, (active[0], destination, active[2])))
    for slot, value in moved:
        slot.set(value)
    return True


def _read(argv: list[str]) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=READ_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    return None if done.returncode else done.stdout.strip()


def _git(bundle: Path, revision: str, argv: list[str], timeout: int = READ_SECONDS, label: str = "bundle") -> str:
    recorded = f"the launch recorded {label} commit {revision}, but"
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{recorded} git did not answer within {timeout} seconds for the {label} at {bundle}") from exc
    if done.returncode:
        raise ValueError(f"{recorded} git cannot read the {label} at {bundle}: {done.stderr.strip()}")
    return done.stdout.strip()


def _pin(stack: ExitStack, bundle: Path, revision: str, root: Path, label: str) -> tuple[Path, str]:
    commit = _git(
        bundle,
        revision,
        ["git", "-C", str(bundle), "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
        label=label,
    )
    temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="bundle-", dir=root))
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
        _git(bundle, revision, [*git, *args], COPY_SECONDS, label)
    return copy, commit


@contextmanager
def recorded(
    bundle: Path | None, revision: str, root: Path, user: Path | None = None, user_revision: str = ""
) -> Iterator[None]:
    if bundle is None:
        raise ValueError(f"the launch recorded bundle commit {revision}, but no bundle is linked")
    if user_revision and user is None:
        raise ValueError(f"the launch recorded user bundle commit {user_revision}, but no user bundle is linked")
    root.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        copy, commit = _pin(stack, bundle, revision, root, "bundle")
        pinned = {_ACTIVE: (bundle, copy, commit)}
        if user_revision:
            user_copy, user_commit = _pin(stack, user, user_revision, root, "user bundle")
            pinned[_ACTIVE_USER] = (user, user_copy, user_commit)
        for slot in (_ACTIVE, _ACTIVE_USER):
            stack.callback(slot.reset, slot.set(pinned.get(slot)))
        yield
