import ast
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from scripts.ci_mutation.clearances import clearance_path


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


def mutation_diff(path: str, original: str, mutated: str, start: int) -> str:
    before = original.splitlines()
    after = mutated.splitlines()
    lines = [f"--- {path}", "+++ mutant"]
    for group in SequenceMatcher(a=before, b=after, autojunk=False).get_grouped_opcodes(1):
        lines.append(f"@@ line {start + group[0][1]} @@")
        for tag, first, last, low, high in group:
            lines += [f"{' ' if tag == 'equal' else '-'}{line}" for line in before[first:last]]
            if tag != "equal":
                lines += [f"+{line}" for line in after[low:high]]
    return "\n".join(lines)


def function_start(source: str, original: str, class_name: str | None) -> int:
    nodes = ast.parse(source).body
    if class_name is not None:
        nodes = next(node.body for node in nodes if isinstance(node, ast.ClassDef) and node.name == class_name)
    expected = ast.dump(ast.parse(original).body[0])
    function = next(node for node in nodes if ast.dump(node) == expected)
    return min([function.lineno] + [node.lineno for node in function.decorator_list])


def collect_results(path: Path) -> list[dict]:
    import libcst as cst
    from mutmut.__main__ import (
        orig_function_and_class_names_from_key,
        read_mutant_function,
        read_mutants_module,
        read_original_function,
    )
    from mutmut.utils.format_utils import get_mutant_name

    text = subprocess.check_output([sys.executable, "-m", "mutmut", "results", "--all", "true"], text=True)
    prefix = get_mutant_name(path, "")
    results = [(key, status) for key, status in parse_results(text) if key.rpartition(".")[0] + "." == prefix]
    module = read_mutants_module(path)
    source = path.read_text()
    rows = []
    for key, status in results:
        row = {"name": key, "status": status, "lines": [], "fingerprint": ""}
        if status != "killed":
            _, class_name = orig_function_and_class_names_from_key(key)
            original = cst.Module([read_original_function(module, key)]).code.strip()
            mutated = cst.Module([read_mutant_function(module, key)]).code.strip()
            start = function_start(source, original, class_name)
            row["lines"] = sorted(mutation_lines(original, mutated, start))
            row["fingerprint"] = hashlib.sha256((original + "\0" + mutated).encode()).hexdigest()
            row["diff"] = mutation_diff(path.as_posix(), original, mutated, start)
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


def survivor_text(report: dict) -> str:
    blocks = []
    for row in report["failures"]:
        key = f"{report['path']}:{row['name']}:{row['fingerprint']}"
        lines = ", ".join(map(str, row["lines"]))
        clearance = clearance_path(Path(), key).as_posix()
        blocks.append(f"{row['status']} on lines {lines}: {key}\nclearance file: {clearance}\n{row['diff']}")
    return "\n\n".join(blocks)


if __name__ == "__main__":
    rows = {path: collect_results(Path(path)) for path in sys.argv[2:]}
    Path(sys.argv[1]).write_text(json.dumps(rows, indent=2) + "\n")
