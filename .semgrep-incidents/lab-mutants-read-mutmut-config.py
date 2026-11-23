from collections.abc import Sequence

from mutmut.configuration import Config

from scripts.ci_mutation.selection import changed_mutations, created_mutations, selected_mutants


def expanded_with_the_working_directory_config(path: str, source: str) -> int:
    # ruleid: lab-mutants-read-mutmut-config
    _, graded, _ = created_mutations(path, source)
    return len(graded)


def changed_with_the_working_directory_config(path: str, source: str, lines: set[int]) -> int:
    # ruleid: lab-mutants-read-mutmut-config
    _, mutations = changed_mutations(path, source, lines)
    return len(mutations)


def trampolined_with_the_working_directory_config(path: str, source: str, lines: set[int]) -> list[str]:
    # ruleid: lab-mutants-read-mutmut-config
    _, names = selected_mutants(path, source, lines)
    return names


def patterns_of_the_working_directory() -> list[str]:
    # ruleid: lab-mutants-read-mutmut-config
    return Config.get().do_not_mutate_patterns


def expanded_with_given_patterns(path: str, source: str) -> int:
    # ok: lab-mutants-read-mutmut-config
    _, graded, _ = created_mutations(path, source, patterns=())
    return len(graded)


def changed_with_given_patterns(path: str, source: str, lines: set[int], patterns: Sequence[str]) -> int:
    # ok: lab-mutants-read-mutmut-config
    _, mutations = changed_mutations(path, source, lines, patterns)
    return len(mutations)


def trampolined_with_given_patterns(path: str, source: str, lines: set[int]) -> list[str]:
    # ok: lab-mutants-read-mutmut-config
    _, names = selected_mutants(path, source, lines, patterns=())
    return names
