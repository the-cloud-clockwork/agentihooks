import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

CATALOG = Path("tests/ci_lab/plants")
IDENTITY = {
    "GIT_AUTHOR_NAME": "CI lab plant",
    "GIT_AUTHOR_EMAIL": "ci-lab@example.invalid",
    "GIT_COMMITTER_NAME": "CI lab plant",
    "GIT_COMMITTER_EMAIL": "ci-lab@example.invalid",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
}


@dataclass(frozen=True)
class Pair:
    name: str
    base: str
    green: str
    red: str
    gate: str
    reason: str


def _git(root: Path, *arguments: str, data: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        input=data,
        env={**os.environ, **(environ or {})},
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout


def prepare(root: Path, name: str, base: str) -> Pair:
    if re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)*", name) is None:
        raise ValueError("a plant name contains lowercase words and hyphens")
    fixture = json.loads((root / CATALOG / f"{name}.json").read_text())
    revision = _git(root, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
    sources: dict[str, str] = {}
    modes: dict[str, str] = {}
    for edit in fixture["edits"]:
        path = edit["path"]
        if path not in sources:
            entry = _git(root, "ls-tree", revision, "--", path).split()
            modes[path] = entry[0] if entry else "100644"
            sources[path] = _git(root, "show", f"{revision}:{path}") if entry else ""
        if sources[path].count(edit["before"]) != 1:
            raise ValueError(f"{name}: {path} must match its edit exactly once")
        sources[path] = sources[path].replace(edit["before"], edit["after"])
    directory = (root / _git(root, "rev-parse", "--git-path", "index").strip()).parent
    with TemporaryDirectory(dir=directory) as temporary:
        environ = {**os.environ, **IDENTITY, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _git(root, "read-tree", revision, environ=environ)
        for path, source in sources.items():
            blob = _git(root, "hash-object", "-w", "--stdin", data=source).strip()
            _git(root, "update-index", "--cacheinfo", f"{modes[path]},{blob},{path}", environ=environ)
        tree = _git(root, "write-tree", environ=environ).strip()
    original = _git(root, "rev-parse", f"{revision}^{{tree}}").strip()
    green = _git(root, "commit-tree", original, "-p", revision, data=f"CI lab {name} twin\n", environ=IDENTITY).strip()
    red = _git(root, "commit-tree", tree, "-p", green, data=f"CI lab {name} plant\n", environ=IDENTITY).strip()
    return Pair(name, revision, green, red, fixture["gate"], fixture["reason"])
