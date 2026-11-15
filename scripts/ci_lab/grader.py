"""The protected grader revision a lab grade runs its static gates from, and the digest of each gate's import closure."""

import ast
import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

__all__ = ["closure", "digest", "revision"]


def revision(payload: Mapping[str, Any], dev_head: str) -> str:
    """Return the pull request base sha of a pull request event, else the dev head a push grades with."""
    pull = payload.get("pull_request")
    return dev_head if pull is None else str(pull["base"]["sha"])


def _source(root: Path, module: str) -> Path | None:
    *package, name = module.split(".")
    for candidate in (root.joinpath(*package, name, "__init__.py"), root.joinpath(*package, f"{name}.py")):
        if candidate.is_file():
            return candidate
    return None


def _anchor(node: ast.ImportFrom, module: str, package: bool) -> str:
    parts = module.split(".")
    keep = len(parts) - node.level + package if node.level else 0
    return ".".join([*parts[:keep], *filter(None, [node.module])])


def _imports(source: Path, module: str) -> Iterator[str]:
    package = source.name == "__init__.py"
    for node in ast.walk(ast.parse(source.read_bytes())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            anchor = _anchor(node, module, package)
            yield anchor
            yield from (f"{anchor}.{alias.name}" for alias in node.names)


def closure(root: Path, modules: Iterable[str]) -> tuple[str, ...]:
    """Return the repository files under root that the modules import, transitively, sorted."""
    pending = [name for module in modules for name in (module, f"{module}.__main__")]
    sources: dict[str, Path | None] = {}
    while pending:
        module = pending.pop()
        if module in sources:
            continue
        sources[module] = source = _source(root, module)
        parts = module.split(".")
        pending.extend(".".join(parts[:end]) for end in range(1, len(parts)))
        if source is not None:
            pending.extend(_imports(source, module))
    return tuple(sorted(path.relative_to(root).as_posix() for path in sources.values() if path is not None))


def digest(root: Path, modules: Iterable[str]) -> str:
    """Return the sha256 over the repository paths and content digests of the modules' closure."""
    record = [[path, hashlib.sha256((root / path).read_bytes()).hexdigest()] for path in closure(root, modules)]
    return hashlib.sha256(json.dumps(record, separators=(",", ":")).encode()).hexdigest()
