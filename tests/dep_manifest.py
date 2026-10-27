"""Per test dependency manifests from coverage contexts, and the tests a change invalidates.

A test depends on every function whose body it executed, on the module level code of every measured file it
touched, on its own test module and conftest chain, and on every repository file it opened. A test that started
a process the tracer cannot follow also depends on every file under the measured source trees.
"""

import argparse
import ast
import hashlib
import io
import json
import re
import statistics
import subprocess
import sys
import tokenize
from pathlib import Path

import yaml

MODULE = "<module>"
MEASURED = ("hooks/", "scripts/")
GLOBAL_FILES = {"pyproject.toml", "uv.lock"}
GLOBAL_PREFIXES = (".github/", ".test_durations")


def _spans(tree: ast.AST) -> list[tuple[int, int, int, str]]:
    spans = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                spans.append((child.lineno, child.body[0].lineno, child.end_lineno, name))
                visit(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return spans


def line_units(source: str) -> dict[int, str]:
    units = {}
    for _, body, end, name in sorted(_spans(ast.parse(source)), key=lambda span: span[0]):
        for line in range(body, end + 1):
            units[line] = name
    return units


def _code_lines(source: str) -> set[int]:
    lines = set()
    skipped = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in skipped:
            lines.update(range(token.start[0], token.end[0] + 1))
    return lines


def unit_hashes(source: str) -> dict[str, str]:
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    try:
        spans = _spans(ast.parse(source))
    except SyntaxError:
        return {MODULE: hashlib.sha256(source.encode()).hexdigest()}
    parts = {}
    inside = set()
    for start, _, end, name in spans:
        parts.setdefault(name, []).append("\n".join(lines[start - 1 : end]))
        inside.update(range(start, end + 1))
    code = _code_lines(source)
    parts[MODULE] = ["\n".join(line for number, line in enumerate(lines, 1) if number in code - inside)]
    return {name: hashlib.sha256("\0".join(texts).encode()).hexdigest() for name, texts in parts.items()}


def changed_units(old: str | None, new: str | None) -> set[str]:
    before = unit_hashes(old) if old is not None else {}
    after = unit_hashes(new) if new is not None else {}
    return {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}


def _test_id(context: str) -> str:
    return context.rsplit("|", 1)[0]


def _executed(coverage_file: Path, root: Path) -> tuple[dict[str, set[str]], dict[str, dict[str, list[int]]]]:
    from coverage import CoverageData

    data = CoverageData(basename=str(coverage_file))
    data.read()
    executed, lines = {}, {}
    for measured in sorted(data.measured_files()):
        path = Path(measured)
        relative = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
        if not (root / relative).is_file():
            continue
        try:
            by_line = line_units((root / relative).read_text(encoding="utf-8"))
        except SyntaxError:
            by_line = {}
        module = f"{relative}::{MODULE}"
        for line, contexts in data.contexts_by_lineno(measured).items():
            unit = f"{relative}::{by_line.get(line, MODULE)}"
            for context in filter(None, contexts):
                test = _test_id(context)
                executed.setdefault(test, set()).update((unit, module))
                lines.setdefault(test, {}).setdefault(relative, []).append(line)
    return executed, {
        test: {path: sorted(set(found)) for path, found in paths.items()} for test, paths in lines.items()
    }


def build(coverage_file: Path, inputs: list[Path], root: Path, commit: str, environment: str) -> dict:
    executed, lines = _executed(coverage_file, root)
    units = sorted(set().union(*executed.values()))
    index = {name: number for number, name in enumerate(units)}
    tests = {
        test: {"units": sorted(index[name] for name in found), "files": [], "escapes": False}
        for test, found in executed.items()
    }
    for report in inputs:
        for line in filter(str.strip, report.read_text().splitlines()):
            seen = json.loads(line)
            test = tests.setdefault(seen["test"], {"units": [], "files": [], "escapes": False})
            test["files"] = sorted(set(test["files"]) | set(seen["files"]))
            test["escapes"] = test["escapes"] or seen["escapes"]
    return {
        "version": 2,
        "commit": commit,
        "environment": environment,
        "units": units,
        "tests": dict(sorted(tests.items())),
        "lines": lines,
    }


def pack(manifest: dict) -> dict:
    files, spans = {}, {}
    packed = {key: value for key, value in manifest.items() if key not in ("tests", "lines")}
    packed["tests"] = {
        test: [found["units"], files.setdefault(tuple(found["files"]), len(files)), found["escapes"]]
        for test, found in manifest["tests"].items()
    }
    if "lines" in manifest:
        packed["lines"] = {
            test: [spans.setdefault((path, tuple(found)), len(spans)) for path, found in paths.items()]
            for test, paths in manifest["lines"].items()
        }
    packed["files"] = [list(found) for found in files]
    packed["spans"] = [[path, list(found)] for path, found in spans]
    return packed


def unpack(packed: dict) -> dict:
    files, spans = packed.pop("files"), packed.pop("spans")
    packed["tests"] = {
        test: {"units": units, "files": files[index], "escapes": escapes}
        for test, (units, index, escapes) in packed["tests"].items()
    }
    if "lines" in packed:
        packed["lines"] = {test: dict(spans[index] for index in found) for test, found in packed["lines"].items()}
    return packed


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _show(repo: Path, revision: str, path: str) -> str | None:
    shown = subprocess.run(["git", "-C", str(repo), "show", f"{revision}:{path}"], capture_output=True)
    return shown.stdout.decode("utf-8", "replace") if shown.returncode == 0 else None


def changes(repo: Path, base: str, head: str) -> list[tuple[str, str | None, str | None]]:
    paths = _git(repo, "diff", "--no-renames", "--name-only", base, head).splitlines()
    # A numstat diff fetches every missing blob of a blobless clone in one request; each show would fetch its own.
    _git(repo, "diff", "--no-renames", "--numstat", base, head, "--", "*.py")
    found = []
    for path in paths:
        if path.endswith(".py"):
            found.append((path, _show(repo, base, path), _show(repo, head, path)))
        else:
            found.append((path, None, None))
    return found


def _global(path: str) -> bool:
    if path in GLOBAL_FILES or path.startswith(GLOBAL_PREFIXES):
        return True
    return path.endswith(".py") and not path.startswith(MEASURED + ("tests/",))


def workflow_execution_changed(old: str | None, new: str | None) -> bool:
    try:
        before, after = (yaml.safe_load(text) for text in (old, new))
        for workflow in (before, after):
            jobs = workflow["jobs"]
            if not isinstance(jobs, dict) or not all(isinstance(jobs[name], dict) for name in ("unit", "split")):
                return True
        pending = ["unit", "unit-selected"]
        producers = set()
        while pending:
            name = pending.pop()
            if name in producers:
                continue
            producers.add(name)
            for workflow in (before, after):
                job = workflow["jobs"].get(name, {})
                needs = job.get("needs", [])
                pending.extend([needs] if isinstance(needs, str) else needs)
        return any(before.get(key) != after.get(key) for key in ("env", "defaults")) or any(
            before["jobs"].get(name) != after["jobs"].get(name) for name in producers
        )
    except (TypeError, KeyError, AttributeError, yaml.YAMLError):
        return True


def global_input(path: str, old: str | None, new: str | None, version: int | None) -> bool:
    if path.startswith(".github/workflows/") and not path.endswith(".py") and version == 2:
        return path == ".github/workflows/test.yml" and workflow_execution_changed(old, new)
    return _global(path)


def invalidated(manifest: dict, changed: list[tuple[str, str | None, str | None]]) -> tuple[set[str], str | None]:
    tests = manifest["tests"]
    for path, old, new in changed:
        if global_input(path, old, new, manifest.get("version")):
            return set(tests), path
    index = {name: number for number, name in enumerate(manifest["units"])}
    units = set()
    hit = set()
    source_changed = False
    for path, old, new in changed:
        if path.startswith(MEASURED):
            source_changed = True
            if path.endswith(".py"):
                units.update(index[f"{path}::{name}"] for name in changed_units(old, new) if f"{path}::{name}" in index)
                continue
        if path.startswith("tests/") and path.endswith(".py"):
            source_changed = True
            if path.rsplit("/", 1)[-1].startswith("test_"):
                hit.update(test for test in tests if test.split("::", 1)[0] == path)
            else:
                hit.update(tests)
            continue
        if path.startswith(("tests/", ".github/workflows/")):
            source_changed = True
        hit.update(test for test, found in tests.items() if path in found["files"])
    for test, found in tests.items():
        if (source_changed and found["escapes"]) or units.intersection(found["units"]):
            hit.add(test)
    return hit, None


def share(manifest: dict, repo: Path, ref: str, merges: int) -> dict:
    commits = _git(repo, "rev-list", "--first-parent", f"-n{merges}", ref).split()
    total = len(manifest["tests"])
    rows = []
    for commit in commits:
        hit, cause = invalidated(manifest, changes(repo, f"{commit}^1", commit))
        rows.append({"commit": commit, "invalidated": len(hit), "reusable": 1 - len(hit) / total, "global": cause})
    shares = [row["reusable"] for row in rows]
    return {
        "tests": total,
        "merges": len(rows),
        "mean": statistics.mean(shares) if shares else 0.0,
        "median": statistics.median(shares) if shares else 0.0,
        "rows": rows,
    }


def node(test: str) -> str:
    return re.sub(r"@[^\[\]]*$", "", test)


def plan(manifest: dict, changed: list[tuple[str, str | None, str | None]], run: str) -> dict:
    hit, cause = invalidated(manifest, changed)
    reused = sorted({node(test) for test in manifest["tests"] if test not in hit})
    return {"run": run, "commit": manifest["commit"], "global": cause, "invalidated": len(hit), "tests": reused}


def deselect(reused: list[str], collected: list[str]) -> dict:
    tests = set(reused).intersection(collected)
    files = {}
    for nodeid in collected:
        path = nodeid.split("::", 1)[0]
        files[path] = files.get(path, True) and nodeid in tests
    return {"tests": sorted(tests), "files": sorted(path for path, whole in files.items() if whole)}


def branch_only(plans: list[dict], tests: list[str]) -> list[str]:
    dev = {test for plan in plans if "runs" not in plan for test in plan["tests"]}
    return [test for test in tests if test not in dev]


def _deselect_plans(paths: list[Path], collected: Path, out: Path) -> int:
    plans = [json.loads(path.read_text()) for path in paths]
    chosen = deselect([test for plan in plans for test in plan["tests"]], json.loads(collected.read_text()))
    if len(plans) > 1 or any("runs" in plan for plan in plans):
        chosen["branch"] = branch_only(plans, chosen["tests"])
    out.write_text(json.dumps(chosen))
    print(f"Reusing {len(chosen['tests'])} collected tests; {len(chosen['files'])} test files are not collected")
    if "branch" in chosen:
        print(f"{len(chosen['branch'])} of them passed on earlier runs of this pull request")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    built = commands.add_parser("build")
    built.add_argument("--coverage", type=Path, required=True)
    built.add_argument("--inputs", type=Path, required=True)
    built.add_argument("--root", type=Path, default=Path.cwd())
    built.add_argument("--commit", required=True)
    built.add_argument("--environment", required=True)
    built.add_argument("--out", type=Path, required=True)
    built.add_argument("--packed", type=Path)
    checked = commands.add_parser("invalidated")
    checked.add_argument("--manifest", type=Path, required=True)
    checked.add_argument("--repo", type=Path, default=Path.cwd())
    checked.add_argument("--base")
    checked.add_argument("--head", default="HEAD")
    measured = commands.add_parser("share")
    measured.add_argument("--manifest", type=Path, required=True)
    measured.add_argument("--repo", type=Path, default=Path.cwd())
    measured.add_argument("--ref", default="origin/dev")
    measured.add_argument("--merges", type=int, default=40)
    deselected = commands.add_parser("deselect")
    deselected.add_argument("--plan", type=Path, action="append", required=True)
    deselected.add_argument("--collected", type=Path, required=True)
    deselected.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "deselect":
        return _deselect_plans(args.plan, args.collected, args.out)
    if args.command == "build":
        reports = sorted(args.inputs.rglob("*.jsonl"))
        if not reports:
            print(f"::error::No dependency input reports under {args.inputs}.", file=sys.stderr)
            return 1
        manifest = build(args.coverage, reports, args.root.resolve(), args.commit, args.environment)
        if not manifest["tests"]:
            print("::error::The coverage data holds no per test contexts.", file=sys.stderr)
            return 1
        args.out.write_text(json.dumps(manifest, separators=(",", ":")))
        if args.packed:
            args.packed.write_text(json.dumps(pack(manifest), separators=(",", ":")))
        escaping = sum(found["escapes"] for found in manifest["tests"].values())
        print(f"Dependency manifest: {len(manifest['tests'])} tests, {len(manifest['units'])} units, {escaping} escape")
        return 0
    manifest = json.loads(args.manifest.read_text())
    if args.command == "invalidated":
        hit, cause = invalidated(manifest, changes(args.repo, args.base or manifest["commit"], args.head))
        print(json.dumps({"global": cause, "tests": sorted(hit)}, indent=2))
        return 0
    found = share(manifest, args.repo, args.ref, args.merges)
    print(json.dumps(found, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
