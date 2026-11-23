"""Gate the private names test modules import from other test modules.

The Semgrep rule .semgrep/test-private-imports.yml finds each such import, its finding spanning the
imported name. .semgrep/test-private-imports-allowlist.json lists the offenders dev held when the rule
landed, one tests/<file>.py::<name> entry each, and the Semgrep workflow refuses an entry its base
list lacks. check refuses an offender new against the baseline commit that the list does not name, and
a listed entry its file no longer imports; write rewrites the list from a scan of every test module.
The protected grader copy of this file runs on its own, so it imports only the standard library.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

RULE = Path(".semgrep/test-private-imports.yml")
ALLOWLIST = Path(".semgrep/test-private-imports-allowlist.json")
TESTS = Path("tests")
FLAGS = ("--metrics", "off", "--json", "--strict", "--timeout", "30", "--x-ignore-semgrepignore-files")


def configs(grader: Path | None) -> list[Path]:
    """Return the head rule, then the grader's copy of it when the grader holds one."""
    found = [RULE]
    if grader is not None and (grader / RULE).is_file():
        found.append(grader / RULE)
    return found


def scan(rules: Sequence[Path], targets: Sequence[Path], baseline: str | None = None) -> dict[str, Any]:
    """Return Semgrep's JSON report of the rules over the targets, only new findings when a baseline is given."""
    command = ["semgrep", "scan", *FLAGS]
    for rule in rules:
        command += ["--config", str(rule)]
    if baseline is not None:
        command += ["--baseline-commit", baseline]
    done = subprocess.run([*command, *map(str, targets)], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"semgrep exited {done.returncode}: {done.stderr.strip()}")
    return json.loads(done.stdout)


def offenders(report: dict[str, Any], root: Path) -> set[str]:
    """Return a file::name key for each private name the report's findings span, read at their byte offsets."""
    keys: set[str] = set()
    for result in report["results"]:
        text = (root / result["path"]).read_bytes()
        name = text[result["start"]["offset"] : result["end"]["offset"]].decode()
        keys.add(f"{result['path']}::{name}")
    return keys


def changed(baseline: str, root: Path) -> set[Path]:
    """Return the files that differ between the baseline commit and HEAD, deleted ones included."""
    done = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", baseline, "HEAD"], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise SystemExit(f"git diff exited {done.returncode}: {done.stderr.strip()}")
    return {Path(line) for line in done.stdout.splitlines() if line}


def grade(new: set[str], current: set[str], allowed: set[str], touched: set[str]) -> list[str]:
    """Return an error for each new offender the list does not name and each touched entry no longer found."""
    errors: list[str] = []
    for key in sorted(new - allowed):
        path, name = key.split("::", 1)
        errors.append(
            f"{path} imports the private name {name} from another test module; "
            "make the helper public in a shared test module instead"
        )
    for key in sorted(touched - current):
        errors.append(
            f"{ALLOWLIST} lists {key}, which its file no longer imports; run python -m scripts.private_test_imports write"
        )
    return errors


def check(baseline: str, grader: Path | None, root: Path) -> int:
    """Grade the change against the list: print each error and return 1 on any.

    Only findings new against the baseline commit count, and only entries in files the change touches are
    rescanned, so offenders and stale entries that reached the base by another route never fail this change.
    """
    rules = configs(grader)
    allowed = set(json.loads((root / ALLOWLIST).read_text()))
    new = offenders(scan(rules, [TESTS], baseline), root)
    diff = changed(baseline, root)
    touched = {key for key in allowed if Path(key.split("::", 1)[0]) in diff}
    files = sorted(path for path in {Path(key.split("::", 1)[0]) for key in touched} if (root / path).is_file())
    current = offenders(scan(rules, files), root) if files else set[str]()
    errors = grade(new, current, allowed, touched)
    for error in errors:
        print(f"::error::{error}")
    print(
        f"{len(allowed)} listed private test imports, {len(new - allowed)} new, {len(touched - current)} no longer imported"
    )
    return 1 if errors else 0


def write(root: Path) -> int:
    """Rewrite the list from a scan of every test module, sorted and indented by one space."""
    keys = sorted(offenders(scan([RULE], [TESTS]), root))
    (root / ALLOWLIST).write_text(json.dumps(keys, indent=1) + "\n")
    print(f"{ALLOWLIST} lists {len(keys)} private test imports")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check or write command from the working directory."""
    parser = argparse.ArgumentParser(prog="python -m scripts.private_test_imports")
    commands = parser.add_subparsers(dest="command", required=True)
    gate = commands.add_parser("check")
    gate.add_argument("--baseline-commit", required=True)
    gate.add_argument("--grader", type=Path)
    commands.add_parser("write")
    args = parser.parse_args(argv)
    if args.command == "check":
        return check(args.baseline_commit, args.grader, Path.cwd())
    return write(Path.cwd())


if __name__ == "__main__":
    sys.exit(main())
