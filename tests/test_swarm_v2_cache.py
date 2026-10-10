import errno
import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_v2 import cache, filesystem

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
POLICY_FILE = ROOT / "docker" / "swarm-node" / "cache-policy.json"
LAYOUT_FILE = ROOT / "docker" / "swarm-node" / "layout.json"
GIB = 1 << 30
SCOPE = "github.com/org/repo"
UNTRUSTED = "github.com/untrusted/repo"


class Clock:
    def __init__(self, now: float = 4_000_000_000.0):
        self.now = now

    def __call__(self) -> float:
        self.now += 10
        return self.now


def free(amount: int):
    return lambda path: SimpleNamespace(total=amount, used=0, free=amount)


def build(root: Path) -> SimpleNamespace:
    cache.METRICS.update(dict.fromkeys(cache.METRICS, 0))
    base = root / "attempts"
    base.mkdir()
    layout = filesystem.load(LAYOUT_FILE)
    policy = replace(cache.load(POLICY_FILE), store=root / "cache")
    return SimpleNamespace(
        tmp=root,
        store=cache.Store(policy, SCOPE, clock=Clock(), usage=free(100 * GIB)),
        first=filesystem.allocate(base, "attempt-1", layout),
        second=filesystem.allocate(base, "attempt-2", layout),
    )


def key(kind="pip", lock="a" * 64, scope=SCOPE, toolchain="python-3.12") -> cache.Key:
    return cache.Key(kind=kind, toolchain=toolchain, lock=lock, platform="linux-amd64", scope=scope)


def fill(layer: cache.Layer, files: dict[str, bytes], executable: tuple[str, ...] = ()) -> None:
    for name, data in files.items():
        path = layer.writable / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if name in executable:
            path.chmod(0o755)


def tree(root: Path) -> dict[str, bytes | None]:
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
        if p.name != ".lock"
    }


def entries(store: cache.Store) -> list[str]:
    return sorted(p.name for p in store.policy.store.iterdir() if not p.name.startswith("."))


def publish_as(store: cache.Store, execution, cache_key: cache.Key, files: dict, executable=()) -> Path:
    layer = cache.attach(store, execution, cache_key)
    fill(layer, files, executable)
    return cache.publish(store, execution, layer)


def published(world, cache_key: cache.Key, files: dict[str, bytes], executable=()) -> Path:
    store = replace(world.store, scope=cache_key.scope)
    return publish_as(store, world.first, cache_key, files, executable)


@pytest.fixture
def world(tmp_path):
    return build(tmp_path)


@pytest.fixture(autouse=True)
def metrics(monkeypatch):
    monkeypatch.setattr(cache, "METRICS", {"cache_hits": 0, "cache_misses": 0, "cache_corruption_total": 0})


def test_the_shipped_policy_turns_reuse_on_and_keeps_credentials_and_session_databases_out():
    document = json.loads(POLICY_FILE.read_text())
    policy = cache.load(POLICY_FILE)
    assert document["package"] == "SV2-FSY-03"
    assert policy.enabled is True
    assert policy.store == Path("/home/worker/cache")
    assert policy.max_bytes == 20 * GIB
    assert policy.reserve_bytes == 2 * GIB
    assert policy.kinds == {"pip": False, "uv": False, "npm": False, "toolchain": True}
    assert policy.shared == ("trusted",)
    for name in (
        ".credentials.json",
        "auth.json",
        ".git-credentials",
        ".env",
        ".env.local",
        ".env.production",
        "prod.env.local",
        "state.sqlite",
        "history.db",
        "s.jsonl",
        "x-wal",
    ):
        assert cache.excluded(policy, name), name
    assert not cache.excluded(policy, "wheel.whl")


def test_load_reads_the_installed_policy_when_no_path_is_given(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "POLICIES", (tmp_path / "missing.json", POLICY_FILE))
    assert cache.load() == cache.load(POLICY_FILE)
    monkeypatch.setattr(cache, "POLICIES", (tmp_path / "missing.json",))
    with pytest.raises(cache.CacheError) as error:
        cache.load()
    assert str(error.value) == "no cache policy is installed, so cache reuse stays off"


def test_load_reads_an_explicit_path_instead_of_the_installed_policy(tmp_path):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({**json.loads(POLICY_FILE.read_text()), "max_bytes": 7}))
    assert cache.load(other).max_bytes == 7


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"policy_version": 2}, "cache policy version 2 has no reader"),
        ({"kinds": {}}, "cache policy must name each kind with whether it may hold executables"),
        ({"kinds": ["pip"]}, "cache policy must name each kind with whether it may hold executables"),
        ({"kinds": {"pip": "no"}}, "cache policy must name each kind with whether it may hold executables"),
        ({"kinds": {"Bad Kind": False}}, "cache policy must name each kind with whether it may hold executables"),
        ({"max_bytes": 0}, "cache policy byte limits must be positive integers"),
        ({"reserve_bytes": -1}, "cache policy byte limits must be positive integers"),
        ({"max_bytes": "1"}, "cache policy byte limits must be positive integers"),
        ({"max_bytes": True}, "cache policy byte limits must be positive integers"),
        ({"store": "relative/cache"}, "cache policy store must be an absolute path"),
        ({"excluded": "x"}, "cache policy excluded patterns and shared scopes must be lists of names"),
        ({"excluded": [1]}, "cache policy excluded patterns and shared scopes must be lists of names"),
        ({"shared": "trusted"}, "cache policy excluded patterns and shared scopes must be lists of names"),
        ({"shared": [None]}, "cache policy excluded patterns and shared scopes must be lists of names"),
        ({"enabled": "yes"}, "cache policy enabled must be true or false"),
    ],
)
def test_parse_refuses_a_malformed_policy(change, message):
    document = {**json.loads(POLICY_FILE.read_text()), **change}
    with pytest.raises(cache.CacheError) as error:
        cache.parse(document)
    assert str(error.value) == message


def test_parse_accepts_the_smallest_limits_and_reuse_off():
    document = {**json.loads(POLICY_FILE.read_text()), "max_bytes": 1, "reserve_bytes": 1, "enabled": False}
    policy = cache.parse(document)
    assert (policy.max_bytes, policy.reserve_bytes, policy.enabled) == (1, 1, False)
    assert policy.excluded == tuple(document["excluded"])


def test_a_store_defaults_to_the_wall_clock_and_the_real_disk_and_the_image_policy_path():
    store = cache.Store(cache.load(POLICY_FILE), SCOPE)
    assert store.clock is time.time
    assert store.usage is shutil.disk_usage
    assert cache.POLICIES == (POLICY_FILE, Path("/opt/swarm-node/cache-policy.json"))


def test_the_key_digest_covers_every_field():
    digests = {
        key().digest(),
        key(kind="uv").digest(),
        key(toolchain="python-3.11").digest(),
        key(lock="b" * 64).digest(),
        replace(key(), platform="linux-arm64").digest(),
        key(scope="github.com/other/repo").digest(),
    }
    assert len(digests) == 6
    canonical = json.dumps(asdict(key()), sort_keys=True, separators=(",", ":"))
    assert key().digest() == hashlib.sha256(canonical.encode()).hexdigest()


def test_lock_digest_is_the_sha256_of_the_lock_file(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"pinned\n")
    assert cache.lock_digest(lock) == hashlib.sha256(b"pinned\n").hexdigest()


def test_attach_refuses_an_unknown_kind(world):
    with pytest.raises(cache.CacheError) as error:
        cache.attach(world.store, world.first, key(kind="cargo"))
    assert str(error.value) == "cache kind cargo is not in the policy"
    assert not (world.first.path("scratch") / "cache").exists()


@pytest.mark.parametrize("field", ["toolchain", "lock", "platform", "scope"])
def test_attach_refuses_a_key_missing_any_field(world, field):
    with pytest.raises(cache.CacheError) as error:
        cache.attach(world.store, world.first, replace(key(), **{field: ""}))
    assert str(error.value) == "a cache key needs its toolchain, lock, platform and scope"


def test_an_attempt_reads_its_own_and_shared_scopes_and_publishes_only_its_own(world):
    with pytest.raises(cache.CacheError) as error:
        cache.attach(world.store, world.first, key(scope="github.com/other/repo"))
    assert str(error.value) == "cache scope github.com/other/repo is not granted to this attempt"
    seed = published(world, key(kind="toolchain", scope="trusted"), {"bin/node": b"trusted"}, ("bin/node",))
    shared = cache.attach(world.store, world.second, key(kind="toolchain", scope="trusted"))
    assert shared.seed == seed
    fill(shared, {"bin/node": b"poisoned"}, ("bin/node",))
    filesystem.remove(seed.parent)
    with pytest.raises(cache.CacheError) as error:
        cache.publish(world.store, world.second, shared)
    assert str(error.value) == "cache scope trusted is not granted to this attempt"
    assert entries(world.store) == []


def test_a_miss_gives_an_empty_private_writable_layer_and_counts_a_miss(world):
    layer = cache.attach(world.store, world.first, key())
    assert layer == cache.Layer(key(), None, world.first.path("scratch") / "cache" / key().digest())
    assert layer.writable.is_dir() and not list(layer.writable.iterdir())
    assert layer.writable.stat().st_mode & 0o777 == 0o700
    assert (layer.writable.parent).stat().st_mode & 0o777 == 0o700
    assert cache.METRICS == {"cache_hits": 0, "cache_misses": 1, "cache_corruption_total": 0}
    assert cache.cache_hit_rate() == 0.0


def test_cache_hit_rate_is_zero_before_any_attach_and_the_share_of_hits_after():
    assert cache.cache_hit_rate() == 0.0
    cache.METRICS.update(cache_hits=3, cache_misses=1)
    assert cache.cache_hit_rate() == 0.75


def test_two_compatible_attempts_reuse_one_sealed_seed_while_homes_stay_private(world):
    (world.first.path("home") / ".credentials.json").write_text("fixture")
    seed = published(world, key(), {"wheels/pkg.whl": b"wheel", "http/abc": b"body"})
    assert seed == world.store.policy.store / key().digest() / "content"
    layer = cache.attach(world.store, world.second, key())
    assert layer.seed == seed
    assert layer.writable == world.second.path("scratch") / "cache" / key().digest()
    assert tree(seed) == {"http": None, "http/abc": b"body", "wheels": None, "wheels/pkg.whl": b"wheel"}
    assert not any(p.stat().st_mode & 0o222 for p in [seed, *seed.rglob("*")])
    assert not list(world.second.path("home").iterdir())
    assert cache.METRICS == {"cache_hits": 1, "cache_misses": 1, "cache_corruption_total": 0}
    assert cache.cache_hit_rate() == 0.5


def test_publish_records_the_key_the_manifest_and_the_size(world):
    published(world, key(kind="toolchain"), {"bin/tool": b"#!/bin/sh\n", "lib/a": b"abc"}, executable=("bin/tool",))
    entry = world.store.policy.store / key(kind="toolchain").digest()
    assert json.loads((entry / "entry.json").read_text()) == {
        "key": asdict(key(kind="toolchain")),
        "files": {
            "bin/tool": [hashlib.sha256(b"#!/bin/sh\n").hexdigest(), True],
            "lib/a": [hashlib.sha256(b"abc").hexdigest(), False],
        },
        "bytes": 13,
    }
    assert entry.stat().st_mtime == world.store.clock.now
    assert world.store.policy.store.stat().st_mode & 0o777 == 0o700


def test_publish_refuses_a_kind_outside_the_policy(world):
    layer = cache.attach(world.store, world.first, key())
    with pytest.raises(cache.CacheError) as error:
        cache.publish(world.store, world.first, replace(layer, key=key(kind="cargo")))
    assert str(error.value) == "cache kind cargo is not in the policy"
    assert entries(world.store) == []


def test_content_that_turns_unsafe_while_copying_is_refused_and_staging_removed(world, monkeypatch):
    layer = cache.attach(world.store, world.first, key())
    fill(layer, {"a.whl": b"x"})
    copy = shutil.copytree

    def planting(source, target, **kwargs):
        copy(source, target, **kwargs)
        (Path(target) / "late.db").write_text("planted")

    monkeypatch.setattr(cache.shutil, "copytree", planting)
    with pytest.raises(cache.CacheError) as error:
        cache.publish(world.store, world.first, layer)
    assert str(error.value) == "the cache entry could not be written: cache content holds an excluded file: late.db"
    assert list(world.store.policy.store.iterdir()) == [world.store.policy.store / ".lock"]


def test_a_link_swapped_in_after_the_check_is_copied_as_a_link_and_refused(world, monkeypatch):
    secret = world.first.path("home") / "token"
    secret.write_text("secret-value")
    layer = cache.attach(world.store, world.first, key())
    fill(layer, {"a.whl": b"x"})
    copy = shutil.copytree

    def swapping(source, target, **kwargs):
        (Path(source) / "a.whl").unlink()
        (Path(source) / "a.whl").symlink_to(secret)
        copy(source, target, **kwargs)

    monkeypatch.setattr(cache.shutil, "copytree", swapping)
    with pytest.raises(cache.CacheError) as error:
        cache.publish(world.store, world.first, layer)
    assert str(error.value) == "the cache entry could not be written: cache content holds a link: a.whl"
    assert entries(world.store) == []


def test_a_dangling_link_in_place_of_an_entry_is_discarded_by_attach_and_by_publish(world):
    world.store.policy.store.mkdir(mode=0o700)
    entry = world.store.policy.store / key().digest()
    entry.symlink_to(world.tmp / "gone")
    layer = cache.attach(world.store, world.first, key())
    assert layer.seed is None and not entry.is_symlink()
    assert cache.METRICS["cache_corruption_total"] == 1
    entry.symlink_to(world.tmp / "gone")
    fill(layer, {"a.whl": b"x"})
    assert cache.publish(world.store, world.first, layer) == entry / "content"
    assert not entry.is_symlink() and (entry / "content" / "a.whl").read_bytes() == b"x"
    assert cache.METRICS["cache_corruption_total"] == 2


def test_publish_rebuilds_an_entry_moved_under_its_key_even_when_the_files_match(world):
    moved = published(world, key(lock="1" * 64), {"a.whl": b"x"})
    root = world.store.policy.store
    moved.parent.rename(root / key().digest())
    layer = cache.attach(replace(world.store, policy=replace(world.store.policy, enabled=False)), world.first, key())
    fill(layer, {"a.whl": b"x"})
    seed = cache.publish(world.store, world.first, layer)
    assert json.loads((seed.parent / "entry.json").read_text())["key"] == asdict(key())
    assert cache.METRICS["cache_corruption_total"] == 1


def test_publish_waits_for_the_store_lock_even_against_a_shared_holder(world):
    layer = cache.attach(world.store, world.first, key())
    fill(layer, {"a.whl": b"x"})
    result = []
    with open(world.store.policy.store / ".lock", "a") as handle:
        cache.fcntl.flock(handle, cache.fcntl.LOCK_SH)
        worker = threading.Thread(target=lambda: result.append(cache.publish(world.store, world.first, layer)))
        worker.start()
        worker.join(timeout=0.5)
        assert worker.is_alive() and entries(world.store) == []
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert result == [world.store.policy.store / key().digest() / "content"]


def test_a_mismatched_dependency_lock_misses_instead_of_reusing(world):
    published(world, key(), {"wheels/pkg.whl": b"wheel"})
    layer = cache.attach(world.store, world.second, key(lock="b" * 64))
    assert layer.seed is None
    assert layer.writable.name == key(lock="b" * 64).digest()
    assert entries(world.store) == [key().digest()]


@pytest.mark.parametrize(
    ("name", "message"),
    [
        (".credentials.json", "cache content holds an excluded file: nested/.credentials.json"),
        ("history.sqlite", "cache content holds an excluded file: nested/history.sqlite"),
        ("session.jsonl", "cache content holds an excluded file: nested/session.jsonl"),
    ],
)
def test_publish_refuses_credentials_and_session_databases_and_leaves_the_store_unchanged(world, name, message):
    layer = cache.attach(world.store, world.first, key())
    fill(layer, {"ok.whl": b"x", f"nested/{name}": b"secret-value"})
    with pytest.raises(cache.CacheError) as error:
        cache.publish(world.store, world.first, layer)
    assert str(error.value) == message
    assert "secret-value" not in str(error.value)
    assert entries(world.store) == []


def test_publish_refuses_an_executable_in_a_dependency_kind(world):
    layer = cache.attach(world.store, world.first, key())
    fill(layer, {"bin/run": b"#!/bin/sh\n"}, executable=("bin/run",))
    with pytest.raises(cache.CacheError) as error:
        cache.publish(world.store, world.first, layer)
    assert str(error.value) == "cache kind pip holds an executable: bin/run"


@pytest.mark.parametrize("bit", [0o100, 0o010, 0o001])
def test_any_execute_bit_counts_as_an_executable(world, bit):
    layer = cache.attach(world.store, world.first, key())
    fill(layer, {"run": b"x"})
    (layer.writable / "run").chmod(0o600 | bit)
    with pytest.raises(cache.CacheError):
        cache.publish(world.store, world.first, layer)


def test_publish_refuses_links_and_special_files(world):
    layer = cache.attach(world.store, world.first, key())
    fill(layer, {"real.whl": b"x"})
    (layer.writable / "link.whl").symlink_to("real.whl")
    with pytest.raises(cache.CacheError) as error:
        cache.publish(world.store, world.first, layer)
    assert str(error.value) == "cache content holds a link: link.whl"
    (layer.writable / "link.whl").unlink()
    os.mkfifo(layer.writable / "pipe")
    with pytest.raises(cache.CacheError) as error:
        cache.publish(world.store, world.first, layer)
    assert str(error.value) == "cache content holds a special file: pipe"


def test_publish_refuses_a_layer_outside_the_attempt_scratch_root(world):
    layer = cache.attach(world.store, world.first, key())
    escaped = replace(layer, writable=world.first.path("home"))
    with pytest.raises(filesystem.LayoutError):
        cache.publish(world.store, world.first, escaped)
    other = cache.attach(world.store, world.second, key())
    with pytest.raises(filesystem.LayoutError):
        cache.publish(world.store, world.first, other)
    assert entries(world.store) == []


def test_publish_never_replaces_an_existing_entry_and_says_when_content_differs(world):
    seed = published(world, key(), {"a.whl": b"first"})
    layer = cache.attach(world.store, world.second, key())
    fill(layer, {"a.whl": b"second"})
    with pytest.raises(cache.CacheError) as error:
        cache.publish(world.store, world.second, layer)
    assert str(error.value) == "the cache entry for this key holds other content and is not replaced"
    assert (seed / "a.whl").read_bytes() == b"first"
    (layer.writable / "a.whl").write_bytes(b"first")
    assert cache.publish(world.store, world.second, layer) == seed


def test_a_failure_after_staging_keeps_every_victim_and_leaves_no_staging(world, monkeypatch):
    kept = published(world, key(lock="1" * 64), {"a.whl": b"x" * 100})
    small = replace(world.store, policy=replace(world.store.policy, max_bytes=150))
    layer = cache.attach(small, world.first, key())
    fill(layer, {"a.whl": b"x" * 100})

    def refused(path, times):
        raise OSError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(cache.os, "utime", refused)
    with pytest.raises(cache.CacheError) as error:
        cache.publish(small, world.first, layer)
    assert str(error.value) == f"the cache entry could not be written: {os.strerror(errno.EPERM)}"
    assert kept.exists()
    assert entries(world.store) == [key(lock="1" * 64).digest()]
    assert not list(world.store.policy.store.glob(".staging-*"))


def test_an_untrusted_project_publishes_only_into_its_own_scope(world):
    trusted = key(kind="toolchain", scope="trusted")
    seed = published(world, trusted, {"bin/node": b"trusted"}, executable=("bin/node",))
    before = tree(world.store.policy.store)
    untrusted = replace(world.store, scope=UNTRUSTED)
    publish_as(untrusted, world.second, key(kind="toolchain", scope=UNTRUSTED), {"bin/node": b"bad"}, ("bin/node",))
    assert cache.attach(world.store, world.first, trusted).seed == seed
    assert (seed / "bin" / "node").read_bytes() == b"trusted"
    assert {k: v for k, v in tree(world.store.policy.store).items() if k in before} == before


def test_an_entry_moved_under_another_key_is_discarded_by_its_recorded_key(world):
    poisoned = key(kind="toolchain", scope=UNTRUSTED)
    published(world, poisoned, {"bin/node": b"poisoned"}, executable=("bin/node",))
    trusted = key(kind="toolchain", scope="trusted")
    root = world.store.policy.store
    (root / poisoned.digest()).rename(root / trusted.digest())
    layer = cache.attach(world.store, world.second, trusted)
    assert layer.seed is None
    assert not (root / trusted.digest()).exists()
    assert cache.METRICS["cache_corruption_total"] == 1


@pytest.mark.parametrize("damage", ["content", "mode", "extra", "missing", "record", "fields", "shape", "link"])
def test_corrupt_content_is_discarded_counted_and_rebuilt(world, damage):
    seed = published(world, key(kind="toolchain"), {"bin/t": b"tool", "lib/a": b"abc"}, executable=("bin/t",))
    entry = seed.parent
    for path in [entry, *entry.rglob("*")]:
        path.chmod(path.stat().st_mode | 0o200)
    elsewhere = world.tmp / "elsewhere"
    if damage == "fields":
        (entry / "entry.json").write_text("{}")
    elif damage == "shape":
        (entry / "entry.json").write_text("[]")
    elif damage == "content":
        (seed / "lib" / "a").write_bytes(b"abd")
    elif damage == "mode":
        (seed / "lib" / "a").chmod(0o755)
    elif damage == "extra":
        (seed / "lib" / "b").write_bytes(b"planted")
    elif damage == "missing":
        (seed / "lib" / "a").unlink()
    elif damage == "record":
        (entry / "entry.json").write_text("{")
    else:
        entry.rename(elsewhere)
        entry.symlink_to(elsewhere)
    layer = cache.attach(world.store, world.second, key(kind="toolchain"))
    assert layer.seed is None
    assert not entry.exists() and not entry.is_symlink()
    assert elsewhere.is_dir() == (damage == "link")
    assert cache.METRICS == {"cache_hits": 0, "cache_misses": 2, "cache_corruption_total": 1}
    fill(layer, {"bin/t": b"tool", "lib/a": b"abc"}, executable=("bin/t",))
    assert cache.publish(world.store, world.second, layer) == seed
    assert cache.attach(world.store, world.first, key(kind="toolchain")).seed == seed


def test_a_hit_marks_the_entry_used_at_the_store_clock(world):
    seed = published(world, key(), {"a.whl": b"x"})
    cache.attach(world.store, world.second, key())
    assert seed.parent.stat().st_mtime == world.store.clock.now


def test_a_full_disk_refuses_publish_without_evicting_or_writing(world):
    published(world, key(), {"a.whl": b"x" * 100})
    before = tree(world.store.policy.store)
    full = replace(world.store, usage=free(world.store.policy.reserve_bytes + 50))
    layer = cache.attach(full, world.second, key(lock="b" * 64))
    fill(layer, {"b.whl": b"y" * 300})
    with pytest.raises(cache.CacheError) as error:
        cache.publish(full, world.second, layer)
    assert str(error.value) == "the cache store lacks room for 300 bytes, so nothing was evicted or written"
    assert tree(world.store.policy.store) == before
    assert (layer.writable / "b.whl").read_bytes() == b"y" * 300


def test_eviction_frees_disk_room_when_the_reserve_would_be_crossed(world):
    old = published(world, key(lock="1" * 64), {"a.whl": b"x" * 100})
    room = free(world.store.policy.reserve_bytes + 200)
    tight = replace(world.store, usage=lambda path: room(path) if path == world.store.policy.store else None)
    layer = cache.attach(tight, world.second, key(lock="2" * 64))
    fill(layer, {"b.whl": b"y" * 300})
    assert cache.publish(tight, world.second, layer).exists()
    assert not old.exists()


def test_publish_fits_exactly_at_the_reserve(world):
    full = replace(world.store, usage=free(world.store.policy.reserve_bytes + 300))
    layer = cache.attach(full, world.first, key())
    fill(layer, {"b.whl": b"y" * 300})
    assert cache.publish(full, world.first, layer) is not None


def test_eviction_removes_least_recently_used_entries_only_as_far_as_needed(world):
    small = replace(world.store, policy=replace(world.store.policy, max_bytes=300))
    first = published(world, key(lock="1" * 64), {"w/a.whl": b"x" * 100})
    second = published(world, key(lock="2" * 64), {"w/a.whl": b"x" * 100})
    third = published(world, key(lock="3" * 64), {"w/a.whl": b"x" * 100})
    for seed, used in ((first, 300), (second, 100), (third, 200)):
        os.utime(seed.parent, (world.store.clock.now + used,) * 2)
    layer = cache.attach(small, world.second, key(lock="4" * 64))
    fill(layer, {"w/a.whl": b"x" * 100})
    cache.publish(small, world.second, layer)
    assert not second.exists()
    assert first.exists() and third.exists()
    assert len(entries(world.store)) == 3


def test_an_entry_that_fills_the_budget_exactly_evicts_nothing(world):
    small = replace(world.store, policy=replace(world.store.policy, max_bytes=300))
    kept = published(world, key(lock="1" * 64), {"w/a.whl": b"x" * 200})
    layer = cache.attach(small, world.second, key(lock="2" * 64))
    fill(layer, {"w/a.whl": b"x" * 100})
    assert cache.publish(small, world.second, layer).exists()
    assert kept.exists()


def test_an_entry_larger_than_the_budget_is_refused_with_nothing_evicted(world):
    small = replace(world.store, policy=replace(world.store.policy, max_bytes=150))
    kept = published(world, key(lock="1" * 64), {"a.whl": b"x" * 100})
    layer = cache.attach(small, world.second, key(lock="2" * 64))
    fill(layer, {"a.whl": b"x" * 151})
    with pytest.raises(cache.CacheError):
        cache.publish(small, world.second, layer)
    assert kept.exists()


def test_a_write_failure_leaves_no_staging_no_entry_and_evicts_nothing(world, monkeypatch):
    kept = published(world, key(lock="1" * 64), {"a.whl": b"x" * 100})
    small = replace(world.store, policy=replace(world.store.policy, max_bytes=150))
    layer = cache.attach(small, world.first, key())
    fill(layer, {"a.whl": b"x" * 100})

    def no_space(source, target, **kwargs):
        Path(target).mkdir(parents=True)
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(cache.shutil, "copytree", no_space)
    with pytest.raises(cache.CacheError) as error:
        cache.publish(small, world.first, layer)
    assert str(error.value) == f"the cache entry could not be written: {os.strerror(errno.ENOSPC)}"
    assert entries(world.store) == [key(lock="1" * 64).digest()]
    assert not list(world.store.policy.store.glob(".staging-*"))
    assert kept.exists()
    assert (layer.writable / "a.whl").read_bytes() == b"x" * 100


def test_publish_clears_staging_left_by_an_interrupted_publish(world):
    world.store.policy.store.mkdir(mode=0o700)
    stale = world.store.policy.store / ".staging-dead"
    (stale / "content").mkdir(parents=True)
    filesystem.seal(stale)
    published(world, key(), {"a.whl": b"x"})
    assert not stale.exists()
    assert entries(world.store) == [key().digest()]


def test_reuse_off_attaches_without_a_seed_and_publishes_nothing(world):
    seed = published(world, key(), {"a.whl": b"x"})
    off = replace(world.store, policy=replace(world.store.policy, enabled=False))
    layer = cache.attach(off, world.second, key())
    assert layer.seed is None and layer.writable.is_dir()
    fill(layer, {"b.whl": b"y"})
    assert cache.publish(off, world.second, replace(layer, key=key(lock="b" * 64))) is None
    assert entries(world.store) == [key().digest()]
    assert (seed / "a.whl").read_bytes() == b"x"
