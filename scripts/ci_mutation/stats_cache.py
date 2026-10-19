import functools
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from scripts.ci_mutation.scope import imported_names, is_test_file, module_name, selects, test_file

SCHEMA = 2
CONTEXT_FIELDS = ("schema", "sources", "changes", "options", "runtime")
KEY_FIELDS = ("path", "tests")
DYNAMIC_IMPORTS = ("importlib", "__import__")
TRACKED = ("git", "ls-files", "-z")
report = functools.partial(print, flush=True)
encode = functools.partial(json.dumps, sort_keys=True)


def test_inputs(
    root: Path, path: str, modules: dict[str, str], records: dict[str, tuple[str, set[str]]]
) -> dict[str, str]:
    pending = {path}
    inputs = {}
    while pending:
        current = pending.pop()
        if current not in records:
            text = (root / current).read_text()
            dependencies = (
                set(modules.values())
                if any(marker in text for marker in DYNAMIC_IMPORTS)
                else {modules[name] for name in imported_names(text, Path(current).parent.parts) if name in modules}
            )
            records[current] = (hashlib.sha256(text.encode()).hexdigest(), dependencies)
        digest, dependencies = records[current]
        inputs[current] = digest
        pending.update(dependencies - inputs.keys())
    return inputs


def collect_cached_stats(
    root: Path,
    folder: Path,
    changes: dict,
    files: list[str],
    options: list[str],
    collect: Callable[[list[str]], list[dict]],
) -> list[dict]:
    tracked = subprocess.check_output(TRACKED, cwd=root, text=True).split("\0")
    sources = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in tracked
        if path and not is_test_file(path) and (root / path).is_file()
    }
    versions = {distribution.metadata["Name"]: distribution.version for distribution in metadata.distributions()}
    context = dict(zip(CONTEXT_FIELDS, (SCHEMA, sources, changes, options, [sys.version, sys.platform, versions])))
    modules = {module_name(Path(path)): path for path in tracked if Path(path).suffix == ".py"}
    records = {}
    folder.mkdir(parents=True, exist_ok=True)
    keys = {
        path: hashlib.sha256(
            encode(
                {**context, **dict(zip(KEY_FIELDS, (path, test_inputs(root, test_file(path), modules, records))))}
            ).encode()
        ).hexdigest()
        for path in files
    }
    stored = {path: folder / f"{key}.json" for path, key in keys.items()}
    reused = [json.loads(file.read_text()) for file in stored.values() if file.is_file()]
    missing = [path for path, file in stored.items() if not file.is_file()]
    report(f"Mutation stats cache: reused {len(reused)} test files; collect {len(missing)}")
    fresh = collect(missing) if missing else []
    if any(result["status"] != 0 for result in fresh):
        raise SystemExit(1)
    for path in missing:
        durations = {
            test: seconds for result in fresh for test, seconds in result["durations"].items() if selects(path, test)
        }
        tests = {
            function: sorted(
                {test for result in fresh for test in result["tests"].get(function, []) if test in durations}
            )
            for function in {function for result in fresh for function in result["tests"]}
        }
        (folder / f"{keys[path]}.json").write_text(
            json.dumps(
                {
                    "status": 0,
                    "tests": tests,
                    "durations": durations,
                    "cpu": sum(result["cpu"] for result in fresh) / len(missing),
                }
            )
        )
    return reused + fresh
