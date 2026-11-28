from __future__ import annotations

import fcntl
import os
import shutil
import stat
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from uuid import UUID

from scripts.ci_lab import contract
from scripts.scheduler.process_interval import Interval

ROOT = "CI_LAB_CACHE_ROOT"
LIMIT = "CI_LAB_CACHE_LIMIT_BYTES"
POD_UID = "CI_LAB_CACHE_POD_UID"
SCAN_SECONDS = 10.0
MARKER = "ci-lab-cache-v1\n"
_pid = os.getpid
_uid = os.getuid
_terminate = os._exit


class PolicyError(ValueError):
    pass


def _pod(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _marked(directory: Path) -> bool:
    marker = directory / ".owner"
    try:
        info = directory.lstat()
        owner = marker.lstat()
        return (
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == _uid()
            and stat.S_ISREG(owner.st_mode)
            and owner.st_uid == _uid()
            and marker.read_text() == MARKER
        )
    except FileNotFoundError:
        return False


def _configuration(environ: Mapping[str, str], required: bool) -> tuple[Path, int, str] | None:
    values = tuple(environ.get(name, "") for name in (ROOT, LIMIT, POD_UID))
    if not any(values) and not required:
        return None
    if not all(values):
        raise PolicyError(f"{ROOT}, {LIMIT} and {POD_UID} must all be set")
    directory, limit, pod = values
    if not limit.isascii() or not limit.isdecimal() or int(limit) <= 0 or not _pod(pod):
        raise PolicyError(f"{LIMIT} must be positive decimal bytes and {POD_UID} must be a Pod UUID")
    root = Path(directory)
    try:
        info = root.lstat()
        safe = (
            root.is_absolute()
            and root.resolve(strict=True) == root
            and stat.S_ISDIR(info.st_mode)
            and info.st_uid == _uid()
            and not info.st_mode & 0o022
        )
        owned = _marked(root)
        empty = not any(path.name != ".lock" for path in root.iterdir()) if safe and not owned else False
    except OSError as exc:
        raise PolicyError(f"{ROOT} cannot be inspected: {exc}") from exc
    if _pid() != 1 or not safe or not (owned or empty):
        raise PolicyError(f"{ROOT} requires PID one and an owned private directory")
    return root, int(limit), pod


def _directory(path: Path) -> None:
    path.mkdir(mode=0o750, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != _uid() or info.st_mode & 0o022:
        raise PolicyError(f"unsafe cache directory: {path}")


def _generation(root: Path, pod: str) -> Path:
    if not _marked(root):
        (root / ".owner").write_text(MARKER)
    generation = root / pod
    if generation.exists() or generation.is_symlink():
        if not _marked(generation):
            raise PolicyError("current cache generation is not owned")
        if (generation / ".invalid").exists() or (generation / ".invalid").is_symlink():
            shutil.rmtree(generation)
    for previous in root.iterdir():
        if previous.name != pod and _pod(previous.name) and _marked(previous):
            shutil.rmtree(previous)
    if not generation.exists():
        generation.mkdir(mode=0o750)
        (generation / ".owner").write_text(MARKER)
    _directory(generation / "node")
    _directory(generation / "store")
    return generation


def _prepare(root: Path, pod: str, limit: int) -> Path:
    generation = _generation(root, pod)
    if allocated_bytes(root) > limit:
        shutil.rmtree(generation)
        generation = _generation(root, pod)
        if allocated_bytes(root) > limit:
            raise PolicyError("cache root exceeds its limit after owned generation reset")
    return generation


@contextmanager
def _owned(root: Path, pod: str, limit: int) -> Generator[Path, None, None]:
    try:
        descriptor = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise PolicyError(f"cache ownership lock cannot be opened: {exc}") from exc
    with os.fdopen(descriptor, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != _uid() or info.st_nlink != 1 or info.st_mode & 0o022:
            raise PolicyError("cache ownership lock is unsafe")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PolicyError("cache already has an owner") from exc
        try:
            generation = _prepare(root, pod, limit)
        except OSError as exc:
            raise PolicyError(f"cache generation cannot be prepared: {exc}") from exc
        yield generation


def _walk_error(error: OSError) -> None:
    if not isinstance(error, FileNotFoundError):
        raise error


def allocated_bytes(root: Path) -> int:
    seen: set[tuple[int, int]] = set()
    total = 0
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for _path, directories, files, directory in os.fwalk(".", dir_fd=descriptor, onerror=_walk_error):
            for name in (".", *directories, *files):
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                key = info.st_dev, info.st_ino
                if key not in seen:
                    seen.add(key)
                    total += info.st_blocks * 512
    finally:
        os.close(descriptor)
    return total


def _invalidate(generation: Path, reason: str) -> None:
    try:
        with (generation / ".invalid").open("w") as marker:
            marker.write(reason)
            marker.flush()
            os.fsync(marker.fileno())
        print(f"ci-lab cache invalidated: {reason}", file=sys.stderr, flush=True)
    finally:
        _terminate(1)


def _check_budget(root: Path, generation: Path, limit: int) -> None:
    try:
        used = allocated_bytes(root)
    except OSError as exc:
        _invalidate(generation, f"measurement failed: {exc}")
        return
    if used > limit:
        _invalidate(generation, f"allocated {used} bytes exceeds {limit}")


def run(
    start: Callable[[Mapping[str, str]], int],
    environ: Mapping[str, str],
    *,
    required: bool = False,
) -> int:
    configuration = _configuration(environ, required)
    if configuration is None:
        return start(environ)
    root, limit, pod = configuration
    with _owned(root, pod, limit) as generation:
        resolved = dict(environ) | {
            contract.GIT_MIRROR: str(generation / "node"),
            "CI_LAB_MIRROR_STORE": str(generation / "store"),
        }
        watcher = Interval(partial(_check_budget, root, generation, limit), every=SCAN_SECONDS)
        watcher.start()
        try:
            return start(resolved)
        finally:
            try:
                watcher.close()
            except Exception as exc:
                _invalidate(generation, f"monitor shutdown failed: {exc}")
