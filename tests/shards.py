import ast
import importlib
import os
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
        names = {
            ast.unparse(
                node.args[0]
                if node.args
                else next((kw.value for kw in node.keywords if kw.arg == "name"), ast.Constant("default"))
            )
            for node in ast.walk(ast.parse((root / path).read_text()))
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
    durations: dict[str, float], shards: int, grouped: set[str] | dict[str, str], workers: int
) -> list[list[str]]:
    serial = {node: seconds if node.split("::", 1)[0] in grouped else 0.0 for node, seconds in durations.items()}
    serial_groups = (
        {node: grouped.get(node.split("::", 1)[0], "") for node in durations} if isinstance(grouped, dict) else None
    )
    return _assign_work(durations, serial, dict.fromkeys(durations, 0.0), shards, workers, serial_groups)


def _assign_work(
    seconds: dict[str, float],
    serial: dict[str, float],
    collection: dict[str, float],
    shards: int,
    workers: int,
    serial_groups: dict[str, str] | None = None,
) -> list[list[str]]:
    loads = [0.0] * shards
    serial_loads: list[dict[str, float]] = [{} for _ in range(shards)]
    collection_loads = [0.0] * shards
    groups: list[list[str]] = [[] for _ in range(shards)]
    for path in sorted(seconds, key=lambda f: (-max(seconds[f] / workers, serial[f]) - collection[f], f)):
        group = serial_groups[path] if serial_groups is not None else ""
        lightest = min(
            range(shards),
            key=lambda i: (
                max(
                    (loads[i] + seconds[path]) / workers,
                    max(serial_loads[i].values(), default=0.0),
                    serial_loads[i].get(group, 0.0) + serial[path],
                )
                + collection_loads[i]
                + collection[path],
                loads[i],
            ),
        )
        loads[lightest] += seconds[path]
        serial_loads[lightest][group] = serial_loads[lightest].get(group, 0.0) + serial[path]
        collection_loads[lightest] += collection[path]
        groups[lightest].append(path)
    return groups


def slowest_first(nodeids: list[str], durations: dict[str, float], floor: float) -> list[str]:
    seconds = {nodeid: durations.get(nodeid.split("@", 1)[0], 0.0) for nodeid in nodeids}
    slow = sorted((nodeid for nodeid in nodeids if seconds[nodeid] >= floor), key=lambda nodeid: -seconds[nodeid])
    return slow + [nodeid for nodeid in nodeids if seconds[nodeid] < floor]


def warm_imports(modules: list[str], workers: int) -> list[int]:
    pids = []
    for start in range(workers):
        pid = os.fork()
        if pid == 0:
            try:
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
