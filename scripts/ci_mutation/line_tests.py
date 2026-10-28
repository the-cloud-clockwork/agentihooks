import ast
from pathlib import Path

from scripts.ci_mutation.report import mutation_lines


def covered_mutant_tests(coverage_file: Path, root: Path, tests: dict[str, set[str]]) -> dict[str, list[str]]:
    from coverage import CoverageData
    from mutmut import get_mutant_name

    if not coverage_file.is_file():
        return {}
    data = CoverageData(basename=str(coverage_file))
    data.read()
    selected = {}
    for filename in data.measured_files():
        path = Path(filename)
        if not path.is_relative_to(root) or not path.is_file():
            continue
        source = path.read_text()
        functions = {
            node.name: node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and "__mutmut_" in node.name
        }
        contexts = data.contexts_by_lineno(filename)
        for name, original in functions.items():
            if not name.endswith("__mutmut_orig"):
                continue
            function = name.removesuffix("__mutmut_orig")
            allowed = set(tests.get(get_mutant_name(path.relative_to(root), function), ()))
            seen = {
                context.rpartition("|")[0]
                for line, values in contexts.items()
                if original.lineno <= line <= original.end_lineno
                for context in values
                if "|" in context
            }
            before = ast.get_source_segment(source, original)
            for mutant, node in functions.items():
                if not mutant.startswith(function + "__mutmut_") or mutant == name:
                    continue
                after = ast.get_source_segment(source, node).replace(mutant, name, 1)
                lines = mutation_lines(before, after, original.lineno)
                if not lines or any(not contexts.get(line) or "" in contexts[line] for line in lines):
                    continue
                chosen = allowed - seen
                for line in lines:
                    chosen.update(context.rpartition("|")[0] for context in contexts[line] if "|" in context)
                chosen &= allowed
                if chosen and chosen < allowed:
                    selected[get_mutant_name(path.relative_to(root), mutant)] = sorted(chosen)
    return selected
