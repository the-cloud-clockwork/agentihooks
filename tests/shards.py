import ast
import importlib
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Every xdist worker of a shard rewrites each file it collects, while stored durations split between workers; measured.
SECONDS_PER_SOURCE_BYTE = 3e-6
FIRST_SHARD_FILES = frozenset({"tests/routing/test_codex_api.py"})


def discover_test_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py"))


def source_sizes(root: Path, files: list[str]) -> dict[str, int]:
    return {path: (root / path).stat().st_size for path in files}


def grouped_files(root: Path, files: list[str]) -> dict[str, str]:
    grouped = {}
    for path in files:
        source = (root / path).read_text()
        if "xdist_group" not in source:
            continue
        names = {
            ast.unparse(
                node.args[0]
                if node.args
                else next((kw.value for kw in node.keywords if kw.arg == "name"), ast.Constant("default"))
            )
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "xdist_group"
        }
        if names:
            grouped[path] = "_".join(sorted(names))
    return grouped


def assign_files(
    durations: dict[str, float],
    files: list[str],
    shards: int,
    sizes: dict[str, int],
    grouped: set[str] | None = None,
    workers: int = 1,
) -> list[list[str]]:
    seconds = dict.fromkeys(files, 0.0)
    for nodeid, duration in durations.items():
        path = nodeid.split("::", 1)[0]
        if path in seconds:
            seconds[path] += duration
    serial = {path: seconds[path] if grouped and path in grouped else 0.0 for path in files}
    collection = {path: sizes.get(path, 0) * SECONDS_PER_SOURCE_BYTE for path in files}
    return _assign_work(seconds, serial, collection, shards, workers)


def assign_nodes(
    durations: dict[str, float],
    shards: int,
    grouped: set[str] | dict[str, str],
    workers: int,
    sizes: dict[str, int] | None = None,
) -> list[list[str]]:
    # A named group's tests in one file reuse what its first test built, which another shard would build again; a
    # group longer than an even shard share still splits, since one shard cannot hold it.
    share = sum(durations.values()) / shards
    file_seconds: dict[str, float] = {}
    for node, duration in durations.items():
        path = node.split("::", 1)[0]
        file_seconds[path] = file_seconds.get(path, 0.0) + duration
    units: dict[str, list[str]] = {}
    for node in durations:
        path = node.split("::", 1)[0]
        together = isinstance(grouped, dict) and path in grouped and file_seconds[path] <= share
        units.setdefault(path if together else node, []).append(node)
    seconds = {unit: sum(durations[node] for node in nodes) for unit, nodes in units.items()}
    files = {unit: unit.split("::", 1)[0] for unit in units}
    serial = {unit: seconds[unit] if path in grouped else 0.0 for unit, path in files.items()}
    serial_groups = {unit: grouped.get(path, "") for unit, path in files.items()} if isinstance(grouped, dict) else None
    collection = {unit: (sizes or {}).get(path, 0) * SECONDS_PER_SOURCE_BYTE for unit, path in files.items()}
    parts = _assign_work(seconds, serial, collection, shards, workers, serial_groups, files)
    return [[node for unit in part for node in units[unit]] for part in parts]


def _assign_work(
    seconds: dict[str, float],
    serial: dict[str, float],
    collection: dict[str, float],
    shards: int,
    workers: int,
    serial_groups: dict[str, str] | None = None,
    collection_groups: dict[str, str] | None = None,
) -> list[list[str]]:
    loads = [0.0] * shards
    serial_loads: list[dict[str, float]] = [{} for _ in range(shards)]
    # Serial loads only grow, so each shard's largest one is kept instead of rescanned for every unit.
    serial_peaks = [0.0] * shards
    collection_loads = [0.0] * shards
    collected: list[set[str]] = [set() for _ in range(shards)]
    groups: list[list[str]] = [[] for _ in range(shards)]
    for path in sorted(seconds, key=lambda f: (-max(seconds[f] / workers, serial[f]) - collection[f], f)):
        group = serial_groups[path] if serial_groups is not None else ""
        file = collection_groups[path] if collection_groups is not None else path
        lightest = min(
            range(shards),
            key=lambda i: (
                max(
                    (loads[i] + seconds[path]) / workers,
                    serial_peaks[i],
                    serial_loads[i].get(group, 0.0) + serial[path],
                )
                + collection_loads[i]
                + (collection[path] if file not in collected[i] else 0.0),
                loads[i],
            ),
        )
        loads[lightest] += seconds[path]
        total = serial_loads[lightest].get(group, 0.0) + serial[path]
        serial_loads[lightest][group] = total
        serial_peaks[lightest] = max(serial_peaks[lightest], total)
        if file not in collected[lightest]:
            collection_loads[lightest] += collection[path]
            collected[lightest].add(file)
        groups[lightest].append(path)
    return groups


def slowest_first(nodeids: list[str], durations: dict[str, float], floor: float) -> list[str]:
    seconds = {nodeid: durations.get(re.sub(r"@[^\[\]]*$", "", nodeid), 0.0) for nodeid in nodeids}
    # A group's later tests reuse what its first test built, so the group keeps collection order at its slowest place.
    groups = {nodeid: match[1] for nodeid in nodeids if (match := re.search(r"@([^\[\]]*)$", nodeid))}
    slowest = {}
    for nodeid, group in groups.items():
        slowest[group] = max(slowest.get(group, 0.0), seconds[nodeid])
    seconds.update({nodeid: slowest[group] for nodeid, group in groups.items()})
    slow = sorted((nodeid for nodeid in nodeids if seconds[nodeid] >= floor), key=lambda nodeid: -seconds[nodeid])
    return slow + [nodeid for nodeid in nodeids if seconds[nodeid] < floor]


def warm_imports(modules: list[str], workers: int) -> list[int]:
    pids = []
    for start in range(workers):
        pid = os.fork()
        if pid == 0:
            try:
                # The fork inherits the controller's coverage tracer; its data is never saved, so tracing only costs CPU the collecting workers need.
                sys.settrace(None)
                for module in modules[start::workers]:
                    try:
                        importlib.import_module(module)
                    except BaseException:
                        pass
            finally:
                os._exit(0)
        pids.append(pid)
    return pids


def setup_nodes_in_parallel(manager, putevent) -> list:
    manager.config.hook.pytest_xdist_setupnodes(config=manager.config, specs=manager.specs)
    if hasattr(manager.config, "_tmp_path_factory"):
        manager.config._tmp_path_factory.getbasetemp()
    with ThreadPoolExecutor(len(manager.specs)) as pool:
        return list(pool.map(lambda spec: manager.setup_node(spec, putevent), manager.specs))
