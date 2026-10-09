import ast
import json
import multiprocessing
import os
import re
import sys
import tempfile
from pathlib import Path
from time import process_time

from mutmut.utils.format_utils import get_mutant_name

from scripts.ci_mutation.mutant_shards import shard_names
from scripts.ci_mutation.report import mutation_lines
from scripts.ci_mutation.stats import write_part

GROUP = re.compile(r"xdist_group\(\s*(?:name\s*=\s*)?[\"']([^\"']+)[\"']")


def changed_mutations(filename: str, source: str, changed: set[int]) -> tuple[object, list]:
    from libcst.metadata import MetadataWrapper, WhitespaceInclusivePositionProvider
    from mutmut.mutation.file_mutation import create_mutations

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
    return module, selected


def selected_mutants(filename: str, source: str, changed: set[int]) -> tuple[str, list[str]]:
    from mutmut.mutation.file_mutation import combine_mutations_to_source

    module, selected = changed_mutations(filename, source, changed)
    code, names = combine_mutations_to_source(module, selected)
    return code, list(names)


def keep_selected_tests(stats: dict[str, set[str]], tests_by_prefix: dict[str, set[str]]) -> set[str]:
    kept = set()
    for function, tests in stats.items():
        allowed = tests_by_prefix.get(function.rpartition(".")[0] + ".", set())
        stats[function] = {test for test in tests if test.partition("::")[0] in allowed}
        kept |= stats[function]
    return kept


def file_seconds(root: Path, files: list[str]) -> dict[str, float]:
    durations = root / ".test_durations"
    durations = json.loads(durations.read_text()) if durations.exists() else {}
    seconds = dict.fromkeys(files, 0.01)
    for nodeid, duration in durations.items():
        if (path := nodeid.partition("::")[0]) in seconds:
            seconds[path] += duration
    return seconds


def part_files(root: Path, files: list[str], part: tuple[int, int]) -> list[str]:
    # Each part runs on its own runner, so an xdist group may span parts; stats_shards keeps it whole within one.
    index, total = part
    seconds = file_seconds(root, files)
    parts = [[] for _ in range(total)]
    for path in sorted(files, key=lambda path: (-seconds[path], path)):
        min(parts, key=lambda chosen: sum(seconds[path] for path in chosen)).append(path)
    return sorted(parts[index])


def stats_shards(root: Path, files: list[str], count: int) -> list[list[str]]:
    seconds = file_seconds(root, files)
    # Files that share an xdist group never run concurrently in CI, so they share a shard here.
    units = []
    for path in files:
        keys = {f"group {group}" for group in GROUP.findall((root / path).read_text())} or {path}
        members = [path]
        for unit in [unit for unit in units if unit[0] & keys]:
            units.remove(unit)
            keys |= unit[0]
            members = unit[1] + members
        units.append((keys, members))
    shards = [[] for _ in range(max(1, min(count, len(units))))]
    for _, members in sorted(units, key=lambda unit: (-sum(seconds[path] for path in unit[1]), unit[1])):
        min(shards, key=lambda shard: sum(seconds[path] for path in shard)).extend(members)
    # pytest drops a package conftest for files given after a file from another folder.
    return [sorted(shard) for shard in shards]


def collect_shard_stats(runner, test_runner, tests: list[str], output: Path, basetemp: str) -> None:
    os.environ["MUTANT_UNDER_TEST"] = "stats"
    os.environ["PY_IGNORE_IMPORTMISMATCH"] = "1"
    test_runner._pytest_add_cli_args = [*test_runner._pytest_add_cli_args, f"--basetemp={basetemp}"]
    start = process_time()
    status = test_runner.run_stats(tests=tests)
    tests_by_function = {name: sorted(names) for name, names in runner.mutmut.tests_by_mangled_function_name.items()}
    output.write_text(
        json.dumps(
            {
                "status": status,
                "cpu": process_time() - start,
                "tests": tests_by_function,
                "durations": runner.mutmut.duration_by_test,
            }
        )
    )


def run_stats_buckets(runner, test_runner, shards: list[list[str]], work: Path) -> list[dict]:
    context = multiprocessing.get_context("fork")
    processes = []
    for index, tests in enumerate(shards):
        output = work / f"stats-{index}.json"
        basetemp = tempfile.mkdtemp(prefix="mutation-stats-")
        process = context.Process(target=collect_shard_stats, args=(runner, test_runner, tests, output, basetemp))
        process.start()
        processes.append((process, output))
    results = []
    for process, output in processes:
        process.join()
        results.append(json.loads(output.read_text()) if process.exitcode == 0 else {"status": process.exitcode})
    if failed := [result["status"] for result in results if result["status"] != 0]:
        print(f"failed to collect stats. runner returned {failed}", flush=True)
        raise SystemExit(1)
    return results


def apply_stats(runner, results: list[dict]) -> None:
    for result in results:
        for function, tests in result["tests"].items():
            runner.mutmut.tests_by_mangled_function_name[function].update(tests)
        runner.mutmut.duration_by_test.update(result["durations"])
    if not any(runner.mutmut.tests_by_mangled_function_name.values()):
        print("failed to collect stats: no selected test reaches a mutated function", flush=True)
        raise SystemExit(1)
    runner.mutmut.stats_time = sum(result["cpu"] for result in results)
    runner.save_stats()


def collect_parallel_stats(runner, test_runner, shards: list[list[str]], work: Path) -> None:
    apply_stats(runner, run_stats_buckets(runner, test_runner, shards, work))


def worker_count(requested: int) -> int:
    return requested if os.environ.get("CI") else min(requested, 2)


def shard_collector(collect, shard: tuple[int, int]):
    def collect_shard_mutants(*, mutant_names):
        mutants, by_path = collect(mutant_names=mutant_names)
        owned = shard_names([key for _, key, _ in mutants], shard)
        return [mutant for mutant in mutants if mutant[1] in owned], by_path

    return collect_shard_mutants


def load_or_collect_stats(runner, test_runner, paths: list[str], mode: str, stats: tuple[str, ...]) -> None:
    if mode not in ("", "collect", "reuse"):
        raise ValueError(f"unknown mutation stats mode {mode!r}")
    for path in paths:
        data = runner.SourceFileMutationData(path=Path(path))
        data.load()
        if data.exit_code_by_key:
            break
    else:
        if mode == "collect":
            write_part(Path(stats[0]), stats[1], (int(stats[2]), int(stats[3])), [])
        raise SystemExit(0)
    if mode == "reuse":
        apply_stats(runner, json.loads(Path(stats[0]).read_text()))
        return
    # mutmut resolves source paths against the working directory on every hit; tests may change it.
    config = runner.Config.get()
    relative = config.source_paths
    config.source_paths = [(Path("mutants") / path).resolve() for path in relative]
    workers = worker_count(len(os.sched_getaffinity(0)))
    try:
        if mode == "collect":
            output, key, index, total = Path(stats[0]), stats[1], int(stats[2]), int(stats[3])
            files = part_files(Path.cwd(), config.pytest_add_cli_args_test_selection, (index, total))
            buckets = stats_shards(Path.cwd(), files, workers) if files else []
            write_part(output, key, (index, total), run_stats_buckets(runner, test_runner, buckets, Path.cwd()))
            raise SystemExit(0)
        shards = stats_shards(Path.cwd(), config.pytest_add_cli_args_test_selection, workers)
        collect_parallel_stats(runner, test_runner, shards, Path.cwd())
    finally:
        config.source_paths = relative


def run_selected(selection: Path, shard: tuple[int, int], mode: str = "", *stats: str) -> None:
    from mutmut import __main__ as runner

    if not os.environ.get("CI"):
        print("Local mutation worker cap: 2 (mutation and stats)")
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
            "    from mutmut.configuration import Config as _mutmut_Config\n"
            "    try:\n"
            "        _mutmut_os.chdir(_mutmut_root)\n"
            "        _mutmut_config = _mutmut_Config.get()\n"
            # A test that copies the mutated tree elsewhere imports it from a folder without a mutmut config.
            "    except FileNotFoundError:\n"
            f"        _mutmut_root = _mutmut_Path({str((Path.cwd() / 'mutants').resolve())!r})\n"
            "        _mutmut_os.chdir(_mutmut_root)\n"
            "        _mutmut_config = _mutmut_Config.get()\n"
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

    def collect_selected_stats(test_runner):
        load_or_collect_stats(runner, test_runner, list(changes), mode, stats)
        related.update(keep_selected_tests(runner.mutmut.tests_by_mangled_function_name, tests_by_prefix))

    run_tests = runner.PytestRunner.run_tests

    def run_related_tests(self, *, mutant_name, tests):
        if mutant_name is None and not tests and related:
            # The stats shards already passed every selected test, which is all mutmut's clean run repeats.
            if not os.environ.get("MUTANT_UNDER_TEST"):
                return 0
            tests = sorted(related, key=lambda test: runner.mutmut.duration_by_test[test])
            try:
                return run_tests(self, mutant_name=None, tests=tests)
            except runner.BadTestExecutionCommandsException:
                # The stats shards proved these node ids collect, so a usage error here is the forced failure at import.
                return 1
        return run_tests(self, mutant_name=mutant_name, tests=tests)

    runner.collect_or_load_stats = collect_selected_stats
    # Every shard generates the same mutants, so names and clearance keys match an unsharded run; each runs its share.
    runner.collect_source_file_mutation_data = shard_collector(runner.collect_source_file_mutation_data, shard)
    # The forced fail control passes no tests and would otherwise rerun every selected module.
    runner.PytestRunner.run_tests = run_related_tests
    # mutmut 3.6.0 writes one copy of a whole function per selected mutant.
    runner.write_all_mutants_to_file = write_selected
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        sys.modules.pop(name)
    runner.cli(["run", "--max-children", str(worker_count(os.cpu_count()))])


if __name__ == "__main__":
    run_selected(Path(sys.argv[1]), (int(sys.argv[2]), int(sys.argv[3])), *sys.argv[4:])
