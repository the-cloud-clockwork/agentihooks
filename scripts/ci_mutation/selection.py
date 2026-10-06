import json
import sys
from pathlib import Path


def selected_mutants(filename: str, source: str, changed: set[int]) -> tuple[str, list[str]]:
    from libcst.metadata import MetadataWrapper, WhitespaceInclusivePositionProvider
    from mutmut.mutation.file_mutation import combine_mutations_to_source, create_mutations

    from scripts.ci_mutation.report import mutation_lines

    module, mutations, ignored_classes, ignored_functions = create_mutations(filename, source)
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
    code, names = combine_mutations_to_source(module, selected, ignored_classes, ignored_functions)
    return code, list(names)


def run_selected(selection: Path) -> None:
    from mutmut import __main__ as runner

    changes = json.loads(selection.read_text())

    def write_selected(*, out, source, filename):
        code, names = selected_mutants(str(filename), source, set(changes[str(filename)]))
        out.write(code)
        return names

    collect_stats = runner.collect_or_load_stats

    def collect_selected_stats(test_runner):
        for path in changes:
            data = runner.SourceFileMutationData(path=Path(path))
            data.load()
            if data.exit_code_by_key:
                return collect_stats(test_runner)
        raise SystemExit(0)

    runner.collect_or_load_stats = collect_selected_stats
    # mutmut 3.6.0 writes one copy of a whole function per selected mutant.
    runner.write_all_mutants_to_file = write_selected
    for name in ("scripts.ci_mutation", "scripts"):
        sys.modules.pop(name)
    runner.cli(["run", "--max-children", "1"])


if __name__ == "__main__":
    run_selected(Path(sys.argv[1]))
