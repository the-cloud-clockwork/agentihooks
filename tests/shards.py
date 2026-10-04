from pathlib import Path


def discover_test_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py"))


def assign_files(durations: dict[str, float], files: list[str], shards: int) -> list[list[str]]:
    seconds = dict.fromkeys(files, 0.0)
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
