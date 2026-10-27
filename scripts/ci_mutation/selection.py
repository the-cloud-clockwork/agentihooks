import ast
import json
import multiprocessing
import os
import re
import sys
import tempfile
import traceback
from collections import Counter
from pathlib import Path
from time import process_time
from types import ModuleType, SimpleNamespace

import pytest
from mutmut.utils.format_utils import get_mutant_name

from scripts.ci_cpu_quota import quota_cpus
from scripts.ci_cpu_quota import worker_count as quota_workers
from scripts.ci_mutation.killers import KillMemory
from scripts.ci_mutation.mutant_shards import shard_names, shard_tests
from scripts.ci_mutation.report import mutation_lines
from scripts.ci_mutation.scope import selects, test_file
from scripts.ci_mutation.stats import write_part

GROUP = re.compile(r"xdist_group\(\s*(?:name\s*=\s*)?[\"']([^\"']+)[\"']")
# This group only loads fakeredis once per xdist worker; it guards no shared resource.
IMPORT_GROUPS = {"fakeredis"}


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


def selected_mutant_names(filename: str, source: str, changed: set[int]) -> list[str]:
    import libcst as cst
    from mutmut.mutation.file_mutation import group_by_top_level_node
    from mutmut.mutation.trampoline_templates import mangle_function_name

    module, selected = changed_mutations(filename, source, changed)
    grouped = group_by_top_level_node(selected)
    names = []
    for statement in module.body:
        if isinstance(statement, cst.FunctionDef):
            functions = [(statement, None)]
        elif isinstance(statement, cst.ClassDef) and isinstance(statement.body, cst.IndentedBlock):
            functions = [
                (method, statement.name.value) for method in statement.body.body if isinstance(method, cst.FunctionDef)
            ]
        else:
            continue
        for function, class_name in functions:
            name = mangle_function_name(name=function.name.value, class_name=class_name)
            names.extend(f"{name}__mutmut_{index + 1}" for index in range(len(grouped.get(function, ()))))
    return names


def keep_selected_tests(stats: dict[str, set[str]], tests_by_prefix: dict[str, set[str]]) -> set[str]:
    kept = set()
    for function, tests in stats.items():
        allowed = tests_by_prefix.get(function.rpartition(".")[0] + ".", set())
        stats[function] = {test for test in tests if any(selects(entry, test) for entry in allowed)}
        kept |= stats[function]
    return kept


def unit_records(root: Path) -> list[dict[str, float]]:
    path = root / ".test_durations"
    return [json.loads(path.read_text())] if path.exists() else []


def traced_records(root: Path) -> list[dict[str, float]]:
    folder = Path(os.environ.get("MUTATION_STATS_CACHE", root / ".mutation-stats-cache"))
    records = [json.loads(path.read_text()) for path in folder.glob("*.json")]
    return [record["durations"] for record in records if record["status"] == 0]


def recorded_seconds(records: list[dict[str, float]]) -> dict[str, float]:
    durations = {}
    for record in records:
        for nodeid, seconds in record.items():
            nodeid = re.sub(r"@[^\[\]]*$", "", nodeid)
            durations[nodeid] = max(durations.get(nodeid, 0), seconds)
    return durations


def file_seconds(root: Path, files: list[str], *, split: bool = False) -> dict[str, float]:
    durations = recorded_seconds([*unit_records(root), *traced_records(root)])
    return entry_seconds(durations, files, split=split)


def entry_seconds(durations: dict[str, float], files: list[str], *, split: bool = False) -> dict[str, float]:
    seconds = {} if split else dict.fromkeys(files, 0.01)
    by_file = {}
    for entry in files:
        by_file.setdefault(test_file(entry), []).append(entry)
    matched = set()
    for nodeid, duration in durations.items():
        for entry in by_file.get(test_file(nodeid), ()):
            if selects(entry, nodeid):
                key = nodeid if split else entry
                seconds[key] = seconds.get(key, 0.01) + duration
                matched.add(entry)
    for entry in files:
        if entry not in matched:
            seconds.setdefault(entry, 0.01)
    return seconds


def collect_test_ids(test_runner, files: list[str]) -> list[str]:
    handle, name = tempfile.mkstemp()
    os.close(handle)
    output = Path(name)

    def collect():
        import pytest

        def finish(session):
            output.write_text(json.dumps(sorted({item.nodeid for item in session.items})))

        os.environ["MUTANT_UNDER_TEST"] = "stats"
        try:
            status = pytest.main(
                [*test_runner._pytest_add_cli_args, "--collect-only", "-q", "-m", "not wall_clock", *files],
                plugins=[SimpleNamespace(pytest_collection_finish=finish)],
            )
            code = int(status not in (0, 5))
        except BaseException:
            traceback.print_exc()
            code = 1
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)

    try:
        process = multiprocessing.get_context("fork").Process(target=collect)
        process.start()
        process.join()
        if process.exitcode != 0:
            raise SystemExit(process.exitcode)
        return json.loads(output.read_text())
    finally:
        output.unlink()


def part_files(root: Path, files: list[str], part: tuple[int, int]) -> list[str]:
    # Every part must split identically, and each restores its own traced cache.
    return split_parts(files, entry_seconds(recorded_seconds(unit_records(root)), files), part)


def split_parts(files: list[str], seconds: dict[str, float], part: tuple[int, int]) -> list[str]:
    # Each part runs on its own runner, so an xdist group may span parts; stats_shards keeps it whole within one.
    index, total = part
    return partition(files, seconds, total)[index]


def partition(files: list[str], seconds: dict[str, float], total: int) -> list[list[str]]:
    parts = [[] for _ in range(total)]
    loads = Counter()
    for path in sorted(files, key=lambda path: (-seconds[path], path)):
        chosen = min(range(total), key=loads.__getitem__)
        parts[chosen].append(path)
        loads[chosen] += seconds[path]
    return [sorted(part) for part in parts]


def stats_shards(root: Path, files: list[str], count: int, seconds: dict[str, float] | None = None) -> list[list[str]]:
    if seconds is None:
        seconds = file_seconds(root, files)
    # Files that share an xdist group never run concurrently in CI, so they share a shard here.
    units, owners, groups = {}, {}, {}
    for number, path in enumerate(files):
        name = test_file(path)
        if name not in groups:
            groups[name] = set(GROUP.findall((root / name).read_text())) - IMPORT_GROUPS
        keys = {f"group {group}" for group in groups[name]} or {name}
        members = [path]
        for unit in sorted({owners[key] for key in keys if key in owners}):
            joined, before = units.pop(unit)
            keys |= joined
            members = before + members
        units[number] = (keys, members)
        owners.update(dict.fromkeys(keys, number))
    shards = [[] for _ in range(max(1, min(count, len(units))))]
    loads = [0.0] * len(shards)
    for _, members in sorted(units.values(), key=lambda unit: (-sum(seconds[path] for path in unit[1]), unit[1])):
        chosen = min(range(len(shards)), key=loads.__getitem__)
        shards[chosen].extend(members)
        loads[chosen] += sum(seconds[path] for path in members)
    # pytest drops a package conftest for files given after a file from another folder.
    return [sorted(shard) for shard in shards]


def loaded_files(bases: list[Path], modules: dict[str, ModuleType] = sys.modules) -> list[str]:
    found = set()
    for module in list(modules.values()):
        if origin := getattr(module, "__file__", None):
            path = Path(origin).resolve()
            base = next((base for base in bases if path.is_relative_to(base)), None)
            if base is not None:
                found.add(path.relative_to(base).as_posix())
    return sorted(found)


def collect_shard_stats(runner, test_runner, tests: list[str], output: Path, basetemp: str) -> None:
    os.environ["MUTANT_UNDER_TEST"] = "stats"
    os.environ["PY_IGNORE_IMPORTMISMATCH"] = "1"
    # Stats tracing slows every test, so a wall clock bound only holds in the unit shards.
    test_runner._pytest_add_cli_args = [
        *test_runner._pytest_add_cli_args,
        "-p",
        "pytest_rerunfailures",
        "--reruns",
        "1",
        "-m",
        "not wall_clock",
        f"--basetemp={basetemp}",
    ]
    measured = bool(os.environ.get("MUTATION_STATS_CACHE"))
    coverage_file = output.with_suffix(".coverage")
    if measured:
        # An exported COVERAGE_FILE reaches every subprocess a test starts, whose coverage runs then write here.
        os.environ.pop("COVERAGE_FILE", None)
        config = output.with_suffix(".coveragerc")
        config.write_text(f"[run]\ndata_file = {coverage_file.resolve()}\n")
        test_runner._pytest_add_cli_args.extend(
            ["-p", "pytest_cov.plugin", "--cov=.", "--cov-context=test", "--cov-report=", f"--cov-config={config}"]
        )
    work = Path.cwd().resolve()
    bases = [
        work / "mutants",
        work,
        *(Path(root).resolve() for root in [os.environ.get("MUTATION_STATS_ROOT")] if root),
    ]
    start = process_time()
    status = test_runner.run_stats(tests=tests)
    # A bucket of only wall clock tests deselects everything, and pytest reports that as no tests collected.
    status = 0 if status == pytest.ExitCode.NO_TESTS_COLLECTED else status
    dependencies = {}
    if measured:
        from subprocess import check_output

        from scripts.ci_mutation.stats_dependencies import measured_dependencies

        root = Path(os.environ["MUTATION_STATS_ROOT"])
        tracked = set(check_output(["git", "ls-files", "-z"], cwd=root, text=True).split("\0"))
        dependencies["dependencies"] = measured_dependencies(
            coverage_file, Path.cwd() / "mutants", root, list(runner.mutmut.duration_by_test), tracked
        )
    tests_by_function = {name: sorted(names) for name, names in runner.mutmut.tests_by_mangled_function_name.items()}
    output.write_text(
        json.dumps(
            {
                **dependencies,
                "status": status,
                "cpu": process_time() - start,
                "tests": tests_by_function,
                "durations": runner.mutmut.duration_by_test,
                "files": tests,
                "modules": loaded_files(bases),
            }
        )
    )


def collect_and_exit(runner, test_runner, tests: list[str], output: Path, basetemp: str) -> None:
    try:
        collect_shard_stats(runner, test_runner, tests, output, basetemp)
    except BaseException:
        traceback.print_exc()
        code = 1
    else:
        code = 0
    sys.stdout.flush()
    sys.stderr.flush()
    # A normal child exit joins every thread and child process a test left running, which can wait forever.
    os._exit(code)


def run_stats_buckets(runner, test_runner, shards: list[list[str]], work: Path) -> list[dict]:
    context = multiprocessing.get_context("fork")
    processes = []
    for index, tests in enumerate(shards):
        output = work / f"stats-{index}.json"
        basetemp = tempfile.mkdtemp(prefix="mutation-stats-")
        process = context.Process(target=collect_and_exit, args=(runner, test_runner, tests, output, basetemp))
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
    return quota_workers(requested, quota_cpus()) if os.environ.get("CI") else min(requested, 2)


def shard_collector(collect, shard: tuple[int, int]):
    def collect_shard_mutants(*, mutant_names):
        mutants, by_path = collect(mutant_names=mutant_names)
        owned = shard_names([key for _, key, _ in mutants], shard)
        return [mutant for mutant in mutants if mutant[1] in owned], by_path

    return collect_shard_mutants


def collect_file_stats(runner, test_runner, files: list[str], workers: int, work: Path) -> list[dict]:
    def collect(chosen):
        buckets = stats_shards(work, chosen, workers) if chosen else []
        return run_stats_buckets(runner, test_runner, buckets, work)

    if folder := os.environ.get("MUTATION_STATS_CACHE"):
        from scripts.ci_mutation.stats_cache import collect_cached_stats

        changes = json.loads((work / "changed-lines.json").read_text())
        scope = {path: sorted(change["lines"]) for path, change in changes.items()}
        return collect_cached_stats(
            Path(os.environ["MUTATION_STATS_ROOT"]),
            Path(folder),
            scope,
            files,
            test_runner._pytest_add_cli_args,
            collect,
        )
    return collect(files)


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
            files = config.pytest_add_cli_args_test_selection
            if total > 1:
                files = collect_test_ids(test_runner, files)
            files = part_files(Path.cwd(), files, (index, total))
            write_part(output, key, (index, total), collect_file_stats(runner, test_runner, files, workers, Path.cwd()))
            raise SystemExit(0)
        shards = stats_shards(Path.cwd(), config.pytest_add_cli_args_test_selection, workers)
        collect_parallel_stats(runner, test_runner, shards, Path.cwd())
    finally:
        config.source_paths = relative


# Text, not a module: a mutated copy of a stand-in module is itself trampolined and cannot serve the trampoline.
LIGHT_MAIN = """import importlib
import sys


class MutmutProgrammaticFailException(Exception):
    pass


def mangled_name_from_mutant_name(mutant_name):
    assert "__mutmut_" in mutant_name, mutant_name
    return mutant_name.partition("__mutmut_")[0]


def record_trampoline_hit(name):
    pass


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    if getattr(sys.modules.get("mutmut.__main__"), "__getattr__", None) is __getattr__:
        del sys.modules["mutmut.__main__"]
    return getattr(importlib.import_module("mutmut.__main__"), name)
"""


LIGHT_BOOTSTRAP = (
    "import sys as _mutmut_sys\n"
    # A process the mutation run did not start would import mutmut's whole engine for three trampoline names.
    "_mutmut_entry = getattr(getattr(_mutmut_sys.modules.get('__main__'), '__spec__', None), 'name', '')\n"
    # The runner, mutated itself inside a nested gate test, still needs the real module for its stats.
    "if 'mutmut.__main__' not in _mutmut_sys.modules and _mutmut_entry != 'scripts.ci_mutation.selection':\n"
    "    import types as _mutmut_types\n"
    "    _mutmut_sys.modules['mutmut.__main__'] = _mutmut_types.ModuleType('mutmut.__main__')\n"
    f"    exec({LIGHT_MAIN!r}, _mutmut_sys.modules['mutmut.__main__'].__dict__)\n"
    "    import mutmut as _mutmut_package\n"
    "    _mutmut_package.__main__ = _mutmut_sys.modules['mutmut.__main__']\n"
)


def run_unit(
    run_tests, runner, mutant_name: str | None, tests: list[str], chosen: list[str], memory: KillMemory
) -> int:
    if tests and not chosen:
        # Each unit runs the head and one slice and together they cover every test, so a unit with none votes survived.
        return 0
    return run_tests(runner, mutant_name=mutant_name, tests=memory.prepare(mutant_name, chosen))


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
            + LIGHT_BOOTSTRAP
            + f"_mutmut_root = _mutmut_Path(__file__).resolve().parents[{len(Path(filename).parts) - 1}]\n"
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
        tests = list(tests)
        return run_unit(run_tests, self, mutant_name, tests, shard_tests(tests, mutant_name, shard[0]), memory)

    memory = KillMemory(Path.cwd() / "killers")

    runner.collect_or_load_stats = collect_selected_stats
    # Every shard generates the same mutants, so names and clearance keys match an unsharded run; each runs its share.
    runner.collect_source_file_mutation_data = shard_collector(runner.collect_source_file_mutation_data, shard)
    # The forced fail control passes no tests and would otherwise rerun every selected module.
    runner.PytestRunner.run_tests = run_related_tests
    # A function's mutants usually die on one test, so later ones run the recorded killer first.
    runner.PytestRunner.execute_pytest = memory.wrap(runner.PytestRunner.execute_pytest)
    # mutmut 3.6.0 writes one copy of a whole function per selected mutant.
    runner.write_all_mutants_to_file = write_selected
    for name in [name for name in sys.modules if name == "scripts" or name.startswith("scripts.")]:
        sys.modules.pop(name)
    runner.cli(["run", "--max-children", str(worker_count(os.cpu_count()))])


if __name__ == "__main__":
    run_selected(Path(sys.argv[1]), (int(sys.argv[2]), int(sys.argv[3])), *sys.argv[4:])
