"""Keep only each mutation group's own evidence in a mutation output folder.

Usage: mutation_evidence.py FOLDER

A mutation shard that did not succeed uploads FOLDER. Each group folder in it, named
<index>-<suffix> by the mutation runner, holds the workspace the group ran in, which
every shard copies from the same commit, beside the files the group's own run wrote.
This removes the workspace and keeps run.log, report.log, results.json, killers/,
mutants/mutmut-stats.json and every mutants/**/*.py.meta. Files directly in FOLDER,
such as report.json, and folders with any other name stay as they are. A symbolic
link in a group folder is removed and never followed.
"""

import os
import re
import sys
from pathlib import Path

GROUP = re.compile(r"\d+-.+")
OWN = frozenset({"run.log", "report.log", "results.json", "mutants/mutmut-stats.json"})


def owned(relative: Path) -> bool:
    """Return whether a path inside a group folder names a file the group's own run wrote."""
    return (
        relative.as_posix() in OWN
        or relative.parts[0] == "killers"
        or (relative.parts[0] == "mutants" and relative.name.endswith(".py.meta"))
    )


def keep_own(group: Path) -> tuple[list[int], list[int]]:
    """Remove every workspace file and link in group, returning the sizes of the kept and the removed entries."""
    kept: list[int] = []
    removed: list[int] = []
    for folder, folders, files in os.walk(group, topdown=False):
        here = Path(folder)
        for path in [here / name for name in files] + [here / name for name in folders if (here / name).is_symlink()]:
            size = path.lstat().st_size
            if path.is_symlink() or not owned(path.relative_to(group)):
                path.unlink()
                removed.append(size)
            else:
                kept.append(size)
        for path in (here / name for name in folders):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    return kept, removed


def main(argv: list[str]) -> int:
    """Keep each group's own evidence in the folder argv names and print what stayed and what went."""
    if len(argv) != 2:
        print("usage: mutation_evidence.py FOLDER", file=sys.stderr)
        return 2
    folder = Path(argv[1])
    entries = sorted(folder.iterdir()) if folder.is_dir() else []
    groups = [path for path in entries if path.is_dir() and not path.is_symlink() and GROUP.fullmatch(path.name)]
    kept: list[int] = []
    removed: list[int] = []
    for group in groups:
        own, workspace = keep_own(group)
        kept += own
        removed += workspace
    print(
        f"Mutation evidence: kept {len(kept)} files ({sum(kept)} bytes) and removed {len(removed)} workspace files"
        f" ({sum(removed)} bytes) in {len(groups)} group folders"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
