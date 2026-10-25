import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path

from scripts.size_limits import _RUFF, RUFF_VERSION, GradeError, _ruff_version, files

RULES = ("BLE001", "TRY203", "TRY400")
SCOPE = ("hooks/", "scripts/gates/")
ALLOWLIST = Path("tests/SWALLOWED_EXCEPTIONS_ALLOWLIST.json")


def _graded(tree: Path) -> list[Path]:
    graded = [path for path in files(tree) if path.relative_to(tree).as_posix().startswith(SCOPE)]
    if not graded:
        raise GradeError(f"no tracked Python files under {' or '.join(SCOPE)} in {tree}")
    return graded


def measure(tree: Path) -> dict[str, dict[str, int]]:
    tree = tree.resolve()
    flags = ("--isolated", "--ignore-noqa", "--exit-zero")
    command = [*_RUFF, "check", *flags, "--output-format", "json", "--select", ",".join(RULES)]
    result = subprocess.run(command + [str(path) for path in _graded(tree)], capture_output=True, text=True)
    if result.returncode != 0:
        raise GradeError(f"ruff exited {result.returncode}: {result.stderr.strip()}")
    counts = Counter()
    for hit in json.loads(result.stdout):
        if hit["code"] not in RULES:
            raise GradeError(f"cannot grade {hit['filename']}: {hit['code']} {hit['message']}")
        counts[Path(hit["filename"]).relative_to(tree).as_posix(), hit["code"]] += 1
    measured = {}
    for (unit, rule), count in counts.items():
        measured.setdefault(unit, {})[rule] = count
    return measured


def load(tree: Path) -> dict[str, dict[str, int]] | None:
    path = tree / ALLOWLIST
    return json.loads(path.read_text()) if path.is_file() else None


def write(tree: Path) -> None:
    (tree / ALLOWLIST).write_text(json.dumps(measure(tree), indent=1, sort_keys=True) + "\n")


def grade(base: dict, head: dict, recorded: dict | None) -> list[str]:
    errors = []
    for unit, rules in head.items():
        for rule, count in rules.items():
            allowed = base.get(unit, {}).get(rule, 0)
            if count > allowed:
                errors.append(f"{unit} has {count} {rule} hits; the base allowlist allows {allowed}")
    if recorded != head:
        errors.append(
            f"{ALLOWLIST} differs from the head's measurement; run python -m scripts.swallowed_exceptions --write"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path)
    parser.add_argument("--head", type=Path, default=Path.cwd())
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not (args.write or args.base):
        parser.error("grading needs --base")
    if (version := _ruff_version()) != RUFF_VERSION:
        print(f"::error::The swallowed exception gate needs ruff {RUFF_VERSION}, found {version or 'none'}.")
        return 1
    try:
        if args.write:
            write(args.head)
            return 0
        head = measure(args.head)
        recorded = load(args.head)
        base = load(args.base)
        if base is None:
            raise GradeError(f"the base has no {ALLOWLIST}, so the head cannot be graded")
    except (GradeError, ValueError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    errors = grade(base, head, recorded)
    print(f"{len(head)} files swallow exceptions, {len(base)} on the base allowlist, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
