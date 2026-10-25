import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

# The workflow runs the base branch copy of this script from outside the checkout, so the tree is the working directory.
ROOT = Path.cwd()
RULES = ROOT / ".semgrep-guards"
FLOORS = RULES / "floors.json"
TARGET = "hooks"


def _semgrep(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["semgrep", "scan", "--metrics", "off", *args], capture_output=True, text=True, cwd=cwd or ROOT
    )


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT)


def rules() -> list[Path]:
    return sorted(RULES.glob("*.yml"))


def findings(rule_files: list[Path], target: str) -> list[dict]:
    configs = [arg for rule in rule_files for arg in ("--config", str(rule))]
    done = _semgrep("--json", "--strict", "--verbose", *configs, target)
    report = json.loads(done.stdout or "{}")
    if done.returncode or report.get("errors") or "results" not in report:
        raise SystemExit(f"semgrep could not scan {target} (exit {done.returncode}):\n{done.stderr}")
    return report["results"]


def _rule_id(finding: dict) -> str:
    return finding["check_id"].rpartition(".")[2]


def tally(rule_files: list[Path], found: list[dict]) -> dict[str, int]:
    by_rule = Counter(_rule_id(finding) for finding in found)
    return {rule.stem: by_rule[rule.stem] for rule in rule_files}


def counts(rule_files: list[Path], target: str) -> dict[str, int]:
    return tally(rule_files, findings(rule_files, target))


def _fixtures(rule: Path) -> tuple[Path, Path]:
    return rule.with_suffix(".red.py"), rule.with_suffix(".clean.py")


def fixture_test(rule_files: list[Path]) -> tuple[set[str], set[str]]:
    # The repository ignores the guard folder, so a fresh copy with an empty ignore file grades every fixture.
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / "rules"
        copy.mkdir()
        (Path(folder) / ".semgrepignore").write_text("")
        for rule in rule_files:
            for path in (rule, *_fixtures(rule)):
                shutil.copy(path, copy / path.name)
        done = _semgrep("--test", "--json", "--config", "rules", "rules", cwd=Path(folder))
    report = json.loads(done.stdout or "{}")
    checks = [check for result in report.get("results", {}).values() for check in result["checks"].values()]
    if "results" not in report or report.get("config_with_errors") or any(check["errors"] for check in checks):
        raise SystemExit(f"semgrep could not test the guard rule fixtures (exit {done.returncode}):\n{done.stderr}")
    matches = [(Path(target).name, lines) for check in checks for target, lines in check["matches"].items()]
    reported = {name for name, _ in matches}
    mismatched = {name for name, lines in matches if lines["expected_lines"] != lines["reported_lines"]}
    if done.returncode and not mismatched:
        raise SystemExit(f"semgrep could not test the guard rule fixtures (exit {done.returncode}):\n{done.stderr}")
    return reported, mismatched


def fixture_errors(rule_files: list[Path]) -> list[str]:
    tested = [rule for rule in rule_files if all(path.is_file() for path in _fixtures(rule))]
    reported, mismatched = fixture_test(tested) if tested else (set(), set())
    errors = []
    for rule in rule_files:
        red, clean = _fixtures(rule)
        if rule not in tested:
            errors.append(f"{rule.stem}: needs both {red.name} and {clean.name}")
            continue
        if f"ruleid: {rule.stem}" not in red.read_text():
            errors.append(f"{rule.stem}: {red.name} marks no line the rule must find")
        elif red.name not in reported or red.name in mismatched:
            errors.append(f"{rule.stem}: the rule does not find exactly the marked lines of {red.name}")
        if clean.name in mismatched:
            errors.append(f"{rule.stem}: the rule finds something in {clean.name}")
    return errors


def at_base(base: str, path: Path) -> str | None:
    if _git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode:
        raise SystemExit(f"base {base} is not a commit")
    shown = _git("show", f"{base}:{path.relative_to(ROOT).as_posix()}")
    return shown.stdout if shown.returncode == 0 else None


def base_floors(base: str) -> dict[str, int]:
    text = at_base(base, FLOORS)
    return {} if text is None else json.loads(text)


def changed_rules(base: str, rule_files: list[Path]) -> set[str]:
    return {rule.stem for rule in rule_files if at_base(base, rule) not in (None, rule.read_text())}


def _marks(rule: Path, text: str | None) -> int:
    return 0 if text is None else text.count(f"ruleid: {rule.stem}")


def marked_cases(base: str, rule_files: list[Path]) -> tuple[list[str], set[str]]:
    errors, grown = [], set()
    for rule in rule_files:
        red = rule.with_suffix(".red.py")
        before = _marks(rule, at_base(base, red))
        now = _marks(rule, red.read_text() if red.is_file() else None)
        if now < before:
            errors.append(f"{rule.stem}: {red.name} marks {now} cases, {before} at the base; fixtures only grow")
        elif now > before:
            grown.add(rule.stem)
    return errors, grown


def floor_errors(
    floors: dict[str, int], base: dict[str, int], rule_files: list[Path], changed: set[str], grown: set[str]
) -> list[str]:
    names = {rule.stem for rule in rule_files}
    errors = [f"{name}: floor without a rule file" for name in sorted(set(floors) - names)]
    errors += [f"{name}: guard rule removed since the base; floors only shrink" for name in sorted(set(base) - names)]
    found = findings(rule_files, TARGET)
    for name, count in tally(rule_files, found).items():
        if name not in floors:
            errors.append(f"{name}: no floor; run python -m scripts.ci_semgrep_floors record")
            continue
        was = base.get(name, floors[name])
        if floors[name] > was and not (name in changed and name in grown):
            errors.append(
                f"{name}: floor rose from {was} to {floors[name]}; "
                "a floor rises only with a rule change that adds red fixture cases"
            )
        if floors[name] < was and name in changed:
            errors.append(
                f"{name}: floor dropped from {was} to {floors[name]} with a rule change; "
                "change the rule and lower its floor in separate pull requests"
            )
        if count > floors[name]:
            sites = [f"  {f['path']}:{f['start']['line']}" for f in found if _rule_id(f) == name]
            errors.append(f"{name}: {count} findings above the floor of {floors[name]}:\n" + "\n".join(sites))
        elif count < floors[name]:
            errors.append(
                f"{name}: {count} findings under the floor of {floors[name]}; "
                "run python -m scripts.ci_semgrep_floors record"
            )
    return errors


def check(base: str) -> int:
    rule_files = rules()
    if not rule_files:
        print(f"no rule files in {RULES.relative_to(ROOT)}")
        return 1
    floors = json.loads(FLOORS.read_text()) if FLOORS.is_file() else {}
    errors = fixture_errors(rule_files)
    shrunk, grown = marked_cases(base, rule_files)
    errors += shrunk
    errors += floor_errors(floors, base_floors(base), rule_files, changed_rules(base, rule_files), grown)
    for error in errors:
        print(error)
    if not errors:
        print(f"{len(rule_files)} guard rules hold on their fixtures and at their floors: {json.dumps(floors)}")
    return 1 if errors else 0


def record() -> int:
    measured = counts(rules(), TARGET)
    FLOORS.write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n")
    print(json.dumps(measured))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grade the guard Semgrep rules against their fixtures and floors.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").add_argument("--base", required=True)
    commands.add_parser("record")
    args = parser.parse_args(argv)
    return check(args.base) if args.command == "check" else record()


if __name__ == "__main__":
    sys.exit(main())
