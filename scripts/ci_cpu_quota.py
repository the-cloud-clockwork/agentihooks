import os
import sys
from pathlib import Path, PurePosixPath

CGROUP = Path("/sys/fs/cgroup")


def _v2_limit(path: Path) -> float | None:
    try:
        quota, period = (path / "cpu.max").read_text().split()
    except (OSError, ValueError):
        return None
    return None if quota == "max" else int(quota) / int(period)


def _v1_limit(path: Path) -> float | None:
    try:
        quota = int((path / "cpu.cfs_quota_us").read_text())
        period = int((path / "cpu.cfs_period_us").read_text())
    except (OSError, ValueError):
        return None
    return None if quota <= 0 else quota / period


def _chain(root: Path, relative: str) -> list[Path]:
    path = root / PurePosixPath(relative).relative_to("/")
    return [path, *path.parents[: len(path.relative_to(root).parts)]]


def quota_cpus(root: Path = CGROUP, proc_cgroup: Path = Path("/proc/self/cgroup")) -> float | None:
    try:
        lines = proc_cgroup.read_text().splitlines()
    except OSError:
        lines = []
    limits = []
    for line in lines:
        _, controllers, relative = line.split(":", 2)
        if controllers == "":
            limits += [_v2_limit(p) for p in _chain(root, relative)]
        elif "cpu" in controllers.split(","):
            base = root / "cpu" if (root / "cpu").is_dir() else root / controllers
            limits += [_v1_limit(p) for p in _chain(base, relative)]
    limits = [limit for limit in limits if limit is not None]
    return min(limits) if limits else None


def worker_count(cpus: int, quota: float | None) -> int:
    return max(1, min(cpus, int(quota))) if quota is not None else max(1, cpus)


def main() -> None:
    cpus, quota = len(os.sched_getaffinity(0)), quota_cpus()
    workers = worker_count(cpus, quota)
    print(f"pytest workers {workers}: cpu quota {quota}, affinity {cpus}", file=sys.stderr)
    print(workers)


if __name__ == "__main__":
    main()
