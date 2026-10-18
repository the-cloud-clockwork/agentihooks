import tempfile
from dataclasses import replace
from pathlib import Path

from scripts.swarm_v2 import cache
from tests.test_swarm_v2_cache import UNTRUSTED, build, fill, free, key, publish_as, published, tree

A_THEN = "two compatible tasks reuse a dependency cache while keeping native conversations and credentials separate"
B_THEN = "a cache from an untrusted project cannot replace executable toolchain content in another scope"
C_THEN = "corrupt cache content is discarded and rebuilt without deleting the task's source changes"
SCOPE_REFUSED = "cache scope trusted is not granted to this attempt"


def _measured() -> dict:
    return {
        "cache_hit_rate": cache.cache_hit_rate(),
        "cache_corruption_total": cache.METRICS["cache_corruption_total"],
    }


def _refused(store: cache.Store, execution, layer: cache.Layer) -> str:
    try:
        cache.publish(store, execution, layer)
    except cache.CacheError as error:
        return str(error)
    return ""


def _homes_apart(world, seed: Path) -> dict:
    first_home, second_home = world.first.path("home"), world.second.path("home")
    leaked = cache.attach(world.store, world.first, key(lock="c" * 64))
    fill(leaked, {"ok.whl": b"x", ".credentials.json": b"fixture", "session.jsonl": b"{}"})
    refused = _refused(world.store, world.first, leaked)
    return {
        "credential_layer_refused": refused.startswith("cache content holds an excluded file"),
        "homes_distinct": first_home != second_home and not second_home.is_relative_to(first_home),
        "second_home_holds_nothing_of_the_first": not {p.name for p in second_home.rglob("*")}
        & {p.name for p in first_home.rglob("*")},
        "seed_holds_no_home_file": not {p.name for p in seed.rglob("*")} & {p.name for p in first_home.rglob("*")},
    }


def case_a():
    runs = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as root:
            world = build(Path(root))
            (world.first.path("home") / ".credentials.json").write_text("fixture")
            (world.first.path("home") / "session.jsonl").write_text("{}")
            seed = published(world, key(), {"wheels/pkg.whl": b"wheel"})
            layer = cache.attach(world.store, world.second, key())
            run = {
                "second_attempt_reuses_seed": layer.seed == seed,
                "seed_read_only": not any(p.stat().st_mode & 0o222 for p in [seed, *seed.rglob("*")]),
                "writable_layer_private": layer.writable.is_relative_to(world.second.root),
                **_homes_apart(world, seed),
            }
            run["passed"] = all(run.values())
            runs.append({**run, **_measured()})
    return {"then": A_THEN, "passed": all(run["passed"] for run in runs), "independent_fixtures": runs}


def _poisoned_move(world, trusted: cache.Key) -> bool:
    other = replace(trusted, toolchain="node-23")
    published(world, other, {"bin/node": b"trusted"}, ("bin/node",))
    poisoned = replace(other, scope=UNTRUSTED)
    published(world, poisoned, {"bin/node": b"poisoned"}, ("bin/node",))
    store = world.store.policy.store
    cache.filesystem.remove(store / other.digest())
    (store / poisoned.digest()).rename(store / other.digest())
    return cache.attach(world.store, world.second, other).seed is None and not (store / other.digest()).exists()


def case_b():
    with tempfile.TemporaryDirectory() as root:
        world = build(Path(root))
        store = world.store.policy.store
        trusted = key(kind="toolchain", scope="trusted")
        seed = published(world, trusted, {"bin/node": b"trusted"}, ("bin/node",))
        before = tree(store)
        untrusted = replace(world.store, scope=UNTRUSTED)
        shared = cache.attach(untrusted, world.second, trusted)
        fill(shared, {"bin/node": b"poisoned"}, ("bin/node",))
        refused = _refused(untrusted, world.second, shared)
        publish_as(untrusted, world.second, replace(trusted, scope=UNTRUSTED), {"bin/node": b"bad"}, ("bin/node",))
        protected = {k: v for k, v in tree(store).items() if k in before} == before
        lock_miss = cache.attach(world.store, world.second, replace(trusted, lock="b" * 64)).seed is None
        full = replace(world.store, usage=free(world.store.policy.reserve_bytes))
        layer = cache.attach(full, world.second, key())
        fill(layer, {"a.whl": b"x" * 10**6})
        held = tree(store)
        full_refused = _refused(full, world.second, layer).endswith("nothing was evicted or written")
        result = {
            "untrusted_publish_into_trusted_scope_refused": refused == SCOPE_REFUSED,
            "trusted_toolchain_unchanged": protected and cache.attach(world.store, world.first, trusted).seed == seed,
            "mismatched_lock_misses_beside_a_live_entry": lock_miss and (seed / "bin" / "node").exists(),
            "full_disk_refused_without_mutation": full_refused and tree(store) == held,
            "error_names_no_content": "poisoned" not in refused,
        }
        result["moved_poisoned_entry_discarded"] = _poisoned_move(world, trusted)
    return {"then": B_THEN, "passed": all(result.values()), **result, **_measured()}


def _rollback(world, seed: Path, source: Path) -> dict:
    off = replace(world.store, policy=replace(world.store.policy, enabled=False))
    held = tree(world.store.policy.store)
    layer = cache.attach(off, world.second, key())
    fill(layer, {"fresh.whl": b"y"})
    return {
        "rollback_attach_has_no_seed": layer.seed is None,
        "rollback_publish_writes_nothing": cache.publish(off, world.second, layer) is None,
        "rollback_store_and_source_kept": tree(world.store.policy.store) == held and source.exists() and seed.exists(),
    }


def case_c():
    with tempfile.TemporaryDirectory() as root:
        world = build(Path(root))
        source = world.first.path("worktree") / "task" / "module.py"
        source.parent.mkdir()
        source.write_text("edited = True\n")
        seed = published(world, key(), {"wheels/pkg.whl": b"wheel"})
        for path in [seed.parent, *seed.parent.rglob("*")]:
            path.chmod(path.stat().st_mode | 0o200)
        (seed / "wheels" / "pkg.whl").write_bytes(b"corrupt")
        discarded = cache.attach(world.store, world.first, key())
        rebuilt = published(world, key(), {"wheels/pkg.whl": b"wheel"})
        replayed = cache.publish(world.store, world.first, discarded)
        result = {
            "corrupt_entry_discarded": discarded.seed is None and cache.METRICS["cache_corruption_total"] == 1,
            "rebuilt_at_same_key": rebuilt == seed and (seed / "wheels" / "pkg.whl").read_bytes() == b"wheel",
            "replay_without_duplicate": replayed == seed
            and len([p for p in world.store.policy.store.iterdir() if not p.name.startswith(".")]) == 1,
            "source_changes_kept": source.read_text() == "edited = True\n",
            "rebuilt_seed_reused": cache.attach(world.store, world.second, key()).seed == seed,
        }
        measured = _measured()
        result.update(_rollback(world, seed, source))
    return {"then": C_THEN, "passed": all(result.values()), **result, **measured}
