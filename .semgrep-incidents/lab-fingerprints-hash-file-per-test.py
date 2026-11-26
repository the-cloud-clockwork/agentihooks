import hashlib
from collections.abc import Mapping


def _blob(tree: Mapping[str, bytes], path: str) -> str | None:
    content = tree.get(path)
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _cached_blob(tree: Mapping[str, bytes], path: str, cache: dict[str, str | None]) -> str | None:
    if path not in cache:
        cache[path] = _blob(tree, path)
    return cache[path]


def per_test_via_blob(manifest: dict, tree: Mapping[str, bytes]) -> dict[str, str]:
    keys: dict[str, str] = {}
    for test, record in manifest["tests"].items():
        opened = record["files"]
        # ruleid: lab-fingerprints-hash-file-per-test
        found = {path: _blob(tree, path) for path in opened}
        keys[test] = str(found)
    return keys


def per_test_via_sha256(manifest: dict, tree: Mapping[str, bytes]) -> dict[str, str]:
    keys: dict[str, str] = {}
    for test, record in manifest["tests"].items():
        opened = record["files"]
        # ruleid: lab-fingerprints-hash-file-per-test
        found = {path: hashlib.sha256(tree[path]).hexdigest() for path in opened}
        keys[test] = str(found)
    return keys


def per_test_via_cache(manifest: dict, tree: Mapping[str, bytes]) -> dict[str, str]:
    cache: dict[str, str | None] = {}
    keys: dict[str, str] = {}
    for test, record in manifest["tests"].items():
        opened = record["files"]
        # ok: lab-fingerprints-hash-file-per-test
        found = {path: _cached_blob(tree, path, cache) for path in opened}
        keys[test] = str(found)
    return keys


def per_test_in_a_comprehension(manifest: dict, tree: Mapping[str, bytes]) -> dict[str, str]:
    # ruleid: lab-fingerprints-hash-file-per-test
    return {test: str(_blob(tree, record["own"])) for test, record in manifest["tests"].items()}


def per_test_with_a_table_made_in_the_loop(manifest: dict, tree: Mapping[str, bytes]) -> dict[str, str]:
    keys: dict[str, str] = {}
    for test, record in manifest["tests"].items():
        cache: dict[str, str | None] = {}
        opened = record["files"]
        # ruleid: lab-fingerprints-hash-file-per-test
        found = {path: _cached_blob(tree, path, cache) for path in opened}
        keys[test] = str(found)
    return keys


def per_test_with_a_fresh_table_each_read(manifest: dict, tree: Mapping[str, bytes]) -> dict[str, str]:
    keys: dict[str, str] = {}
    for test, record in manifest["tests"].items():
        # ruleid: lab-fingerprints-hash-file-per-test
        keys[test] = str(_cached_blob(tree, record["own"], {}))
    return keys
