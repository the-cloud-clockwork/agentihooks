import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from scripts.swarm_v2 import cache, filesystem
from tests.test_swarm_v2_cache import GIB, LAYOUT_FILE, POLICY_FILE, Clock, fill, free, key, tree


def _world(root: Path) -> SimpleNamespace:
    cache.METRICS.update(dict.fromkeys(cache.METRICS, 0))
    base = root / "attempts"
    base.mkdir()
    layout = filesystem.load(LAYOUT_FILE)
    policy = replace(cache.load(POLICY_FILE), store=root / "cache")
    return SimpleNamespace(
        store=cache.Store(policy, clock=Clock(), usage=free(100 * GIB)),
        first=filesystem.allocate(base, "attempt-1", layout),
        second=filesystem.allocate(base, "attempt-2", layout),
    )


def _publish(world, execution, cache_key, files, executable=()) -> Path:
    layer = cache.attach(world.store, execution, cache_key)
    fill(layer, files, executable)
    return cache.publish(world.store, execution, layer)


def _measured() -> dict:
    return {"cache_hit_rate": cache.hit_rate(), "cache_corruption_total": cache.METRICS["cache_corruption_total"]}


def case_a():
    runs = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as root:
            world = _world(Path(root))
            (world.first.path("home") / ".credentials.json").write_text("fixture")
            (world.first.path("home") / "session.jsonl").write_text("{}")
            seed = _publish(world, world.first, key(), {"wheels/pkg.whl": b"wheel"})
            layer = cache.attach(world.store, world.second, key())
            run = {
                "second_attempt_reuses_seed": layer.seed == seed,
                "seed_read_only": not any(p.stat().st_mode & 0o222 for p in [seed, *seed.rglob("*")]),
                "writable_layers_private": layer.writable.is_relative_to(world.second.root),
                "credentials_not_in_seed": not list(seed.rglob(".credentials.json")),
                "conversation_not_in_seed": not list(seed.rglob("*.jsonl")),
                "second_home_empty": not list(world.second.path("home").iterdir()),
            }
            run["passed"] = all(run.values())
            runs.append({**run, **_measured()})
    return {
        "then": "two compatible tasks reuse a dependency cache while keeping native conversations and credentials separate",
        "passed": all(run["passed"] for run in runs),
        "independent_fixtures": runs,
    }


def case_b():
    with tempfile.TemporaryDirectory() as root:
        world = _world(Path(root))
        trusted = key(kind="toolchain", scope="trusted")
        seed = _publish(world, world.first, trusted, {"bin/node": b"trusted"}, ("bin/node",))
        before = tree(world.store.policy.store)
        poisoned = key(kind="toolchain", scope="github.com/untrusted/repo")
        _publish(world, world.second, poisoned, {"bin/node": b"poisoned"}, ("bin/node",))
        kept = cache.attach(world.store, world.first, trusted).seed == seed
        unchanged = {k: v for k, v in tree(world.store.policy.store).items() if k in before} == before
        store = world.store.policy.store
        filesystem.remove(store / trusted.digest())
        (store / poisoned.digest()).rename(store / trusted.digest())
        moved = cache.attach(world.store, world.first, trusted).seed is None
        lock = cache.attach(world.store, world.second, replace(trusted, lock="b" * 64)).seed is None
        full = replace(world.store, usage=free(world.store.policy.reserve_bytes))
        layer = cache.attach(full, world.second, key())
        fill(layer, {"a.whl": b"x"})
        held = tree(store)
        try:
            cache.publish(full, world.second, layer)
            refused = False
        except cache.CacheError as error:
            refused = str(error).endswith("nothing was evicted or written")
        result = {
            "untrusted_scope_cannot_replace_toolchain": kept and unchanged,
            "moved_poisoned_entry_discarded": moved and not (store / trusted.digest()).exists(),
            "mismatched_lock_misses": lock,
            "full_disk_refused_without_mutation": refused and tree(store) == held,
        }
    return {
        "then": "a cache from an untrusted project cannot replace executable toolchain content in another scope",
        "passed": all(result.values()),
        **result,
        **_measured(),
    }


def case_c():
    with tempfile.TemporaryDirectory() as root:
        world = _world(Path(root))
        source = world.first.path("worktree") / "task" / "module.py"
        source.parent.mkdir()
        source.write_text("edited = True\n")
        seed = _publish(world, world.first, key(), {"wheels/pkg.whl": b"wheel"})
        for path in [seed.parent, *seed.parent.rglob("*")]:
            path.chmod(path.stat().st_mode | 0o200)
        (seed / "wheels" / "pkg.whl").write_bytes(b"corrupt")
        discarded = cache.attach(world.store, world.first, key())
        rebuilt = _publish(world, world.first, key(), {"wheels/pkg.whl": b"wheel"})
        replayed = cache.publish(world.store, world.first, discarded)
        result = {
            "corrupt_entry_discarded": discarded.seed is None and cache.METRICS["cache_corruption_total"] == 1,
            "rebuilt_at_same_key": rebuilt == seed and (seed / "wheels" / "pkg.whl").read_bytes() == b"wheel",
            "replay_without_duplicate": replayed == seed
            and len([p for p in world.store.policy.store.iterdir() if not p.name.startswith(".")]) == 1,
            "source_changes_kept": source.read_text() == "edited = True\n",
            "rebuilt_seed_reused": cache.attach(world.store, world.second, key()).seed == seed,
        }
    return {
        "then": "corrupt cache content is discarded and rebuilt without deleting the task's source changes",
        "passed": all(result.values()),
        **result,
        **_measured(),
    }
