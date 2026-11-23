from collections.abc import Set
from pathlib import Path
from typing import Any

from scripts.ci_lab import run_mutation
from scripts.ci_lab.run_mutation import trampoline
from scripts.ci_mutation import scope

EVERY_LINE = frozenset(range(1, 100_000))


def trampolined_with_no_changed_lines(tree: Path, path: str) -> list[str]:
    # ruleid: lab-trampolines-every-source-file
    return run_mutation.trampoline(tree, path)


def trampolined_by_name_with_no_changed_lines(tree: Path, path: str) -> list[str]:
    # ruleid: lab-trampolines-every-source-file
    return trampoline(tree, path)


def trampolined_with_every_line(tree: Path, path: str) -> list[str]:
    # ruleid: lab-trampolines-every-source-file
    return run_mutation.trampoline(tree, path, set(range(1, 100_000)))


def trampolined_by_name_with_every_line(tree: Path, path: str) -> list[str]:
    # ruleid: lab-trampolines-every-source-file
    return trampoline(tree, path, frozenset(range(1, 100_000)))


def trampolined_with_a_constant_of_every_line(tree: Path, path: str) -> list[str]:
    # ruleid: lab-trampolines-every-source-file
    return run_mutation.trampoline(tree, path, EVERY_LINE)


def trampolined_with_another_modules_constant_of_every_line(tree: Path, path: str) -> list[str]:
    # ruleid: lab-trampolines-every-source-file
    return trampoline(tree, path, run_mutation.EVERY_LINE)


def trampolined_under_the_mutmut_source_paths(tree: Path, settings: dict[str, Any], tracked: list[str]) -> list[str]:
    # ruleid: lab-trampolines-every-source-file
    roots: list[str] = settings["tool"]["mutmut"]["source_paths"]
    return [path for path in tracked if any(Path(path).is_relative_to(root) for root in roots)]


def trampolined_with_the_lines_the_head_changes(tree: Path, base: str, head: str) -> list[str]:
    changes = scope.changes_since(tree, base, head)
    # ok: lab-trampolines-every-source-file
    return [name for path in sorted(changes) for name in run_mutation.trampoline(tree, path, changes[path])]


def trampolined_with_given_lines(tree: Path, path: str, changed: Set[int]) -> list[str]:
    # ok: lab-trampolines-every-source-file
    return trampoline(tree, path, changed)
