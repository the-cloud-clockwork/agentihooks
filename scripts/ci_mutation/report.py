import ast
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path


def parse_results(text: str) -> list[tuple[str, str]]:
    results = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*(\S+__mutmut_\d+): (.+)", line)
        if match is None:
            raise ValueError(f"Unrecognised mutation result: {line}")
        results.append((match[1], match[2]))
    return results


def mutation_lines(original: str, mutated: str, start: int) -> set[int]:
    before = original.splitlines()
    after = mutated.splitlines()
    lines = set()
    for tag, first, last, _, _ in SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if tag != "equal":
            lines.update(range(start + first, start + max(first + 1, last)))
    return lines


def function_start(source: str, name: str, class_name: str | None) -> int:
    nodes = ast.parse(source).body
    if class_name is not None:
        nodes = next(node.body for node in nodes if isinstance(node, ast.ClassDef) and node.name == class_name)
    function = next(
        node for node in nodes if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    return min([function.lineno] + [node.lineno for node in function.decorator_list])


def collect_results(path: Path) -> list[dict]:
    import libcst as cst
    from mutmut.__main__ import (
        orig_function_and_class_names_from_key,
        read_mutant_function,
        read_mutants_module,
        read_original_function,
    )

    text = subprocess.check_output([sys.executable, "-m", "mutmut", "results", "--all", "true"], text=True)
    results = parse_results(text)
    module = read_mutants_module(path)
    source = path.read_text()
    rows = []
    for key, status in results:
        row = {"name": key, "status": status, "lines": [], "fingerprint": ""}
        if status != "killed":
            name, class_name = orig_function_and_class_names_from_key(key)
            original = cst.Module([read_original_function(module, key)]).code.strip()
            mutated = cst.Module([read_mutant_function(module, key)]).code.strip()
            start = function_start(source, name, class_name)
            row["lines"] = sorted(mutation_lines(original, mutated, start))
            row["fingerprint"] = hashlib.sha256((original + "\0" + mutated).encode()).hexdigest()
        rows.append(row)
    return rows


def evaluate(path: str, rows: list[dict], changed: set[int], cleared: dict) -> dict:
    report = {
        "path": path,
        "counts": dict(Counter(row["status"] for row in rows)),
        "failures": [],
        "untouched_survivors": [],
        "cleared": [],
    }
    for row in rows:
        status = row["status"]
        if status == "killed":
            continue
        if status not in {"survived", "no tests"}:
            report["failures"].append(row)
            continue
        if not changed.intersection(row["lines"]):
            report["untouched_survivors"].append(row)
            continue
        key = f"{path}:{row['name']}:{row['fingerprint']}"
        if key in cleared:
            entry = cleared[key]
            if not entry.get("reader") or not entry.get("reason"):
                raise ValueError("Mutation clearance requires reader and reason")
            report["cleared"].append({**row, **entry})
        else:
            report["failures"].append(row)
    return report


if __name__ == "__main__":
    Path(sys.argv[2]).write_text(json.dumps(collect_results(Path(sys.argv[1])), indent=2) + "\n")
