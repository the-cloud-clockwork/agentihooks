import importlib
import os
from pathlib import Path

# Every xdist worker of a shard rewrites each file it collects, while stored durations split between workers; measured.
SECONDS_PER_SOURCE_BYTE = 3e-6


def discover_test_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py"))


def source_sizes(root: Path, files: list[str]) -> dict[str, int]:
    return {path: (root / path).stat().st_size for path in files}


def assign_files(durations: dict[str, float], files: list[str], shards: int, sizes: dict[str, int]) -> list[list[str]]:
    seconds = {path: sizes.get(path, 0) * SECONDS_PER_SOURCE_BYTE for path in files}
    for nodeid, duration in durations.items():
        path = nodeid.split("::", 1)[0]
        if path in seconds:
            seconds[path] += duration
    loads = [0.0] * shards
    groups: list[list[str]] = [[] for _ in range(shards)]
    for path in sorted(files, key=lambda f: (-seconds[f], f)):
        lightest = loads.index(min(loads))
        loads[lightest] += seconds[path]
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
