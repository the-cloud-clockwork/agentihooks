import hashlib
import importlib.util
import json
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import cast
from unittest.mock import patch

from scripts.ci_mutation.stats_cache import per_test_stats
from scripts.ci_mutation.stats_dependencies import SHARED, StoredStats


def old_class(path: Path) -> type[StoredStats]:
    spec = importlib.util.spec_from_file_location("t2728_old_dependencies", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(type[StoredStats], module.StoredStats)


def lookup(
    cls: type[StoredStats], root: Path, cache: Path, context: dict, nodes: list[str], shared: bool
) -> tuple[list[dict | None], float, int]:
    stored = cls(root, cache, context, {})
    digests: dict[str, str | None] = {}
    start = time.perf_counter()
    if shared:
        results = [stored.load(node, digests=digests) for node in nodes]
    else:
        results = [stored.load(node) for node in nodes]
    seconds = time.perf_counter() - start
    assert all(result is not None for result in results), stored.misses
    assert not stored.misses, stored.misses
    return results, seconds, len(digests)


def hashes(
    cls: type[StoredStats], root: Path, cache: Path, context: dict, nodes: list[str], shared: bool
) -> tuple[Counter[str], Counter[str]]:
    reads: Counter[str] = Counter()
    payloads: Counter[str] = Counter()
    original = Path.read_bytes
    original_text = Path.read_text

    def counted(path: Path) -> bytes:
        if path.is_relative_to(root):
            reads[path.relative_to(root).as_posix()] += 1
        return original(path)

    def text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path.parent == cache and path.name.endswith(SHARED):
            payloads[path.name] += 1
        return original_text(path, encoding=encoding, errors=errors)

    with patch.object(Path, "read_bytes", counted), patch.object(Path, "read_text", text):
        lookup(cls, root, cache, context, nodes, shared)
    return reads, payloads


def main() -> None:
    archive = Path(sys.argv[1])
    baseline = old_class(Path(sys.argv[2]))
    folder = Path.cwd() / ".lookup-replay"
    root = folder / "inputs"
    cache = folder / "cache"
    root.mkdir(parents=True)
    cache.mkdir()
    with zipfile.ZipFile(archive) as saved:
        bucket = json.loads(saved.read("0-s176e3ut/stats-0.json"))
        incomplete = json.loads(saved.read("0-s176e3ut/stats-1.json"))
        assert not incomplete["dependencies"]
        nodes = list(bucket["durations"])
        dependencies = bucket["dependencies"]
        assert len(nodes) == 803 and set(nodes) == set(dependencies) == set(bucket["files"])
        expected = Counter(path for paths in dependencies.values() for path in paths)
        assert len(expected) == 1340 and expected.total() == 1075219
        for name in expected:
            path = root / name
            assert path.is_relative_to(root) and ".." not in Path(name).parts
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(saved.read(f"0-s176e3ut/{name}"))
    fingerprints = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in expected}
    context = {"inventory": sorted(expected), "replay": "original part seven bucket zero"}
    seed = StoredStats(root, cache, context, {})
    functions = set(bucket["tests"])
    for node in nodes:
        result, covered = per_test_stats(node, [bucket], len(nodes), functions)
        assert result is not None and covered == sorted(dependencies[node])
        seed.save(node, result, {name: fingerprints[name] for name in dependencies[node]}, True)
    assert len(list(cache.glob("*.index"))) == len(nodes)
    cache_before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in cache.iterdir()}
    before, before_seconds, before_memo = lookup(baseline, root, cache, context, nodes, False)
    after, after_seconds, after_memo = lookup(StoredStats, root, cache, context, nodes, True)
    assert before == after
    assert before_memo == 0 and after_memo == len(expected)
    before_hashes, _ = hashes(baseline, root, cache, context, nodes, False)
    after_hashes, after_payloads = hashes(StoredStats, root, cache, context, nodes, True)
    assert before_hashes == expected
    assert after_hashes == Counter(dict.fromkeys(expected, 1))
    assert cache_before == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in cache.iterdir()}
    assert fingerprints == {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in expected}
    print(
        json.dumps(
            {
                "artifact": 14465064482,
                "part": 7,
                "bucket": 0,
                "records": len(nodes),
                "inputs": len(expected),
                "before_seconds": before_seconds,
                "after_seconds": after_seconds,
                "speedup": before_seconds / after_seconds,
                "before_hashes": before_hashes.total(),
                "after_hashes": after_hashes.total(),
                "after_shared_payload_reads": after_payloads.total(),
                "distinct_shared_payloads": len(after_payloads),
                "identical_results": before == after,
                "cache_and_inputs_unchanged": True,
                "scope": "StoredStats.load on original saved bucket and original input bytes; collection and discovery excluded",
                "excluded_bucket": "bucket one has incomplete dependency evidence",
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
