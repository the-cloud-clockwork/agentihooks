"""Keep only each mutation group's own evidence in a mutation output folder.

Usage: mutation_evidence.py FOLDER

A mutation shard that did not succeed uploads FOLDER. Each group folder in it, named
<index>-<suffix> by the mutation runner, holds the workspace the group ran in, which
every shard copies from the same commit, beside the files the group's own run wrote.
This removes the workspace and keeps run.log, report.log, results.json, killers/,
mutants/mutmut-stats.json and every mutants/**/*.py.meta. Files directly in FOLDER,
such as report.json, and folders with any other name stay as they are. A symbolic
link in a group folder is removed and never followed. Every group folder is read
before anything is removed, so a folder the walk cannot read fails the run with
nothing removed.
"""

import os
import re
import sys
from pathlib import Path
from typing import NoReturn

GROUP = re.compile(r"\d+-.+")
OWN_FILES = frozenset({"run.log", "report.log", "results.json", "mutants/mutmut-stats.json"})


def owned(relative: Path) -> bool:
    """Return whether a path inside a group folder names a file the group's own run wrote."""
    return (
        relative.as_posix() in OWN_FILES
        or (relative.parts[0] == "killers" and len(relative.parts) > 1)
        or (relative.parts[0] == "mutants" and relative.name.endswith(".py.meta"))
    )


def raise_unread(error: OSError) -> NoReturn:
    """Raise what the walk could not read, so an unread folder never stays in the upload unreported."""
    raise error


def entries(group: Path) -> list[Path]:
    """Return every file and symbolic link in group, raising on a folder the walk cannot read."""
    found: list[Path] = []
    for folder, folders, files in os.walk(group, onerror=raise_unread):
        here = Path(folder)
        found += [here / name for name in files] + [here / name for name in folders if (here / name).is_symlink()]
    return found


def keep_own(group: Path, paths: list[Path]) -> tuple[list[int], list[int]]:
    """Remove the workspace entries among paths and the folders they leave empty, returning kept and removed sizes."""
    kept: list[int] = []
    removed: list[int] = []
    for path in paths:
        size = path.lstat().st_size
        if path.is_symlink() or not owned(path.relative_to(group)):
            path.unlink()
            removed.append(size)
        else:
            kept.append(size)
    for folder, folders, _ in os.walk(group, topdown=False, onerror=raise_unread):
        for path in (Path(folder) / name for name in folders):
            if not path.is_symlink() and path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    return kept, removed


def main(argv: list[str]) -> int:
    """Keep each group's own evidence in the folder argv names and print what stayed and what went."""
    if len(argv) != 2:
        print("usage: mutation_evidence.py FOLDER", file=sys.stderr)
        return 2
    folder = Path(argv[1])
    found = sorted(folder.iterdir()) if folder.is_dir() else []
    groups = [path for path in found if path.is_dir() and not path.is_symlink() and GROUP.fullmatch(path.name)]
    walked = [(group, entries(group)) for group in groups]
    kept: list[int] = []
    removed: list[int] = []
    for group, paths in walked:
        kept_here, removed_here = keep_own(group, paths)
        kept += kept_here
        removed += removed_here
    print(
        f"Mutation evidence: kept {len(kept)} files ({sum(kept)} bytes) and removed {len(removed)} workspace files"
        f" ({sum(removed)} bytes) in {len(groups)} group folders"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
