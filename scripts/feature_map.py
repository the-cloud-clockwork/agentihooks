import argparse
import ast
import datetime
import json
import os
import re
from pathlib import Path

import yaml


def _tests(root: Path) -> dict[str, str | None]:
    marked = {}
    for path in sorted((root / "tests").rglob("*.py")):
        if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            continue
        nodes = _suite_nodes(ast.parse(path.read_text()).body, path.relative_to(root).as_posix())
        for test_id, node in nodes.items():
            marked[test_id] = None
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and ast.unparse(decorator.func) == "pytest.mark.feature":
                    marked[test_id] = ast.literal_eval(decorator.args[0])
                    if not isinstance(marked[test_id], str) or not marked[test_id].strip():
                        raise ValueError(f"{test_id}: feature marker requires an id")
    return marked


def _rules(root: Path) -> set[str]:
    rules = []
    for path in (root / ".semgrep/features").rglob("*"):
        if path.suffix in {".yaml", ".yml"} and not path.name.endswith((".test.yaml", ".test.yml")):
            data = yaml.safe_load(path.read_text())
            rules.extend(rule["id"] for rule in data["rules"])
    if len(rules) != len(set(rules)):
        raise ValueError("duplicate feature rule id")
    return set(rules)


def _live_target(root: Path, command: list[str]) -> bool:
    if command[0] in {"python", "python3"}:
        if len(command) < 2:
            return False
        if command[1] == "-m":
            if len(command) < 3 or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", command[2]):
                return False
            module = root / command[2].replace(".", "/")
            return module.with_suffix(".py").is_file() or (module / "__main__.py").is_file()
        target = root / command[1]
    elif command[0] == "bash":
        if len(command) < 2:
            return False
        target = root / command[1]
    else:
        target = root / command[0]
        if not os.access(target, os.X_OK):
            return False
    return target.resolve().is_relative_to(root.resolve()) and target.is_file()


def _suite_nodes(body: list[ast.stmt], prefix: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    if any(isinstance(node, ast.Assign) and ast.unparse(node) == "__test__ = False" for node in body):
        return {}
    result = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            result[f"{prefix}::{node.name}"] = node
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            if not any(
                isinstance(item, ast.FunctionDef) and item.name in {"__init__", "__new__"} for item in node.body
            ):
                result.update(_suite_nodes(node.body, f"{prefix}::{node.name}"))
    return result


def _behaviour_test(node_id: str, name: str, tests: dict[str, str | None]) -> None:
    if node_id not in tests:
        raise ValueError(f"{name}: missing behaviour test {node_id}")
    if tests[node_id] != name:
        raise ValueError(f"{name}: behaviour test requires matching feature marker")


def _entry(entry: dict, root: Path, rules: set[str], tests: dict[str, str | None]) -> None:
    required = ("id", "operator_words", "ordered_at", "ledger_item", "behaviour", "semgrep_rule", "behaviour_test")
    for key in required:
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise ValueError(f"feature requires {key}")
    name = entry["id"]
    try:
        datetime.date.fromisoformat(entry["ordered_at"])
    except ValueError as error:
        raise ValueError(f"{name}: invalid ordered_at") from error
    if not re.fullmatch(r"ledger:[\w-]+/(tasks|phases|comments|chat)/[\w-]+", entry["ledger_item"]):
        raise ValueError(f"{name}: invalid ledger_item")
    if entry["semgrep_rule"] not in rules:
        raise ValueError(f"{name}: missing rule {entry['semgrep_rule']}")
    _behaviour_test(entry["behaviour_test"], name, tests)
    command = entry.get("live_check")
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
        raise ValueError(f"{name}: requires live_check argument list")
    if not _live_target(root, command):
        raise ValueError(f"{name}: missing live check {command}")


def validate(root: Path, map_path: Path) -> int:
    data = json.loads(map_path.read_text())
    entries = data["features"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("features must be a nonempty list")
    rules, tests = _rules(root), _tests(root)
    ids, owned_rules, owned_tests = set(), set(), set()
    for entry in entries:
        _entry(entry, root, rules, tests)
        if entry["id"] in ids:
            raise ValueError(f"duplicate feature {entry['id']}")
        ids.add(entry["id"])
        owned_rules.add(entry["semgrep_rule"])
        owned_tests.add(entry["behaviour_test"])
    if missing := rules - owned_rules:
        raise ValueError(f"unmapped rule: {sorted(missing)}")
    if missing := {node_id for node_id, feature in tests.items() if feature is not None} - owned_tests:
        raise ValueError(f"unmapped feature test: {sorted(missing)}")
    return len(entries)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    count = validate(args.root, args.root / "feature-map.json")
    print(f"Feature map: {count} features, all rule, behaviour and live check links exist")


if __name__ == "__main__":
    main()
