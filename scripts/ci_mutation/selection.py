import ast
import json
import os
import sys
from pathlib import Path

from mutmut.utils.format_utils import get_mutant_name

from scripts.ci_mutation.report import mutation_lines


def selected_mutants(filename: str, source: str, changed: set[int]) -> tuple[str, list[str]]:
    from libcst.metadata import MetadataWrapper, WhitespaceInclusivePositionProvider
    from mutmut.mutation.file_mutation import combine_mutations_to_source, create_mutations

    module, mutations, _, _ = create_mutations(filename, source)
    positions = MetadataWrapper(module, unsafe_skip_copy=True).resolve(WhitespaceInclusivePositionProvider)
    selected = []
    for mutation in mutations:
        position = positions[mutation.original_node]
        if not any(position.start.line <= line <= position.end.line for line in changed):
            continue
        lines = mutation_lines(
            module.code_for_node(mutation.original_node),
            module.code_for_node(mutation.mutated_node),
            position.start.line,
        )
        if changed.intersection(lines):
            selected.append(mutation)
    code, names = combine_mutations_to_source(module, selected)
    return code, list(names)


def keep_selected_tests(stats: dict[str, set[str]], tests_by_prefix: dict[str, set[str]]) -> set[str]:
    kept = set()
    for function, tests in stats.items():
        allowed = tests_by_prefix.get(function.rpartition(".")[0] + ".", set())
        stats[function] = {test for test in tests if test.partition("::")[0] in allowed}
        kept |= stats[function]
    return kept


def run_selected(selection: Path) -> None:
    from mutmut import __main__ as runner

    changes = json.loads(selection.read_text())
    tests_by_prefix = {get_mutant_name(Path(path), ""): set(change["tests"]) for path, change in changes.items()}
    related = set()

    def write_selected(*, out, source, filename):
        code, names = selected_mutants(str(filename), source, set(changes[str(filename)]["lines"]))
        bootstrap = (
            "import os as _mutmut_os\n"
            "from pathlib import Path as _mutmut_Path\n"
            f"_mutmut_root = _mutmut_Path(__file__).resolve().parents[{len(Path(filename).parts) - 1}]\n"
            "_mutmut_cwd = _mutmut_os.getcwd()\n"
            "try:\n"
            "    _mutmut_os.chdir(_mutmut_root)\n"
            "    from mutmut.configuration import Config as _mutmut_Config\n"
            "    _mutmut_config = _mutmut_Config.get()\n"
            "    if _mutmut_root.name == 'mutants':\n"
            "        _mutmut_config.source_paths = [(_mutmut_root / path).resolve() for path in _mutmut_config.source_paths]\n"
            "finally:\n"
            "    _mutmut_os.chdir(_mutmut_cwd)\n"
        )
        statements = ast.parse(code).body
        index = 0
        for statement in statements:
            if isinstance(statement, ast.ImportFrom) and statement.module == "__future__":
                index = statement.end_lineno
            elif (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                index = statement.end_lineno
            else:
                break
        source_lines = code.splitlines(keepends=True)
        out.write("".join(source_lines[:index]) + bootstrap + "".join(source_lines[index:]))
        return names

    collect_stats = runner.collect_or_load_stats

    def collect_selected_stats(test_runner):
        for path in changes:
            data = runner.SourceFileMutationData(path=Path(path))
            data.load()
            if data.exit_code_by_key:
                break
        else:
            raise SystemExit(0)
        # mutmut resolves source paths against the working directory on every hit; tests may change it.
        config = runner.Config.get()
        relative = config.source_paths
        config.source_paths = [(Path("mutants") / path).resolve() for path in relative]
        try:
            result = collect_stats(test_runner)
        finally:
            config.source_paths = relative
        related.update(keep_selected_tests(runner.mutmut.tests_by_mangled_function_name, tests_by_prefix))
        return result

    run_tests = runner.PytestRunner.run_tests

    def run_related_tests(self, *, mutant_name, tests):
        if mutant_name is None and not tests and related:
            tests = sorted(related, key=lambda test: runner.mutmut.duration_by_test[test])
        return run_tests(self, mutant_name=mutant_name, tests=tests)

    runner.collect_or_load_stats = collect_selected_stats
    # The clean and forced fail controls pass no tests and would otherwise rerun every selected module.
    runner.PytestRunner.run_tests = run_related_tests
    # mutmut 3.6.0 writes one copy of a whole function per selected mutant.
    runner.write_all_mutants_to_file = write_selected
    for name in ("scripts.ci_mutation", "scripts"):
        sys.modules.pop(name)
    runner.cli(["run", "--max-children", str(os.cpu_count())])


if __name__ == "__main__":
    run_selected(Path(sys.argv[1]))
