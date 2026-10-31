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
DYNAMIC = {
    "import_module",
    "__import__",
    "spec_from_file_location",
    "module_from_spec",
    "exec_module",
    "run_module",
    "run_path",
}


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


def _module_file(root: Path, name: str, folder: str = "") -> str | None:
    found = root.joinpath(folder, *name.split("."))
    for candidate in (found.with_suffix(".py"), found / "__init__.py"):
        if candidate.is_file():
            return candidate.relative_to(root).as_posix()
    return None


def _by_name(tree: ast.AST) -> set[str]:
    names = set(DYNAMIC)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("importlib", "importlib.util", "builtins", "runpy"):
            names.update(alias.asname for alias in node.names if alias.name in DYNAMIC and alias.asname)
        elif isinstance(node, ast.Assign) and getattr(node.value, "attr", getattr(node.value, "id", "")) in DYNAMIC:
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _imported(node: ast.AST, package: tuple[str, ...]) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        base = package[: len(package) - node.level + 1] if node.level else ()
        module = ".".join([*base, *([node.module] if node.module else [])])
        return [module, *(f"{module}.{alias.name}" for alias in node.names)]
    return []


def module_graph(root: Path, path: str) -> tuple[list[tuple[str, int | None]], list[int | None]]:
    """The repository modules a file imports and its calls that import by name.

    Each carries the line of its statement inside a function, or None at module level where it runs on import.
    A name resolves from the repository root and from the file's own folder. A file that cannot be parsed reports
    a module level call so its importers escape.
    """
    try:
        tree = ast.parse((root / path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, SyntaxError, ValueError):
        return [], [None]
    package, folder, by_name = Path(path).parent.parts, Path(path).parent.as_posix(), _by_name(tree)
    edges, dynamic = [], []
    pending: list[tuple[ast.AST, bool, int | None]] = [(tree, False, None)]
    while pending:
        node, inside, statement = pending.pop()
        statement = node.lineno if isinstance(node, ast.stmt) else statement
        line = statement if inside else None
        called = getattr(getattr(node, "func", None), "attr", getattr(getattr(node, "func", None), "id", ""))
        if isinstance(node, ast.Call) and called in by_name:
            dynamic.append(line)
        for name in filter(None, _imported(node, package)):
            found = {_module_file(root, name), _module_file(root, name, folder)} - {None, path}
            edges.extend((target, line) for target in found)
        body = node.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) else []
        body = body if isinstance(body, list) else [body]
        pending.extend(
            (child, inside or any(child is part for part in body), statement) for child in ast.iter_child_nodes(node)
        )
    return edges, dynamic


def _packages(root: Path, path: str) -> list[str]:
    parts = path.split("/")[:-1]
    found = ["/".join([*parts[:end], "__init__.py"]) for end in range(1, len(parts) + 1)]
    return [package for package in found if package != path and (root / package).is_file()]


def attribute(tests: dict, modules: dict, root: Path, lines: dict) -> None:
    """Give each test the collection reads and escapes of every repository module it reaches.

    A test reaches its test module, every measured file it executed, the packages holding each, and every module
    those import at module level or through a function level import line the test executed, plus every module an
    import statement named while the test module was collected. Files outside the measured trees are not traced,
    so all their imports count. A call that imports by name on a counted line makes the test escape, since the
    module it loads is unknown.
    """
    graphs, reached = {}, {}

    def graph(path: str) -> tuple[list[tuple[str, int | None]], list[int | None], list[str]]:
        if path not in graphs:
            graphs[path] = (*module_graph(root, path), _packages(root, path))
        return graphs[path]

    for test, found in tests.items():
        ran = lines.get(test, {})
        start = frozenset({test.split("::", 1)[0], *ran})
        taken = set()
        for path, numbers in ran.items():
            edges, dynamic, _ = graph(path)
            wanted = {line for _, line in edges if line is not None} | {line for line in dynamic if line is not None}
            taken.update((path, line) for line in wanted.intersection(numbers))
        key = start, frozenset(taken)
        if key not in reached:
            seen, pending, escapes = set(start), list(start), False
            while pending:
                path = pending.pop()
                edges, dynamic, packages = graph(path)
                counted = not path.startswith(MEASURED)
                escapes = escapes or any(line is None or counted or (path, line) in taken for line in dynamic)
                follow = [target for target, line in edges if line is None or counted or (path, line) in taken]
                follow += modules.get(path, {}).get("imports", ())
                for target in [*packages, *follow]:
                    if target not in seen:
                        seen.add(target)
                        pending.append(target)
            reads = set().union(*(modules[path]["files"] for path in seen if path in modules))
            escapes = escapes or any(modules[path]["escapes"] for path in seen if path in modules)
            reached[key] = reads, escapes
        reads, escapes = reached[key]
        if not reads.issubset(found["files"]):
            found["files"] = sorted(reads.union(found["files"]))
        found["escapes"] = found["escapes"] or escapes


def build(coverage_file: Path, inputs: list[Path], root: Path, commit: str, environment: str) -> dict:
    executed, lines = _executed(coverage_file, root)
    units = sorted(set().union(*executed.values()))
    index = {name: number for number, name in enumerate(units)}
    tests = {
        test: {"units": sorted(index[name] for name in found), "files": [], "escapes": False}
        for test, found in executed.items()
    }
    modules = {}
    for report in inputs:
        for line in filter(str.strip, report.read_text().splitlines()):
            seen = json.loads(line)
            if "module" in seen:
                module = modules.setdefault(seen["module"], {"files": set(), "escapes": False, "imports": set()})
                module["files"].update(seen["files"])
                module["imports"].update(seen.get("imports", ()))
                module["escapes"] = module["escapes"] or seen["escapes"]
                continue
            test = tests.setdefault(seen["test"], {"units": [], "files": [], "escapes": False})
            test["files"] = sorted(set(test["files"]) | set(seen["files"]))
            test["escapes"] = test["escapes"] or seen["escapes"]
    attribute(tests, modules, root, lines)
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
