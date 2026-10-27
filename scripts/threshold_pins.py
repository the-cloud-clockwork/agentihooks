import argparse
import ast
import fnmatch
import json
import subprocess
from pathlib import Path

AT_MOST = "at most"
COVERS = "covers"
PINS = (
    ("scripts/size_limits.py", "RULES", "at most"),
    ("scripts/size_limits.py", "FUNCTION_LINES", "at most"),
    ("scripts/size_limits.py", "FILE_LINES", "at most"),
    ("scripts/dead_code.py", "MIN_CONFIDENCE", "at most"),
    ("scripts/dead_code.py", "SCOPE", "covers"),
    ("scripts/swallowed_exceptions.py", "RULES", "covers"),
    ("scripts/swallowed_exceptions.py", "SCOPE", "covers"),
    ("scripts/locked_tests.py", "DISABLING", "covers"),
    ("scripts/threshold_pins.py", "PINS", "covers"),
    ("scripts/threshold_pins.py", "SHRINK_ONLY", "covers"),
    ("scripts/threshold_pins.py", "GRADED_BY", "covers"),
)
SHRINK_ONLY = (
    "tests/DEAD_CODE_ALLOWLIST.json",
    "tests/SIZE_ALLOWLIST.json",
    "tests/SWALLOWED_EXCEPTIONS_ALLOWLIST.json",
)
# Each of these rises only with its own gate's evidence, which that gate checks against the base.
GRADED_BY = (
    (".semgrep-guards/floors.json", "scripts.ci_semgrep_floors"),
    ("tests/LOCKED_MANIFEST.json", "scripts.locked_tests"),
)
REGISTERED = ("*ALLOWLIST*.json", "*floors*.json", "*FLOOR*.json", "*MANIFEST*.json", "*BASELINE*.json")


class PinError(Exception):
    pass


def constant(tree: Path, path: str, name: str):
    source = tree / path
    if not source.is_file():
        return None
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise PinError(f"{path} defines no literal {name}")


def loosened(kind: str, base, head) -> str | None:
    if kind == COVERS:
        missing = [item for item in base if item not in head]
        return f"drops {missing}" if missing else None
    if isinstance(base, dict):
        if not isinstance(head, dict):
            return f"is no longer a mapping: {head!r}"
        for key, value in base.items():
            if key not in head:
                return f"drops {key}"
            if (found := loosened(kind, value, head[key])) is not None:
                return f"{key} {found}"
        return None
    if type(head) is not type(base):
        return f"changed from {base!r} to {head!r}"
    return f"rose from {base} to {head}" if head > base else None


def pin_errors(base: Path, head: Path, pins=PINS) -> list[str]:
    errors = []
    for path, name, kind in pins:
        was = constant(base, path, name)
        if was is None:
            continue
        now = constant(head, path, name)
        if now is None:
            errors.append(f"{path} is gone, so {name} is no longer pinned")
        elif (found := loosened(kind, was, now)) is not None:
            errors.append(f"{path} {name} {found}; thresholds never loosen against the base")
    return errors


def shrunk(base, head, where: str) -> list[str]:
    if isinstance(base, dict) and isinstance(head, dict):
        errors = [f"{where}{key} is new; the base does not allow it" for key in sorted(set(head) - set(base))]
        for key in sorted(set(head) & set(base)):
            errors += shrunk(base[key], head[key], f"{where}{key} ")
        return errors
    if type(base) is int and type(head) is int:
        return [f"{where}rose from {base} to {head}"] if head > base else []
    return [] if base == head else [f"{where}changed from {base!r} to {head!r}"]


def allowlist_errors(base: Path, head: Path, allowlists=SHRINK_ONLY) -> list[str]:
    errors = []
    for path in allowlists:
        if not ((base / path).is_file() and (head / path).is_file()):
            continue
        was, now = json.loads((base / path).read_text()), json.loads((head / path).read_text())
        errors += [f"{path}: {error}; allowlists only shrink" for error in shrunk(was, now, "")]
    return errors


def tracked(tree: Path) -> list[str]:
    result = subprocess.run(["git", "-C", str(tree), "ls-files", "-z"], capture_output=True)
    if result.returncode != 0:
        raise PinError(f"git ls-files failed in {tree}: {result.stderr.decode().strip()}")
    return [name for name in result.stdout.decode().split("\0") if name]


def registry_errors(head: Path, allowlists=SHRINK_ONLY, graded=GRADED_BY) -> list[str]:
    known = set(allowlists) | {path for path, _ in graded}
    found = [
        name
        for name in tracked(head)
        if not name.startswith(("tests/fixtures/", "evidence/"))
        and any(fnmatch.fnmatch(Path(name).name, pattern) for pattern in REGISTERED)
    ]
    return [
        f"{name} is a floor or allowlist the threshold pin does not register" for name in found if name not in known
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when a threshold, floor or allowlist loosens against the base.")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        errors = pin_errors(args.base, args.head) + allowlist_errors(args.base, args.head) + registry_errors(args.head)
    except (PinError, SyntaxError, ValueError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    print(f"{len(PINS)} thresholds pinned, {len(SHRINK_ONLY)} allowlists held, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
