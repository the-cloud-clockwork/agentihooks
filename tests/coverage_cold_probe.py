"""Focused proof probe: time the coverage database check on this runner with warm, evicted and prefetched databases."""

import json
import os
import random
import shutil
import sqlite3
import time
import warnings
from contextlib import closing
from pathlib import Path

from coverage import CoverageData

from tests import coverage_check

FILES = 1324
CONTEXTS = 7120
FILES_PER_CONTEXT = 15
ARCS_PER_FILE = 15
COPIES = 8
BUDGET = 80.0
SAMPLES = 4000
MODES = ("integrity_check", "prefetch_integrity_check", "quick_check", "integrity_check", "prefetch_integrity_check")
PRESSURE = ("/proc/loadavg", "/proc/pressure/io", "/proc/pressure/memory", "/sys/fs/cgroup/memory.pressure")


def counters() -> dict[str, int]:
    """Return this process's I/O counters."""
    lines = Path("/proc/self/io").read_text().splitlines()
    return {key: int(value) for key, value in (line.split(": ") for line in lines)}


def pressure() -> dict[str, str]:
    """Return the load and pressure lines this runner exposes."""
    return {name: Path(name).read_text().strip() for name in PRESSURE if Path(name).exists()}


def build(path: Path) -> None:
    """Record a coverage database shaped like a unit shard's, one context per test touching several files."""
    rng = random.Random(5448)
    names = [f"/home/runner/_work/agentihooks/agentihooks/hooks/module_{index}.py" for index in range(FILES)]
    data = CoverageData(basename=str(path))
    for context in range(CONTEXTS):
        data.set_context(f"tests/test_probe_{context % 400}.py::test_case_{context}|run")
        arcs = {}
        for name in rng.sample(names, FILES_PER_CONTEXT):
            start = rng.randrange(1, 2000)
            arcs[name] = [(start + step, start + step + 1) for step in range(ARCS_PER_FILE)]
        data.add_arcs(arcs)
    data.write()


def evict(paths: list[Path]) -> None:
    """Drop every file's pages from the page cache, as memory pressure on a busy node does."""
    for path in paths:
        handle = os.open(path, os.O_RDONLY)
        try:
            os.fsync(handle)
            os.posix_fadvise(handle, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(handle)


def prefetch(path: Path) -> None:
    """Read the file once from start to end."""
    with path.open("rb", buffering=0) as handle:
        while handle.read(1 << 20):
            pass


def pragma(path: Path, name: str, deadline: float) -> str:
    """Run one read only SQLite check pragma, interrupted at the deadline."""
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
        try:
            return db.execute(f"pragma {name}").fetchone()[0]
        except sqlite3.OperationalError as error:
            return f"stopped: {error}"


def timed(paths: list[Path], mode: str) -> dict[str, float | int | str]:
    """Check every path in one mode and report seconds, disk megabytes and files finished."""
    before, start = counters(), time.monotonic()
    finished = 0
    for path in paths:
        if mode.startswith("prefetch_"):
            prefetch(path)
        if pragma(path, mode.removeprefix("prefetch_"), start + BUDGET) != "ok":
            break
        finished += 1
    after = counters()
    return {
        "mode": mode,
        "seconds": round(time.monotonic() - start, 2),
        "files": finished,
        "disk_mb": round((after["read_bytes"] - before["read_bytes"]) / 1e6, 1),
        "read_calls": after["syscr"] - before["syscr"],
    }


def random_reads(path: Path) -> float:
    """Return the mean milliseconds of one 4 KiB read at a random offset of an evicted file."""
    evict([path])
    pages = path.stat().st_size // 4096
    rng = random.Random(1)
    handle = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(handle, 0, 0, os.POSIX_FADV_RANDOM)
        start = time.monotonic()
        for _ in range(SAMPLES):
            os.pread(handle, 4096, rng.randrange(pages) * 4096)
        return (time.monotonic() - start) * 1000 / SAMPLES
    finally:
        os.close(handle)


def probe(root: Path) -> dict[str, object]:
    """Build shard sized databases under root, time every check mode on them and return the report."""
    report: dict[str, object] = {"cpus": os.cpu_count(), "pressure_before": pressure()}
    source = root / "source.coverage"
    start = time.monotonic()
    build(source)
    report["build_seconds"] = round(time.monotonic() - start, 1)
    with closing(sqlite3.connect(source)) as db:
        report["pages"] = db.execute("pragma page_count").fetchone()[0]
        report["arcs"] = db.execute("select count(*) from arc").fetchone()[0]
    report["megabytes"] = round(source.stat().st_size / 1e6, 1)
    paths = []
    for index in range(COPIES):
        copy = root / f"coverage-3.12-{index}" / ".coverage"
        copy.parent.mkdir()
        copy.write_bytes(source.read_bytes())
        paths.append(copy)
    report["problem"] = coverage_check.problem(paths[0])
    report["warm"] = timed(paths, "integrity_check")
    report["random_read_ms"] = round(random_reads(paths[0]), 3)
    passes = []
    for mode in MODES:
        evict(paths)
        passes.append(timed(paths, mode))
    report["passes"] = passes
    report["pressure_after"] = pressure()
    return report


def test_the_coverage_check_on_this_runner(tmp_path: Path) -> None:
    """Keep the timing report of every check mode on this runner for the proof."""
    root = Path(os.environ.get("RUNNER_TEMP", str(tmp_path))) / "coverage-cold-probe"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    try:
        report = probe(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    text = json.dumps(report, indent=1)
    out = Path(os.environ.get("PROOF_REPORT_DIR", str(tmp_path)))
    out.mkdir(parents=True, exist_ok=True)
    (out / "coverage-cold-probe.json").write_text(text)
    warnings.warn(text, stacklevel=1)
    assert report["problem"] is None
