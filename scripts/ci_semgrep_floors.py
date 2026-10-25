import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / ".semgrep-guards"
FLOORS = RULES / "floors.json"
TARGET = "hooks"


def _semgrep(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["semgrep", "scan", "--metrics", "off", *args], capture_output=True, text=True, cwd=ROOT)


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


def fixture_errors(rule: Path) -> list[str]:
    red, clean = rule.with_suffix(".red.py"), rule.with_suffix(".clean.py")
    if not red.is_file() or not clean.is_file():
        return [f"{rule.stem}: needs both {red.name} and {clean.name}"]
    errors = []
    if f"ruleid: {rule.stem}" not in red.read_text():
        errors.append(f"{rule.stem}: {red.name} marks no line the rule must find")
    elif _semgrep("--test", "--config", str(rule), str(red)).returncode:
        errors.append(f"{rule.stem}: the rule does not find exactly the marked lines of {red.name}")
    if counts([rule], str(clean))[rule.stem]:
        errors.append(f"{rule.stem}: the rule finds something in {clean.name}")
    return errors


def base_floors(base: str) -> dict[str, int]:
    shown = subprocess.run(
        ["git", "show", f"{base}:{FLOORS.relative_to(ROOT)}"], capture_output=True, text=True, cwd=ROOT
    )
    return json.loads(shown.stdout) if shown.returncode == 0 else {}


def floor_errors(floors: dict[str, int], base: dict[str, int], rule_files: list[Path]) -> list[str]:
    errors = [f"{name}: floor without a rule file" for name in sorted(set(floors) - {r.stem for r in rule_files})]
    found = findings(rule_files, TARGET)
    measured = tally(rule_files, found)
    for name, count in measured.items():
        if name not in floors:
            errors.append(f"{name}: no floor; run python -m scripts.ci_semgrep_floors record")
            continue
        if floors[name] > base.get(name, floors[name]):
            errors.append(f"{name}: floor rose from {base[name]} to {floors[name]}; floors only shrink")
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
    errors = [error for rule in rule_files for error in fixture_errors(rule)]
    errors += floor_errors(floors, base_floors(base), rule_files)
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
