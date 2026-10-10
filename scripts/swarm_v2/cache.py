import fcntl
import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from fnmatch import fnmatch
from pathlib import Path

from scripts.swarm_v2 import filesystem
from scripts.swarm_v2.filesystem import SEGMENT, Execution

POLICIES = (
    Path(__file__).resolve().parents[2] / "docker" / "swarm-node" / "cache-policy.json",
    Path("/opt/swarm-node/cache-policy.json"),
)
ENTRY = "entry.json"
CONTENT = "content"
LOCK = ".lock"
STAGING = ".staging-"
METRICS = {"cache_hits": 0, "cache_misses": 0, "cache_corruption_total": 0}


class CacheError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    enabled: bool
    store: Path
    max_bytes: int
    reserve_bytes: int
    kinds: dict[str, bool]
    excluded: tuple[str, ...]


@dataclass(frozen=True)
class Key:
    kind: str
    toolchain: str
    lock: str
    platform: str
    scope: str

    def digest(self) -> str:
        canonical = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Store:
    policy: Policy
    clock: Callable[[], float] = time.time
    usage: Callable = shutil.disk_usage


@dataclass(frozen=True)
class Layer:
    key: Key
    seed: Path | None
    writable: Path


def parse(document: dict) -> Policy:
    if document.get("policy_version") != 1:
        raise CacheError(f"cache policy version {document.get('policy_version')!r} has no reader")
    kinds = document.get("kinds")
    if (
        not kinds
        or not isinstance(kinds, dict)
        or not all(SEGMENT.fullmatch(kind) and isinstance(executable, bool) for kind, executable in kinds.items())
    ):
        raise CacheError("cache policy must name each kind with whether it may hold executables")
    limits = (document.get("max_bytes"), document.get("reserve_bytes"))
    if not all(type(limit) is int and limit > 0 for limit in limits):
        raise CacheError("cache policy byte limits must be positive integers")
    store = Path(str(document.get("store")))
    if not store.is_absolute():
        raise CacheError("cache policy store must be an absolute path")
    excluded = document.get("excluded")
    if not isinstance(excluded, list) or not all(isinstance(pattern, str) for pattern in excluded):
        raise CacheError("cache policy excluded patterns must be a list of names")
    if not isinstance(document.get("enabled"), bool):
        raise CacheError("cache policy enabled must be true or false")
    return Policy(document["enabled"], store, *limits, dict(kinds), tuple(excluded))


def load(path: Path | None = None) -> Policy:
    for found in [path] if path else POLICIES:
        if found.is_file():
            return parse(json.loads(found.read_text()))
    raise CacheError("no cache policy is installed, so cache reuse stays off")


def excluded(policy: Policy, name: str) -> bool:
    return any(fnmatch(name, pattern) for pattern in policy.excluded)


def lock_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hit_rate() -> float:
    looked = METRICS["cache_hits"] + METRICS["cache_misses"]
    return METRICS["cache_hits"] / looked if looked else 0.0


def _manifest(tree: Path, policy: Policy, kind: str) -> dict[str, list]:
    files = {}
    for path in sorted(tree.rglob("*")):
        name = str(path.relative_to(tree))
        if path.is_symlink():
            raise CacheError(f"cache content holds a link: {name}")
        if excluded(policy, path.name):
            raise CacheError(f"cache content holds an excluded file: {name}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CacheError(f"cache content holds a special file: {name}")
        executable = bool(path.stat().st_mode & 0o111)
        if executable and not policy.kinds[kind]:
            raise CacheError(f"cache kind {kind} holds an executable: {name}")
        files[name] = [hashlib.sha256(path.read_bytes()).hexdigest(), executable]
    return files


def _drop(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    else:
        filesystem.remove(path)


def _verified(store: Store, key: Key) -> Path | None:
    entry = store.policy.store / key.digest()
    if not entry.exists() and not entry.is_symlink():
        return None
    try:
        record = json.loads((entry / ENTRY).read_text())
        sound = not entry.is_symlink() and record["key"] == asdict(key)
        sound = sound and record["files"] == _manifest(entry / CONTENT, store.policy, key.kind)
    except (OSError, ValueError, KeyError, TypeError):
        sound = False
    if not sound:
        METRICS["cache_corruption_total"] += 1
        _drop(entry)
        return None
    now = store.clock()
    os.utime(entry, (now, now))
    return entry / CONTENT


@contextmanager
def _locked(store: Store):
    store.policy.store.mkdir(mode=0o700, exist_ok=True)
    with open(store.policy.store / LOCK, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _check(policy: Policy, key: Key) -> None:
    if key.kind not in policy.kinds:
        raise CacheError(f"cache kind {key.kind} is not in the policy")
    if not all(asdict(key).values()):
        raise CacheError("a cache key needs its toolchain, lock, platform and scope")


def attach(store: Store, execution: Execution, key: Key) -> Layer:
    _check(store.policy, key)
    layers = execution.path("scratch") / "cache"
    layers.mkdir(mode=0o700, exist_ok=True)
    writable = layers / key.digest()
    writable.mkdir(mode=0o700, exist_ok=True)
    seed = None
    if store.policy.enabled:
        with _locked(store):
            seed = _verified(store, key)
    METRICS["cache_hits" if seed else "cache_misses"] += 1
    return Layer(key, seed, writable)


def _bytes(tree: Path, files: dict) -> int:
    return sum((tree / name).stat().st_size for name in files)


def _size(entry: Path) -> int:
    return sum(path.lstat().st_size for path in (entry / CONTENT).rglob("*") if path.is_file())


def _fits(policy: Policy, total: int, free: int, size: int) -> bool:
    return total + size <= policy.max_bytes and free - size >= policy.reserve_bytes


def _make_room(store: Store, size: int) -> None:
    held = sorted(
        (path for path in store.policy.store.iterdir() if not path.name.startswith(".")),
        key=lambda path: path.lstat().st_mtime,
    )
    total = sum(_size(path) for path in held)
    free = store.usage(store.policy.store).free
    evicted = []
    while held and not _fits(store.policy, total, free, size):
        oldest = held.pop(0)
        freed = _size(oldest)
        total, free = total - freed, free + freed
        evicted.append(oldest)
    if not _fits(store.policy, total, free, size):
        raise CacheError(f"the cache store lacks room for {size} bytes, so nothing was evicted or written")
    for path in evicted:
        _drop(path)


def _write(store: Store, layer: Layer, entry: Path) -> None:
    staging = store.policy.store / f"{STAGING}{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        shutil.copytree(layer.writable, staging / CONTENT)
        files = _manifest(staging / CONTENT, store.policy, layer.key.kind)
        record = {"key": asdict(layer.key), "files": files, "bytes": _bytes(staging / CONTENT, files)}
        (staging / ENTRY).write_text(json.dumps(record))
        filesystem.seal(staging)
        now = store.clock()
        os.utime(staging, (now, now))
        staging.rename(entry)
    except (OSError, CacheError) as error:
        filesystem.remove(staging)
        reason = error.strerror if isinstance(error, OSError) else str(error)
        raise CacheError(f"the cache entry could not be written: {reason}") from None


def publish(store: Store, execution: Execution, layer: Layer) -> Path | None:
    if not store.policy.enabled:
        return None
    filesystem.contain(execution.path("scratch") / "cache", layer.writable)
    _check(store.policy, layer.key)
    size = _bytes(layer.writable, _manifest(layer.writable, store.policy, layer.key.kind))
    entry = store.policy.store / layer.key.digest()
    with _locked(store):
        for stale in store.policy.store.glob(f"{STAGING}*"):
            filesystem.remove(stale)
        if not entry.exists() and not entry.is_symlink():
            _make_room(store, size)
            _write(store, layer, entry)
    return entry / CONTENT
