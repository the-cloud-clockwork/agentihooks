"""The lab flakes export: the flake rows of every lab flakes object written within a window, as one flake ledger file.

agentihooks ci lab flakes --out FOLDER [--hours N] [--objects ROOT] reads a local objects folder, or the lab object
store named by the CI_LAB_MINIO_* settings, and writes FOLDER/lab-flakes.jsonl for tests.flake_budget.
"""

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path

from scripts.ci_lab.objects import ROOT, LocalObjects, S3Objects
from scripts.swarm_v2.artifacts.base import ArtifactError
from scripts.swarm_v2.artifacts.object_store import PAGES
from scripts.swarm_v2.filesystem import contain

__all__ = ["NAME", "PREFIX", "command", "declare", "export", "modified"]

NAME = "lab-flakes.jsonl"
PREFIX = f"{ROOT}flakes/"
HOUR_S = 3600
WINDOW_HOURS = 168


def modified(objects: LocalObjects | S3Objects, prefix: str) -> dict[str, float]:
    """Return every object path under a prefix with the epoch second it was written."""
    if isinstance(objects, LocalObjects):
        return {path: contain(objects.root, path).stat().st_mtime for path in objects.keys(prefix)}
    return _listed(objects, prefix)


def export(objects: LocalObjects | S3Objects, since_s: float, out: Path) -> Path:
    """Write the rows of every flakes object written at or after a time to out/lab-flakes.jsonl, in path order."""
    written = modified(objects, PREFIX)
    rows = b"".join(objects.read(path) for path in sorted(written) if written[path] >= since_s)
    out.mkdir(parents=True, exist_ok=True)
    target = out / NAME
    target.write_bytes(rows)
    return target


def declare(parser: argparse.ArgumentParser) -> None:
    """Declare the ci lab flakes arguments: the output folder, the window in hours and an optional objects folder."""
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--hours", type=int, default=WINDOW_HOURS)
    parser.add_argument("--objects", type=Path)


def command(arguments: argparse.Namespace, environ: Mapping[str, str], now_s: float) -> int:
    """Export the window's flake rows and print the file, or print why the store could not be read and exit 1."""
    from botocore.exceptions import BotoCoreError, ClientError  # pyright: ignore[reportMissingTypeStubs]

    try:
        target = export(_store(arguments.objects, environ), now_s - arguments.hours * HOUR_S, arguments.out)
    except (OSError, ValueError, BotoCoreError, ClientError) as error:
        sys.stderr.write(f"ci lab flakes: {error}\n")
        return 1
    print(target)
    return 0


def _listed(objects: S3Objects, prefix: str) -> dict[str, float]:
    request = {"Bucket": objects.bucket, "Prefix": objects.prefix + prefix}
    found: dict[str, float] = {}
    for _ in range(PAGES):
        page = objects.client.list_objects_v2(**request)
        for item in page.get("Contents", ()):
            found[item["Key"].removeprefix(objects.prefix)] = item["LastModified"].timestamp()
        if not page.get("IsTruncated"):
            return found
        request["ContinuationToken"] = page["NextContinuationToken"]
    raise ArtifactError(f"listing {prefix} did not finish within {PAGES} pages")


def _store(root: Path | None, environ: Mapping[str, str]) -> LocalObjects | S3Objects:
    if root is not None:
        if not root.is_dir():
            raise NotADirectoryError(f"not a folder: {root}")
        return LocalObjects(root)
    from scripts.ci_lab.scheduler import storage

    try:
        return storage(environ)
    except KeyError as error:
        raise ValueError(f"missing setting {error.args[0]}") from error
