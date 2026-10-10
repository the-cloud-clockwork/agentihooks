"""Measure artifact put throughput over a backend directory, and what losing that directory does to publication."""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from scripts.swarm_v2.artifacts import base, publication
from scripts.swarm_v2.artifacts.local import LocalBackend

MIB = 1 << 20
SCOPE = base.Scope("benchmark", "artifact-throughput", "bench", 1)


def throughput(store: base.ArtifactStore, sizes_mib: list[int], rounds: int, clock: Callable[[], float]) -> list[dict]:
    rows = []
    for size in sizes_mib:
        spent = 0.0
        for n in range(rounds):
            data = os.urandom(size * MIB)
            start = clock()
            store.put(SCOPE, f"bench-{size}-{n}", data)
            spent += clock() - start
        rows.append({"size_mib": size, "rounds": rounds, "seconds": spent, "mib_per_s": size * rounds / spent})
    return rows


def loss(store: base.ArtifactStore, folder: Path, source: Path) -> dict:
    before = source.read_bytes()
    aside = folder.with_name(folder.name + ".lost")
    folder.rename(aside)
    folder.write_text("unmounted")
    try:
        paused = publication.publish(store, SCOPE, "bench-loss", source, {})
    finally:
        folder.unlink()
        aside.rename(folder)
    resumed = publication.publish(store, SCOPE, "bench-loss", source, {})
    return {
        "while_lost": paused.state,
        "reason": paused.reason,
        "source_intact": source.read_bytes() == before,
        "after_restore": resumed.state,
    }


def run(root: Path, sizes_mib: list[int], rounds: int, clock: Callable[[], float] = time.perf_counter) -> dict:
    folder = root / f"bench-{uuid.uuid4().hex}"
    folder.mkdir()
    try:
        store = base.ArtifactStore(LocalBackend(folder))
        rows = throughput(store, sizes_mib, rounds, clock)
        with tempfile.TemporaryDirectory() as attempt:
            source = Path(attempt) / "checkpoint.bin"
            source.write_bytes(os.urandom(MIB))
            lost = loss(store, folder, source)
    finally:
        shutil.rmtree(folder)
    return {"root": str(root), "throughput": rows, "loss": lost}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.artifacts.benchmark")
    parser.add_argument("root", type=Path)
    parser.add_argument("--sizes", default="1,16", help="comma separated artifact sizes in MiB")
    parser.add_argument("--rounds", type=int, default=3)
    try:
        args = parser.parse_args(argv)
        sizes = [int(size) for size in args.sizes.split(",")]
    except SystemExit as exc:
        return 0 if exc.code == 0 else 64
    except ValueError:
        print(f"--sizes must be whole MiB counts: {args.sizes}", file=sys.stderr)
        return 64
    if not args.root.is_dir():
        print(f"{args.root} is not a folder", file=sys.stderr)
        return 1
    print(json.dumps(run(args.root, sizes, args.rounds), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
