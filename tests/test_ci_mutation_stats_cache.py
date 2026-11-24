import ast
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_unchanged_test_stats_are_reused_and_only_the_changed_file_is_collected(tmp_path):
    import json

    from scripts.ci_mutation.stats_cache import collect_cached_stats
    from scripts.ci_mutation.stats_dependencies import CONTEXT

    root = tmp_path / "repo"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "scripts/sample.py").write_text("def sample():\n    return 1\n")
    files = ["tests/test_a.py", "tests/test_b.py"]
    for path in files:
        (root / path).write_text("def test_sample():\n    pass\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "scripts/sample.py", *files], cwd=root, check=True)
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {"scripts.sample.x_sample": [f"{path}::test_sample" for path in chosen]},
                "durations": {f"{path}::test_sample": 1 for path in chosen},
                "cpu": 2,
            }
        ]

    cache = tmp_path / "cache"
    changes = {"scripts/sample.py": {"lines": [2]}}
    first = collect_cached_stats(root, cache, changes, files, ["-q"], collect)
    assert calls == [files]
    marker = (cache / CONTEXT).read_text()
    assert [json.loads(record.read_text())["context"] for record in cache.glob("*.json")] == [marker, marker]
    warm = collect_cached_stats(root, cache, changes, files, ["-q"], collect)
    assert calls == [files]
    assert sum(result["cpu"] for result in warm) == sum(result["cpu"] for result in first)
    assert {test for result in warm for test in result["durations"]} == {
        "tests/test_a.py::test_sample",
        "tests/test_b.py::test_sample",
    }
    assert {test for result in first for test in result["tests"]["scripts.sample.x_sample"]} == {
        "tests/test_a.py::test_sample",
        "tests/test_b.py::test_sample",
    }
    (root / files[1]).write_text("def test_sample():\n    assert True\n")
    collect_cached_stats(root, cache, changes, files, ["-q"], collect)
    assert calls == [files, ["tests/test_b.py"]]


def test_an_edited_sibling_test_file_keeps_the_stats_of_a_test_that_never_imports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from coverage import CoverageData

    from scripts.ci_mutation.stats_cache import collect_cached_stats
    from scripts.ci_mutation.stats_dependencies import measured_dependencies

    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "scripts/sample.py").write_text("def sample():\n    return 1\n")
    files = ["tests/test_a.py", "tests/test_b.py"]
    for path in files:
        (root / path).write_text(
            "from scripts.sample import sample\n\n\ndef test_sample():\n    assert sample() == 1\n"
        )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "scripts/sample.py", *files], cwd=root, check=True)
    tracked = {"scripts/sample.py", *files}
    calls: list[list[str]] = []

    def collect(chosen: list[str]) -> list[dict[str, object]]:
        calls.append(chosen)
        nodes = [f"{path}::test_sample" for path in chosen]
        coverage_file = tmp_path / f"coverage-{len(calls)}"
        data = CoverageData(basename=str(coverage_file))
        data.set_context("")
        data.add_lines({**{str(root / path): {1, 4} for path in chosen}, str(root / "scripts/sample.py"): {1}})
        for node, path in zip(nodes, chosen, strict=True):
            data.set_context(f"{node}|run")
            data.add_lines({str(root / path): {5}, str(root / "scripts/sample.py"): {2}})
            monkeypatch.setitem(
                sys.modules, f"stats_probe_{Path(path).stem}", SimpleNamespace(__file__=str(root / path))
            )
        data.write()
        return [
            {
                "status": 0,
                "tests": {"scripts.sample.x_sample": nodes},
                "durations": dict.fromkeys(nodes, 1),
                "cpu": 2,
                "dependencies": measured_dependencies(coverage_file, root, root, nodes, tracked),
            }
        ]

    cache = tmp_path / "cache"
    scope = {"scripts/sample.py": [2]}
    collect_cached_stats(root, cache, scope, files, ["-q"], collect)
    edited = root / "tests/test_b.py"
    edited.write_text(edited.read_text() + "\n\ndef test_more():\n    pass\n")
    collect_cached_stats(root, cache, scope, files, ["-q"], collect)
    assert calls == [files, ["tests/test_b.py"]]


def test_selectors_of_one_test_file_trace_its_dependencies_once(tmp_path, monkeypatch):
    from scripts.ci_mutation import stats_cache

    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_a.py").write_text("def test_one():\n    pass\n\n\ndef test_two():\n    pass\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "tests/test_a.py"], cwd=root, check=True)
    files = ["tests/test_a.py::test_one", "tests/test_a.py::test_two"]
    traced = []
    trace = stats_cache.test_inputs
    monkeypatch.setattr(
        stats_cache, "test_inputs", lambda root, path, *rest: traced.append(path) or trace(root, path, *rest)
    )

    def collect(chosen):
        return [{"status": 0, "tests": {}, "durations": {path: 1 for path in chosen}, "cpu": 1}]

    results = stats_cache.collect_cached_stats(root, tmp_path / "cache", {}, files, ["-q"], collect)
    assert traced == ["tests/test_a.py"]
    assert {test for result in results for test in result["durations"]} == set(files)


def test_failed_collection_never_populates_reusable_stats(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    root.mkdir()
    test = root / "test_sample.py"
    test.write_text("def test_sample():\n    pass\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "test_sample.py"], cwd=root, check=True)
    cache = tmp_path / "cache"
    with pytest.raises(SystemExit, match="1"):
        collect_cached_stats(
            root,
            cache,
            {},
            ["test_sample.py"],
            [],
            lambda files: [{"status": 1, "tests": {}, "durations": {}, "cpu": 1}],
        )
    assert list(cache.glob("*.json")) == []


@pytest.mark.feature("mutation-stats-flake-ledger")
def test_collection_parts_reuse_stats_and_keep_the_new_head_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import load_or_collect_stats

    root = tmp_path / "repo"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "scripts/sample.py").write_text("def sample():\n    return 1\n")
    (root / "test_sample.py").write_text("def test_sample():\n    pass\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", "scripts/sample.py", "test_sample.py"], cwd=root, check=True)
    (root / "changed-lines.json").write_text(
        json.dumps({"scripts/sample.py": {"lines": [2], "tests": ["test_sample.py"]}})
    )
    monkeypatch.chdir(root)
    monkeypatch.setenv("MUTATION_STATS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("MUTATION_STATS_ROOT", str(root))
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("os.sched_getaffinity", lambda pid: {0})
    config = SimpleNamespace(source_paths=[Path("scripts")], pytest_add_cli_args_test_selection=["test_sample.py"])
    runner = SimpleNamespace(
        SourceFileMutationData=lambda **kwargs: SimpleNamespace(load=lambda: None, exit_code_by_key={"m": None}),
        Config=SimpleNamespace(get=lambda: config),
    )
    calls = []
    result = {
        "status": 0,
        "tests": {"scripts.sample.x_sample": ["test_sample.py::test_sample"]},
        "durations": {"test_sample.py::test_sample": 1},
        "cpu": 2,
    }

    flake = '{"test":"test_sample.py::test_sample","outcome":"flaky","errors":["first attempt fails"]}\n'

    def collect(engine: object, test_runner: object, buckets: list[list[str]], work: Path) -> list[dict]:
        calls.append(buckets)
        (work / "stats-0.flakes.jsonl").write_text(flake)
        return [result]

    monkeypatch.setattr("scripts.ci_mutation.selection.run_stats_buckets", collect)
    monkeypatch.setattr("scripts.ci_mutation.selection.stats_shards", lambda root, files, workers: [files])
    for head in ("old", "new"):
        output = root / f"{head}.json"
        with pytest.raises(SystemExit, match="0"):
            load_or_collect_stats(
                runner,
                SimpleNamespace(_pytest_add_cli_args=["-q"]),
                ["scripts/sample.py"],
                "collect",
                (str(output), head, "0", "1"),
            )
        assert json.loads(output.read_text())["key"] == head
        assert (root / "flakes-0.jsonl").read_text() == (flake if head == "old" else "")
        records = list((tmp_path / "cache").glob("*.json"))
        assert records
        assert all("first attempt fails" not in path.read_text() for path in records)
    assert calls == [[["test_sample.py"]]]


@pytest.mark.parametrize(
    "changed",
    [
        "source",
        "lines",
        "fixture",
        "imported_fixture",
        "imported_fixture_through_helper",
        "options",
        "lockfile",
        "excludes",
    ],
)
def test_incompatible_source_fixture_and_runtime_stats_are_recollected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    from scripts.ci_mutation.stats_cache import LOCKFILE, collect_cached_stats

    root = tmp_path / "repo"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    lockfile = root / LOCKFILE
    lockfile.parent.mkdir()
    lockfile.write_text("pytest==8.3.4\n")
    excludes = root / ".github/test-excludes.txt"
    excludes.write_text("opentelemetry-exporter-otlp-proto-grpc\n")
    source = root / "scripts/sample.py"
    source.write_text("def sample():\n    return 1\n")
    test = root / "tests/test_sample.py"
    test.write_text("from tests.test_shared import value\ndef test_sample():\n    assert value == 1\n")
    fixture = root / "tests/conftest.py"
    fixture.write_text("value = 1\n")
    shared = root / "tests/test_shared.py"
    shared.write_text("value = 1\n")
    if changed == "imported_fixture_through_helper":
        (root / "tests/helper.py").write_text("from tests.test_shared import value\n")
        test.write_text("from tests.helper import value\ndef test_sample():\n    assert value == 1\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "add",
            "scripts/sample.py",
            "tests/test_sample.py",
            "tests/conftest.py",
            "tests/test_shared.py",
            ".github/test-excludes.txt",
        ],
        cwd=root,
        check=True,
    )
    if changed == "imported_fixture_through_helper":
        subprocess.run(["git", "add", "tests/helper.py"], cwd=root, check=True)
    calls = []
    files = ["tests/test_sample.py"]

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {"scripts.sample.x_sample": ["tests/test_sample.py::test_sample"]},
                "durations": {"tests/test_sample.py::test_sample": 1},
                "cpu": 2,
            }
        ]

    changes = {"scripts/sample.py": [2]}
    options = ["-q"]
    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, changes, files, options, collect)
    if changed == "source":
        source.write_text("def sample():\n    return 2\n")
    elif changed == "lines":
        changes = {"scripts/sample.py": [1, 2]}
    elif changed == "fixture":
        fixture.write_text("value = 2\n")
    elif changed in ("imported_fixture", "imported_fixture_through_helper"):
        shared.write_text("value = 2\n")
    elif changed == "options":
        options = ["-x"]
    elif changed == "excludes":
        excludes.write_text("opentelemetry-exporter-otlp-proto-http\n")
    else:
        lockfile.write_text("pytest==8.3.5\n")
    collect_cached_stats(root, cache, changes, files, options, collect)
    assert calls == [files, files]


@pytest.mark.parametrize(
    ("package", "before", "after"),
    [
        ("sample-project", "1.2.dev3+gabc", "1.3.dev1+gdef"),
        ("sample-project", "1.2", "1.3"),
        ("dependency", "1.2.dev3", "1.2.dev4"),
        ("dependency", "1.2+gabc", "1.2+gdef"),
        ("dependency", "1.2", "1.3"),
    ],
)
def test_the_interpreter_and_installed_packages_alone_keep_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: str, before: str, after: str
) -> None:
    import sys
    from importlib import metadata
    from types import SimpleNamespace

    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py"]
    _repository(
        root,
        {files[0]: "def test_sample():\n    pass\n", "pyproject.toml": '[project]\nname = "sample-project"\n'},
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1}]

    cache = tmp_path / "cache"
    for version in (before, after):
        distribution = SimpleNamespace(metadata={"Name": package}, version=version)
        monkeypatch.setattr(sys, "version", version)
        monkeypatch.setattr(metadata, "distributions", lambda distribution=distribution: [distribution])
        collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files]


def test_persisted_stats_reuse_survives_a_new_python_process(tmp_path):
    import json
    import os
    import sys

    root = tmp_path / "repo"
    root.mkdir()
    (root / "tests").mkdir()
    (root / "tests/test_sample.py").write_text(
        "\n".join(f"from tests.test_shared_{index} import value" for index in range(5)) + "\n"
    )
    for index in range(5):
        (root / f"tests/test_shared_{index}.py").write_text("value = 1\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "add", "tests/test_sample.py", *(f"tests/test_shared_{index}.py" for index in range(5))],
        cwd=root,
        check=True,
    )
    script = """
import json
import sys
from pathlib import Path
from scripts.ci_mutation.stats_cache import collect_cached_stats

root, folder = map(Path, sys.argv[1:])
calls = root / "collections.json"
def collect(files):
    calls.write_text(json.dumps(json.loads(calls.read_text()) + [files] if calls.exists() else [files]))
    return [{"status": 0, "tests": {"scripts.sample.x_sample": ["tests/test_sample.py::test_sample"]}, "durations": {"tests/test_sample.py::test_sample": 1}, "cpu": 2}]
results = collect_cached_stats(root, folder, {"scripts/sample.py": [2]}, ["tests/test_sample.py"], ["-q"], collect)
assert sum(result["cpu"] for result in results) == 2
assert {test for result in results for test in result["durations"]} == {"tests/test_sample.py::test_sample"}
"""
    for seed in ("0", "42"):
        subprocess.run(
            [sys.executable, "-c", script, str(root), str(tmp_path / "cache")],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            capture_output=True,
            text=True,
            check=True,
        )
    assert json.loads((root / "collections.json").read_text()) == [["tests/test_sample.py"]]


def test_distinct_files_with_identical_dependency_closures_keep_all_coverage(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    root.mkdir()
    (root / "tests").mkdir()
    (root / "tests/test_a.py").write_text("import tests.test_b\ndef test_sample():\n    pass\n")
    (root / "tests/test_b.py").write_text("import tests.test_a\ndef test_sample():\n    pass\n")
    files = ["tests/test_a.py", "tests/test_b.py"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", *files], cwd=root, check=True)
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {"scripts.sample.x_sample": ["tests/test_a.py::test_sample", "tests/test_b.py::test_sample"]},
                "durations": {"tests/test_a.py::test_sample": 1, "tests/test_b.py::test_sample": 2},
                "cpu": 3,
            }
        ]

    cache = tmp_path / "cache"
    cold = collect_cached_stats(root, cache, {}, files, [], collect)
    warm = collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files]
    for results in (cold, warm):
        assert {test for result in results for test in result["tests"]["scripts.sample.x_sample"]} == {
            "tests/test_a.py::test_sample",
            "tests/test_b.py::test_sample",
        }
        assert {test for result in results for test in result["durations"]} == {
            "tests/test_a.py::test_sample",
            "tests/test_b.py::test_sample",
        }


def _repository(root: Path, files: dict[str, str]) -> None:
    root.mkdir()
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "add", *files], cwd=root, check=True)


@pytest.mark.parametrize("changed", ["unrelated", "imported", "test", "scope"])
def test_import_scoped_stats_only_recollect_affected_files(tmp_path, changed):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_a.py", "tests/test_b.py"]
    _repository(
        root,
        {
            "scripts/a.py": "value = 1\n",
            "scripts/b.py": "value = 1\n",
            "scripts/other.py": "value = 1\n",
            files[0]: "from scripts.a import value\n",
            files[1]: "from scripts.b import value\n",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {f"scripts.{Path(path).stem[5:]}.x_value": [f"{path}::test_value"] for path in chosen},
                "durations": {f"{path}::test_value": 1 for path in chosen},
                "cpu": 1,
            }
        ]

    cache = tmp_path / "cache"
    scope = {"scripts/a.py": [1], "scripts/b.py": [1]}
    collect_cached_stats(root, cache, scope, files, [], collect)
    if changed == "unrelated":
        (root / "scripts/other.py").write_text("value = 2\n")
        scope["scripts/other.py"] = [1]
    elif changed == "imported":
        (root / "scripts/a.py").write_text("value = 2\n")
    elif changed == "test":
        (root / files[0]).write_text("from scripts.a import value\nassert value\n")
    else:
        scope["scripts/a.py"] = [1, 2]
    warm = collect_cached_stats(root, cache, scope, files, [], collect)
    assert calls == [files] if changed == "unrelated" else calls == [files, files[:1]]
    assert {test for result in warm for test in result["durations"]} == {
        "tests/test_a.py::test_value",
        "tests/test_b.py::test_value",
    }


@pytest.mark.parametrize("change", ["add_source", "add_test", "add_document", "remove_source"])
def test_unrelated_inventory_changes_keep_stats_and_function_mapping(tmp_path, change):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    path = "tests/test_a.py"
    _repository(root, {path: "import scripts.a\n", "scripts/a.py": "value = 1\n", "other.py": "value = 2\n"})
    calls = []
    result = {
        "status": 0,
        "tests": {"scripts.a.x_value": [f"{path}::test_value"]},
        "durations": {f"{path}::test_value": 1},
        "cpu": 1,
    }

    def collect(chosen):
        calls.append(chosen)
        return [result]

    cache = tmp_path / "cache"
    cold = collect_cached_stats(root, cache, {"scripts/a.py": [1]}, [path], [], collect)
    if change == "remove_source":
        subprocess.run(["git", "rm", "-f", "--", "other.py"], cwd=root, check=True)
    else:
        added = {"add_source": "unrelated.py", "add_test": "tests/test_other.py", "add_document": "NOTES.md"}[change]
        (root / added).write_text("value = 3\n")
        subprocess.run(["git", "add", "--", added], cwd=root, check=True)
    warm = collect_cached_stats(root, cache, {"scripts/a.py": [1]}, [path], [], collect)
    assert calls == [[path]]
    assert warm == cold


@pytest.mark.parametrize("added", ["tests/conftest.py", "scripts/missing.py", "assets/data.txt"])
def test_new_applicable_discovery_inputs_recollect_stats(tmp_path, added):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    path = "tests/test_a.py"
    _repository(
        root,
        {
            path: "import scripts.missing\nfrom pathlib import Path\nPath('assets/data.txt')\n",
            "scripts/a.py": "value = 1\n",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, [path], [], collect)
    (root / added).parent.mkdir(parents=True, exist_ok=True)
    (root / added).write_text("value = 2\n")
    subprocess.run(["git", "add", "--", added], cwd=root, check=True)
    collect_cached_stats(root, cache, {}, [path], [], collect)
    assert calls == [[path], [path]]


@pytest.mark.parametrize("discovery", ["glob", "iter_modules"])
def test_measured_stats_recollect_new_dynamically_discovered_modules(tmp_path, discovery):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    path = "tests/test_plugins.py"
    sources = {
        "glob": "import importlib\nfrom pathlib import Path\nfor path in Path('plugins').glob('*.py'):\n    importlib.import_module(f'plugins.{path.stem}')\n",
        "iter_modules": "import importlib\nimport pkgutil\nimport plugins\nfor item in pkgutil.iter_modules(plugins.__path__):\n    importlib.import_module(f'plugins.{item.name}')\n",
    }
    _repository(root, {path: sources[discovery], "plugins/__init__.py": "", "plugins/a.py": "value = 1\n"})
    calls = []
    test = f"{path}::test_plugins"

    def collect(chosen):
        calls.append(chosen)
        loaded = [str(source.relative_to(root)) for source in sorted((root / "plugins").glob("*.py"))]
        return [
            {
                "status": 0,
                "tests": {f"plugins.{Path(source).stem}.x_value": [test] for source in loaded},
                "durations": {test: 1},
                "dependencies": {test: [path, *loaded]},
                "cpu": 1,
            }
        ]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, [path], [], collect)
    (root / "plugins/notes.md").write_text("unrelated\n")
    (root / "outside.py").write_text("value = 3\n")
    subprocess.run(["git", "add", "--", "plugins/notes.md", "outside.py"], cwd=root, check=True)
    collect_cached_stats(root, cache, {}, [path], [], collect)
    assert calls == [[path]]
    (root / "plugins/b.py").write_text("value = 2\n")
    subprocess.run(["git", "add", "--", "plugins/b.py"], cwd=root, check=True)
    warm = collect_cached_stats(root, cache, {}, [path], [], collect)
    assert calls == [[path], [path]]
    assert "plugins.b.x_value" in warm[0]["tests"]


@pytest.mark.parametrize("discovery", ["file_parent", "default_path"])
def test_measured_stats_recollect_unbounded_discovery_additions(tmp_path, discovery):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    path = "tests/test_plugins.py"
    source = {
        "file_parent": "import importlib\nfrom pathlib import Path\nfor item in Path(__file__).parent.glob('plugin_*.py'):\n    importlib.import_module(f'tests.{item.stem}')\n",
        "default_path": "import importlib\nfrom pkgutil import iter_modules as discover\nfor item in discover():\n    importlib.import_module(item.name)\n",
    }[discovery]
    folder = "tests/" if discovery == "file_parent" else ""
    _repository(root, {path: source, f"{folder}plugin_a.py": "value = 1\n"})
    calls = []
    test = f"{path}::test_plugins"

    def collect(chosen):
        calls.append(chosen)
        loaded = sorted(str(item.relative_to(root)) for item in (root / folder).glob("plugin_*.py"))
        return [
            {
                "status": 0,
                "tests": {f"{Path(item).stem}.x_value": [test] for item in loaded},
                "durations": {test: 1},
                "dependencies": {test: [path, *loaded]},
                "cpu": 1,
            }
        ]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, [path], [], collect)
    (root / "NOTES.md").write_text("unrelated\n")
    subprocess.run(["git", "add", "--", "NOTES.md"], cwd=root, check=True)
    collect_cached_stats(root, cache, {}, [path], [], collect)
    assert calls == [[path]]
    added = f"{folder}plugin_b.py"
    (root / added).write_text("value = 2\n")
    subprocess.run(["git", "add", "--", added], cwd=root, check=True)
    warm = collect_cached_stats(root, cache, {}, [path], [], collect)
    assert calls == [[path], [path]]
    assert "plugin_b.x_value" in warm[0]["tests"]


@pytest.mark.parametrize(
    "expression",
    [
        "Path('pluginsX/').glob('*.py')",
        "Path('pluginsX/').rglob('*.py')",
        "Path('pluginsX/').iterdir()",
        "os.listdir('pluginsX/')",
        "os.scandir('pluginsX/')",
        "os.walk('pluginsX/')",
        "scan('pluginsX/')",
    ],
)
def test_directory_discovery_keeps_its_scope_and_glob_filter(expression):
    import ast

    from scripts.ci_mutation.stats_cache import discovery_inputs

    resources = {"pluginsX/": {"pluginsX/a.py", "pluginsX/note.txt"}, "elsewhere/": {"elsewhere/b.py"}}
    nodes = list(ast.walk(ast.parse("from os import listdir as scan\n" + expression)))
    expected = {"pluginsX/a.py"} if "glob" in expression else resources["pluginsX/"]
    assert discovery_inputs(nodes, set(), resources) == expected


@pytest.mark.parametrize("module", ["glob", "os"])
def test_imported_scanner_aliases_use_the_imported_module(module):
    import ast

    from scripts.ci_mutation.stats_cache import discovery_inputs

    source = (
        "from glob import glob as scan\nscan('pluginsX/*.py')"
        if module == "glob"
        else "from os import scandir as scan\nscan('pluginsX/')"
    )
    resources = {"pluginsX/": {"pluginsX/a.py", "pluginsX/note.txt"}, "elsewhere/": {"elsewhere/b.py"}}
    expected = {"pluginsX/a.py"} if module == "glob" else resources["pluginsX/"]
    assert discovery_inputs(list(ast.walk(ast.parse(source))), set(), resources) == expected


@pytest.mark.parametrize("scanner", ["iter_modules", "walk_packages"])
@pytest.mark.parametrize("package", ["plugins", "plugins.nested"])
def test_package_discovery_keeps_nested_package_scope_and_python_files(scanner, package):
    import ast

    from scripts.ci_mutation.stats_cache import discovery_inputs

    folder = package.replace(".", "/")
    resources = {folder + "/": {folder + "/a.py", folder + "/note.txt"}, "elsewhere/": {"elsewhere/b.py"}}
    source = f"pkgutil.{scanner}({package}.__path__)"
    assert discovery_inputs(list(ast.walk(ast.parse(source))), {package}, resources) == {folder + "/a.py"}


def test_ast_walk_does_not_add_repository_discovery_or_skip_later_scans():
    import ast

    from scripts.ci_mutation.stats_cache import discovery_inputs

    resources = {"pluginsX/": {"pluginsX/a.py"}, "elsewhere/": {"elsewhere/b.py"}}
    source = "ast.walk(tree)\nPath('pluginsX/').glob('*.py')"
    assert discovery_inputs(list(ast.walk(ast.parse(source))), set(), resources) == {"pluginsX/a.py"}


@pytest.mark.parametrize("changed", ["documentation", "unrelated_fixture", "comment", "initializer"])
def test_stats_reuse_tracks_only_applicable_import_and_fixture_inputs(tmp_path, changed):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py"]
    _repository(
        root,
        {
            "scripts/pkg/__init__.py": "value = 1\n",
            "scripts/pkg/sample.py": "# importlib is mentioned in a comment\nvalue = 1\n",
            "scripts/other.py": "value = 1\n",
            files[0]: "import scripts.pkg.sample\n",
            "tests/other/conftest.py": "value = 1\n",
            "docs/other.md": "original\n",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1}]

    cache = tmp_path / "cache"
    scope = {"scripts/pkg/sample.py": [2]}
    collect_cached_stats(root, cache, scope, files, [], collect)
    target = {
        "documentation": "docs/other.md",
        "unrelated_fixture": "tests/other/conftest.py",
        "comment": "scripts/other.py",
        "initializer": "scripts/pkg/__init__.py",
    }[changed]
    (root / target).write_text("value = 2\n")
    collect_cached_stats(root, cache, scope, files, [], collect)
    assert calls == ([files, files] if changed == "initializer" else [files])


@pytest.mark.parametrize(
    "loader",
    [
        "from importlib import import_module as load\nload('scripts.a')\n",
        "from builtins import __import__ as load\nload('scripts.a')\n",
        "from importlib.util import spec_from_file_location as load\nload('virtual', location)\n",
        "import importlib.util\nimportlib.util.spec_from_file_location('virtual', location)\n",
        "import importlib\nimportlib.import_module('.a', package='scripts')\n",
        "import importlib\nload = importlib.import_module\nload('scripts.a')\n",
    ],
)
def test_dynamic_import_dependencies_invalidate_stats_alongside_static_imports(tmp_path, loader):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py"]
    _repository(
        root,
        {
            files[0]: "import scripts.other\n" + loader,
            "scripts/a.py": "value = 1\n",
            "scripts/other.py": "value = 1\n",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    (root / "scripts/a.py").write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files]


def test_a_test_module_its_test_file_loads_as_a_pytest_plugin_keys_its_stats(tmp_path: Path) -> None:
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py"]
    _repository(
        root,
        {
            files[0]: 'pytest_plugins = ["tests.test_plugin"]\nunrelated = ["tests.test_other"]\n',
            "tests/test_plugin.py": "value = 1\n",
            "tests/test_other.py": "value = 1\n",
        },
    )
    calls: list[list[str]] = []

    def collect(chosen: list[str]) -> list[dict[str, object]]:
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    (root / "tests/test_other.py").write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files]
    (root / "tests/test_plugin.py").write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files]


@pytest.mark.parametrize(
    "loader",
    [
        "from importlib import import_module as load\nload('scripts.a')\n",
        "from builtins import __import__ as load\nload('scripts.a')\n",
        "import importlib\nimportlib.import_module('scripts.a')\n",
    ],
)
def test_literal_dynamic_imports_reuse_stats_after_unrelated_source_changes(tmp_path, loader):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py"]
    _repository(
        root,
        {
            files[0]: "import scripts.other\n" + loader,
            "scripts/a.py": "value = 1\n",
            "scripts/other.py": "value = 1\n",
            "scripts/unrelated.py": "value = 1\n",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    (root / "scripts/unrelated.py").write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files]


def test_implicit_source_stats_track_resources_and_recollect_when_a_resource_is_deleted(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py"]
    _repository(
        root,
        {
            files[0]: "def test_sample():\n    pass\n",
            "scripts/a.py": "from pathlib import Path\nvalue = Path('assets/data').read_text()\n",
            "assets/data": "original",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0 if (root / "assets/data").exists() else 1, "tests": {}, "durations": {}, "cpu": 1}]

    cache = tmp_path / "cache"
    scope = {"scripts/a.py": [2]}
    collect_cached_stats(root, cache, scope, files, [], collect)
    (root / "assets/data").write_text("changed")
    collect_cached_stats(root, cache, scope, files, [], collect)
    (root / "assets/data").unlink()
    with pytest.raises(SystemExit):
        collect_cached_stats(root, cache, scope, files, [], collect)
    assert calls == [files, files, files]


@pytest.mark.parametrize(
    "target", ["pyproject.toml", "scripts/ci_mutation/example.py", "assets/image.bin", "scripts/fixture.py"]
)
def test_configuration_collector_fixture_and_binary_resource_changes_recollect_stats(tmp_path, target):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py"]
    _repository(
        root,
        {
            files[0]: "import scripts.a\nfrom pathlib import Path\nPath('assets/image.bin').read_bytes()\n",
            "scripts/a.py": "value = 1\n",
            "scripts/fixture.py": "value = 1\n",
            "tests/conftest.py": "import scripts.fixture\n",
            "scripts/ci_mutation/example.py": "value = 1\n",
            "pyproject.toml": "[tool.pytest.ini_options]\n",
            "assets/image.bin": "original",
        },
    )
    (root / "assets/image.bin").write_bytes(bytes([0, 255]))
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files]
    (root / target).write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files]


@pytest.mark.feature("tracked-test-lock-cache-keys")
def test_a_test_requirements_lock_change_recollects_stats(tmp_path: Path) -> None:
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py"]
    lock = ".github/test-requirements.lock"
    _repository(root, {files[0]: "def test_sample():\n    pass\n", lock: "pytest==8.4.2\n"})
    calls: list[list[str]] = []

    def collect(chosen: list[str]) -> list[dict[str, object]]:
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files]
    (root / lock).write_text("pytest==8.4.3\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files]


def test_each_stored_file_keeps_only_its_own_tests_from_every_bucket(tmp_path, capsys):
    import json

    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_a.py", "tests/test_b.py"]
    _repository(root, {path: "def test_sample():\n    pass\n" for path in files})
    a, b = "tests/test_a.py::Case::test_sample", "tests/test_b.py::Case::test_sample"
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [
            {"status": 0, "tests": {"m.x_first": [a]}, "durations": {a: 1}, "cpu": 4},
            {"status": 0, "tests": {"m.x_first": [b], "m.x_second": [b]}, "durations": {b: 2}, "cpu": 2},
        ]

    cache = tmp_path / "cache/nested"
    collect_cached_stats(root, cache, {}, files, [], collect)
    cold = capsys.readouterr().out
    assert "Mutation stats cache: reused 0 test files; collect 2" in cold
    assert 'Mutation stats cache: misses {"no pointer": 2}; changed inputs []\n' in cold
    (root / files[1]).write_text("def test_sample():\n    assert True\n")
    warm = collect_cached_stats(root, cache, {}, files, [], collect)
    out = capsys.readouterr().out
    assert "Mutation stats cache: reused 1 test files; collect 1" in out
    assert "Mutation stats cache: misses {\"inputs\": 1}; changed inputs [('tests/test_b.py', 1)]\n" in out
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert "misses" not in capsys.readouterr().out
    assert calls == [files, ["tests/test_b.py"]]
    assert warm[0] == {"status": 0, "tests": {"m.x_first": [a], "m.x_second": []}, "durations": {a: 1}, "cpu": 3.0}
    stored = [json.loads(path.read_text())["durations"] for path in cache.glob("*.json")]
    assert {a: 1} in stored and {b: 2} in stored


def test_a_selected_test_id_keeps_its_own_tests_and_recollects_when_its_file_changes(tmp_path):
    import json

    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    _repository(root, {"tests/test_a.py": "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n"})
    entries = ["tests/test_a.py::test_one", "tests/test_a.py::test_two"]
    one, two, wide = f"{entries[0]}[1]", entries[1], "tests/test_a.py::test_one_more"
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {"m.x_f": [one, two, wide]}, "durations": {one: 1, two: 2, wide: 4}, "cpu": 2}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, entries, [], collect)
    stored = [json.loads(path.read_text())["durations"] for path in cache.glob("*.json")]
    assert sorted(stored, key=str) == sorted([{one: 1}, {two: 2}], key=str)
    collect_cached_stats(root, cache, {}, entries, [], collect)
    assert calls == [entries]
    (root / "tests/test_a.py").write_text("def test_one():\n    assert True\n")
    collect_cached_stats(root, cache, {}, entries, [], collect)
    assert calls == [entries, entries]


@pytest.mark.parametrize(
    ("layout", "helper"),
    [
        ({"tests/sub/test_sample.py": "from .test_helper import value\n"}, "tests/sub/test_helper.py"),
        ({"tests/test_sample.py": "from test_helper import value\n"}, "test_helper.py"),
        ({"tests/test_sample.py": "import importlib\nimportlib.import_module('test_helper')\n"}, "test_helper.py"),
        ({"tests/test_sample.py": "__import__('test_helper')\n"}, "test_helper.py"),
    ],
)
def test_relative_top_level_and_dynamic_imports_recollect_a_changed_helper(tmp_path, layout, helper):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    _repository(root, {**layout, helper: "value = 1\n"})
    files = list(layout)
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files]
    (root / helper).write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files]


def test_stats_cache_refuses_a_root_outside_git(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    with pytest.raises(subprocess.CalledProcessError):
        collect_cached_stats(tmp_path, tmp_path / "cache", {}, [], [], lambda files: [])


@pytest.mark.parametrize("record", ["stale", "failed"])
def test_stats_cache_recollects_a_record_that_does_not_match_its_current_inputs(tmp_path, record):
    import json

    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_a.py"]
    _repository(root, {"scripts/a.py": "value = 1\n", files[0]: "from scripts.a import value\n"})
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {"scripts.a.x_value": ["tests/test_a.py::test_value"]},
                "durations": {"tests/test_a.py::test_value": 1},
                "cpu": 1,
            }
        ]

    cache = tmp_path / "cache"
    cold = collect_cached_stats(root, cache, {}, files, [], collect)
    original = next(cache.glob("*.json"))
    stale = original.read_text()
    assert collect_cached_stats(root, cache, {}, files, [], collect) == cold
    assert calls == [files]
    (root / "scripts/a.py").write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files]
    if record == "stale":
        next(path for path in cache.glob("*.json") if path != original).write_text(stale)
    else:
        for path in cache.glob("*.json"):
            value = json.loads(path.read_text())
            value["status"] = 1
            path.write_text(json.dumps(value))
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files, files]


def test_stats_cache_recollects_a_truncated_record_and_saves_only_valid_records(tmp_path):
    import json

    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_a.py", "tests/test_b.py"]
    _repository(root, {path: "def test_sample():\n    pass\n" for path in files})
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {"tests.x_sample": [f"{path}::test_sample" for path in chosen]},
                "durations": {f"{path}::test_sample": 1 for path in chosen},
                "cpu": 1,
            }
        ]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    for path in cache.glob("*.json"):
        path.write_text(path.read_text()[:20])
    part = collect_cached_stats(root, cache, {}, files[:1], [], collect)
    assert calls == [files, files[:1]]
    assert {test for result in part for test in result["durations"]} == {"tests/test_a.py::test_sample"}
    records = [json.loads(path.read_text()) for path in cache.glob("*.json")]
    assert len(records) == 1
    assert records[0]["status"] == 0
    assert collect_cached_stats(root, cache, {}, files[:1], [], collect) == part
    assert calls == [files, files[:1]]


def test_real_fixture_imports_do_not_invalidate_measured_unrelated_test_stats(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    content = {
        "tests/conftest.py": "from tests import fixture_probe\n",
        "tests/fixture_probe.py": 'directory = "scripts/"\n',
        "scripts/cache_unrelated_probe.py": "value = 1\n",
        "scripts/cache_covered_probe.py": "value = 1\n",
        "tests/test_cache_measured_probe.py": "from scripts.cache_covered_probe import value\n",
    }
    root = tmp_path / "repo"
    _repository(root, content)
    files = ["tests/test_cache_measured_probe.py"]
    node = files[0] + "::test_value"
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {"scripts.cache_covered_probe.x_value": [node]},
                "durations": {node: 1},
                "cpu": 1,
                "dependencies": {node: ["scripts/cache_covered_probe.py"]},
            }
        ]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    (root / "scripts/cache_unrelated_probe.py").write_text("value = 2\n")
    warm = collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files]
    assert warm[0]["durations"] == {node: 1}
    assert warm[0]["dependencies"] == {node: ["scripts/cache_covered_probe.py"]}
    assert warm[0]["reused"] is True
    (root / "scripts/cache_covered_probe.py").write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files]


@pytest.mark.parametrize("changed", ["module", "scope", "deleted", "stale", "truncated"])
def test_stored_statistics_validate_record_inputs_and_mutation_scope(tmp_path, changed):
    import hashlib

    from scripts.ci_mutation.stats_dependencies import StoredStats

    root = tmp_path / "repo"
    folder = tmp_path / "cache"
    folder.mkdir()
    _repository(root, {"module.py": "value = 1\n"})
    inputs = {"module.py": hashlib.sha256((root / "module.py").read_bytes()).hexdigest()}
    context = {"inventory": ["module.py"]}
    cache = StoredStats(root, folder, context, {"module.py": [1]})
    result = {"status": 0, "tests": {}, "durations": {"test": 1}, "cpu": 1}
    cache.save("tests/test_module.py", result, inputs, True)
    assert cache.load("tests/test_module.py") == {**result, "reused": True}
    if changed == "module":
        (root / "module.py").write_text("value = 2\n")
    elif changed == "scope":
        cache.changes = {"module.py": [1, 2]}
    elif changed == "deleted":
        (root / "module.py").unlink()
    elif changed == "stale":
        next(folder.glob("*.json")).write_text(
            f'{{"key":"stale","status":0,"inputs":"{cache.share(inputs)}","scope":{{}},"measured":true}}'
        )
    else:
        next(folder.glob("*.json")).write_text('{"key":"')
    assert cache.load("tests/test_module.py") is None
    assert len(list(folder.glob("*.json"))) == (changed != "truncated")
    reason = {"module": "inputs", "deleted": "inputs", "scope": "scope", "stale": "identity", "truncated": "no record"}[
        changed
    ]
    assert cache.misses == {reason: 1}
    assert cache.changed == ({"module.py": 1} if reason == "inputs" else {})


def test_stored_statistics_count_a_missing_pointer_and_a_failed_check(tmp_path):
    from scripts.ci_mutation.stats_dependencies import StoredStats

    root = tmp_path / "repo"
    folder = tmp_path / "cache"
    folder.mkdir()
    _repository(root, {"module.py": "value = 1\n"})
    cache = StoredStats(root, folder, {"inventory": ["module.py"]}, {})
    assert cache.load("tests/test_module.py") is None
    cache.save("tests/test_module.py", {"status": 0, "tests": {}, "durations": {}, "cpu": 1}, {}, False)
    assert cache.load("tests/test_module.py", lambda inputs, measured, discovery: False) is None
    assert cache.misses == {"no pointer": 1, "check": 1}


def test_stored_statistics_keep_each_input_map_and_dependency_list_once(tmp_path):
    import hashlib
    import json

    from scripts.ci_mutation.stats_dependencies import SHARED, StoredStats

    root = tmp_path / "repo"
    folder = tmp_path / "cache"
    folder.mkdir()
    _repository(root, {"module.py": "value = 1\n"})
    inputs = {"module.py": hashlib.sha256((root / "module.py").read_bytes()).hexdigest()}
    cache = StoredStats(root, folder, {"inventory": ["module.py"]}, {})
    paths = [f"tests/test_{index}.py" for index in range(3)]
    for path in paths:
        result = {"status": 0, "tests": {}, "durations": {path: 1}, "cpu": 1, "dependencies": {path: ["module.py"]}}
        cache.save(path, result, inputs, True)
    stored = [json.loads(record.read_text()) for record in folder.glob("*.json")]
    assert len(stored) == 3
    assert sorted(path.read_text() for path in folder.glob(f"*{SHARED}")) == sorted(
        [json.dumps(["module.py"]), json.dumps(inputs)]
    )
    assert all("module.py" not in json.dumps(record) for record in stored)
    reader = StoredStats(root, folder, {"inventory": ["module.py"]}, {})
    assert reader.load(paths[0])["dependencies"] == {paths[0]: ["module.py"]}
    next(folder.glob(f"*{SHARED}")).unlink()
    assert reader.load(paths[1]) is None
    assert reader.misses == {"no inputs": 1}


def test_cache_miss_report_names_only_the_five_most_changed_inputs(tmp_path, capsys):
    import ast

    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    modules = [f"m{index}" for index in range(6)]
    tests = [f"tests/test_{index}.py" for index in range(6)]
    imports = {test: f"import {', '.join(modules[: 6 - index])}\n" for index, test in enumerate(tests)}
    _repository(root, {f"{name}.py": "value = 1\n" for name in modules} | imports)

    def collect(chosen):
        return [{"status": 0, "tests": {}, "durations": {f"{test}::test_one": 1}, "cpu": 1} for test in chosen]

    collect_cached_stats(root, tmp_path / "cache", {}, tests, [], collect)
    for name in modules:
        (root / f"{name}.py").write_text("value = 2\n")
    capsys.readouterr()
    collect_cached_stats(root, tmp_path / "cache", {}, tests, [], collect)
    line = next(line for line in capsys.readouterr().out.splitlines() if "misses" in line)
    assert line.startswith('Mutation stats cache: misses {"inputs": 6}; changed inputs ')
    assert ast.literal_eval(line.partition("changed inputs ")[2]) == [
        (f"{name}.py", 6 - index) for index, name in enumerate(modules[:5])
    ]


def test_rewritten_test_durations_keep_the_stored_stats_of_files_that_name_them(tmp_path, capsys):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    test = "tests/test_a.py"
    _repository(
        root,
        {
            ".test_durations": "{}\n",
            "tests/conftest.py": 'DURATIONS = ".test_durations"\n',
            test: "def test_one():\n    pass\n",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {f"{test}::test_one": 1}, "cpu": 1} for _ in chosen]

    collect_cached_stats(root, tmp_path / "cache", {}, [test], [], collect)
    (root / ".test_durations").write_text('{"tests/test_a.py::test_one": 2}\n')
    capsys.readouterr()
    collect_cached_stats(root, tmp_path / "cache", {}, [test], [], collect)
    assert "Mutation stats cache: reused 1 test files; collect 0" in capsys.readouterr().out
    assert calls == [[test]]
    (root / "tests/conftest.py").write_text('DURATIONS = ".test_durations"\nVALUE = 2\n')
    collect_cached_stats(root, tmp_path / "cache", {}, [test], [], collect)
    assert calls == [[test], [test]]


@pytest.mark.parametrize(
    "evidence", [{}, {"tests/test_a.py::test_one": []}, {"tests/test_a.py::test_one": ["module.py"]}]
)
def test_partial_runtime_statistics_are_never_reused(tmp_path, evidence):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_a.py"]
    _repository(root, {"module.py": "value = 1\n", files[0]: "import module\n"})
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {},
                "durations": {files[0] + "::test_one": 1, files[0] + "::test_two": 1},
                "cpu": 1,
                "dependencies": evidence,
            }
        ]

    folder = tmp_path / "cache"
    collect_cached_stats(root, folder, {}, files, [], collect)
    collect_cached_stats(root, folder, {}, files, [], collect)
    assert calls == [files, files]
    assert list(folder.iterdir()) == []


def test_stored_statistics_are_portable_across_metadata_ordering(tmp_path):
    import hashlib

    from scripts.ci_mutation.stats_dependencies import StoredStats

    root = tmp_path / "repo"
    folder = tmp_path / "cache"
    folder.mkdir()
    _repository(root, {"a.py": "value = 1\n", "b.py": "value = 2\n"})
    inputs = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ("a.py", "b.py")}
    first = StoredStats(root, folder, {"inventory": ["a.py", "b.py"], "runtime": "same"}, {})
    result = {"status": 0, "tests": {}, "durations": {"test": 1}, "cpu": 1}
    first.save("tests/test_a.py", result, inputs, True)
    second = StoredStats(root, folder, {"runtime": "same", "inventory": ["a.py", "b.py"]}, {})
    assert second.load("tests/test_a.py") == {**result, "reused": True}
    second.save("tests/test_a.py", result, dict(reversed(list(inputs.items()))), True)
    assert len(list(folder.glob("*.json"))) == 1


def test_file_stats_pass_sorted_changed_lines_and_runner_options_to_the_cache(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from scripts.ci_mutation.selection import collect_file_stats

    (tmp_path / "changed-lines.json").write_text(json.dumps({"scripts/sample.py": {"lines": [5, 2], "tests": []}}))
    monkeypatch.setenv("MUTATION_STATS_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("MUTATION_STATS_ROOT", str(tmp_path / "root"))
    seen = []

    def cached(root, folder, scope, files, options, collect):
        seen.append((root, folder, scope, files, options))
        return ["cached"]

    monkeypatch.setattr("scripts.ci_mutation.stats_cache.collect_cached_stats", cached)
    runner = SimpleNamespace(_pytest_add_cli_args=["-q", "-x"])
    assert collect_file_stats(None, runner, ["tests/test_sample.py"], 1, tmp_path) == ["cached"]
    assert seen == [
        (
            tmp_path / "root",
            tmp_path / "cache",
            {"scripts/sample.py": [2, 5]},
            ["tests/test_sample.py"],
            ["-q", "-x"],
        )
    ]


@pytest.mark.parametrize("changed", ["unloaded", "loaded", "loaded_resource", "other_test"])
def test_dynamic_import_stats_track_the_modules_the_collection_loaded(tmp_path, changed):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py", "tests/test_other.py"]
    _repository(
        root,
        {
            files[0]: "import importlib\nimport sys\nimportlib.import_module(sys.argv[0])\n",
            files[1]: "def test_other():\n    pass\n",
            "scripts/a.py": (
                "import importlib\nimport sys\nfrom pathlib import Path\n"
                "importlib.import_module(sys.argv[0])\nvalue = Path('assets/data').read_text()\n"
            ),
            "scripts/unloaded.py": "value = 1\n",
            "assets/data": "original",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {},
                "durations": {},
                "cpu": 1,
                "files": chosen,
                "modules": [*chosen, "scripts/a.py", "outside/site.py"],
            }
        ]

    cache = tmp_path / "cache"
    cold = collect_cached_stats(root, cache, {}, files, [], collect)
    assert all("modules" not in result and "files" not in result for result in cold)
    target, text = {
        "unloaded": ("scripts/unloaded.py", "value = 2\n"),
        "loaded": ("scripts/a.py", "value = 2\n"),
        "loaded_resource": ("assets/data", "changed"),
        "other_test": (files[1], "def test_other():\n    assert True\n"),
    }[changed]
    (root / target).write_text(text)
    warm = collect_cached_stats(root, cache, {}, files, [], collect)
    assert all(not result.keys() & {"modules", "files", "loaded"} for result in warm)
    assert (
        calls
        == {
            "unloaded": [files],
            "loaded": [files, [files[0]]],
            "loaded_resource": [files, [files[0]]],
            "other_test": [files, [files[1]]],
        }[changed]
    )


def test_dynamic_import_stats_without_a_module_record_track_every_module(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py"]
    _repository(
        root,
        {
            files[0]: "import importlib\nimport sys\nimportlib.import_module(sys.argv[0])\n",
            "scripts/unloaded.py": "value = 1\n",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1, "files": chosen}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    collect_cached_stats(root, cache, {}, files, [], collect)
    (root / "scripts/unloaded.py").write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files]


def test_dynamic_import_stats_recollect_when_a_loaded_module_changes_its_mutated_lines(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_sample.py"]
    _repository(
        root,
        {
            files[0]: "import importlib\nimport sys\nimport scripts.other\nimportlib.import_module(sys.argv[0])\n",
            "scripts/a.py": "def first():\n    return 1\n\n\ndef second():\n    return 2\n",
            "scripts/other.py": "value = 1\n",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {}, "cpu": 1, "files": chosen, "modules": ["scripts/a.py"]}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {"scripts/a.py": [2]}, files, [], collect)
    collect_cached_stats(root, cache, {"scripts/a.py": [2]}, files, [], collect)
    collect_cached_stats(root, cache, {"scripts/a.py": [6]}, files, [], collect)
    assert calls == [files, files]


def test_loaded_files_name_repository_modules_under_the_first_matching_base(tmp_path):
    import types

    from scripts.ci_mutation.selection import loaded_files

    work = tmp_path / "work"
    (work / "mutants/scripts").mkdir(parents=True)
    (work / "conftest.py").write_text("")
    (work / "mutants/scripts/a.py").write_text("")
    entries = {
        "mutated": types.SimpleNamespace(__file__=str(work / "mutants/scripts/a.py")),
        "copied": types.SimpleNamespace(__file__=str(work / "conftest.py")),
        "outside": types.SimpleNamespace(__file__=str(tmp_path / "site/b.py")),
        "builtin": types.SimpleNamespace(),
        "namespace": types.SimpleNamespace(__file__=None),
    }
    assert loaded_files([work / "mutants", work], entries) == ["conftest.py", "scripts/a.py"]


@pytest.mark.parametrize("record_files", [True, False])
def test_dynamic_import_stats_check_only_the_modules_of_their_own_bucket(tmp_path, record_files):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = ["tests/test_a.py", "tests/test_b.py"]
    loader = "import importlib\nimport sys\nimport scripts.other\nimportlib.import_module(sys.argv[0])\n"
    _repository(
        root,
        {
            files[0]: loader,
            files[1]: loader + "# b\n",
            "scripts/other.py": "value = 1\n",
            "scripts/a_module.py": "value = 1\n",
            "scripts/b_module.py": "value = 1\n",
        },
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        buckets = [
            {"status": 0, "tests": {}, "durations": {}, "cpu": 1, "modules": [f"scripts/{path[-4]}_module.py"]}
            | ({"files": [path]} if record_files else {})
            for path in chosen
        ]
        return buckets if record_files else [{**buckets[0], "modules": [m for b in buckets for m in b["modules"]]}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    (root / "scripts/b_module.py").write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == ([files, [files[1]]] if record_files else [files, files])


def test_measured_stats_trace_every_covered_source_in_one_walk(tmp_path, monkeypatch):
    from scripts.ci_mutation import stats_cache

    root = tmp_path / "repo"
    sources = [f"scripts/covered_{index}/source.py" for index in range(3)]
    _repository(
        root,
        {
            **{source: "value = 1\n" for source in sources},
            "scripts/covered_2/conftest.py": "from scripts.shared_helper import value\n",
            "scripts/shared_helper.py": "value = 1\n",
            "tests/test_measured.py": "def test_value():\n    pass\n",
        },
    )
    files = ["tests/test_measured.py"]
    node = files[0] + "::test_value"
    traced = []
    trace = stats_cache.test_inputs
    monkeypatch.setattr(
        stats_cache, "test_inputs", lambda root, path, *rest: traced.append(path) or trace(root, path, *rest)
    )
    calls = []

    def collect(chosen):
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {node: 1}, "cpu": 1, "dependencies": {node: sources}}]

    cache = tmp_path / "cache"
    stats_cache.collect_cached_stats(root, cache, {}, files, [], collect)
    assert traced == ["tests/test_measured.py", sources]
    stats_cache.collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files]
    (root / "scripts/shared_helper.py").write_text("value = 2\n")
    stats_cache.collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files]


def test_a_measured_record_parses_each_closure_source_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.ci_mutation import stats_cache

    root = tmp_path / "repo"
    sources = {
        "scripts/scan.py": "from pathlib import Path\n\n\ndef names():\n    return sorted(Path('data').glob('*.json'))\n",
        "scripts/helper.py": "from scripts.scan import names\n",
        "tests/test_measured.py": "def test_names():\n    assert True\n",
    }
    _repository(root, {**sources, "data/a.json": "{}\n"})
    node = "tests/test_measured.py::test_names"
    parsed: Counter[str] = Counter()
    parse = ast.parse

    def counted(text: str) -> ast.Module:
        parsed[text] += 1
        return parse(text)

    monkeypatch.setattr(stats_cache, "ast", SimpleNamespace(**{**vars(ast), "parse": counted}))
    calls: list[list[str]] = []

    def collect(chosen: list[str]) -> list[dict[str, object]]:
        calls.append(chosen)
        return [
            {"status": 0, "tests": {}, "durations": {node: 1}, "cpu": 1, "dependencies": {node: ["scripts/helper.py"]}}
        ]

    cache = tmp_path / "cache"
    for _ in range(2):
        parsed.clear()
        stats_cache.collect_cached_stats(root, cache, {}, ["tests/test_measured.py"], [], collect)
        assert {path: parsed[text] for path, text in sources.items()} == dict.fromkeys(sources, 1)
    assert calls == [["tests/test_measured.py"]]


def _walked_lists(walked: list[str | list[str]]) -> list[list[str]]:
    return [path for path in walked if isinstance(path, list)]


@pytest.mark.feature("mutation-stats-shared-closure")
def test_test_ids_sharing_one_dependency_list_walk_its_closure_once_per_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.ci_mutation import stats_cache

    root = tmp_path / "repo"
    dependencies = ["scripts/covered_0.py", "scripts/covered_1.py", "tests/test_shared.py"]
    count = 20
    _repository(
        root,
        {
            "scripts/covered_0.py": "value = 1\n",
            "scripts/covered_1.py": "value = 1\n",
            "tests/test_shared.py": "".join(f"def test_{index:02d}():\n    pass\n" for index in range(count)),
        },
    )
    entries = [f"tests/test_shared.py::test_{index:02d}" for index in range(count)]
    walked: list[str | list[str]] = []
    walk = stats_cache.test_inputs

    def traced(
        root: Path,
        path: str | list[str],
        modules: dict[str, str],
        records: dict[str, tuple[str, set[str], list[ast.AST]]],
        resources: dict[str, set[str]],
        dynamic_imports: bool = True,
        discovered: dict[str, set[str]] | None = None,
    ) -> dict[str, str]:
        walked.append(path)
        return walk(root, path, modules, records, resources, dynamic_imports, discovered)

    monkeypatch.setattr(stats_cache, "test_inputs", traced)
    calls: list[list[str]] = []

    def collect(chosen: list[str]) -> list[dict[str, object]]:
        calls.append(chosen)
        return [
            {
                "status": 0,
                "tests": {},
                "durations": dict.fromkeys(chosen, 1),
                "cpu": 1,
                "dependencies": {entry: list(dependencies) for entry in chosen},
            }
        ]

    cache = tmp_path / "cache"
    stats_cache.collect_cached_stats(root, cache, {}, entries, [], collect)
    assert _walked_lists(walked) == [dependencies]
    walked.clear()
    stats_cache.collect_cached_stats(root, cache, {}, entries, [], collect)
    assert calls == [entries]
    assert _walked_lists(walked) == [dependencies]


@pytest.mark.feature("mutation-stats-shared-closure")
def test_distinct_dependency_lists_in_one_collection_keep_their_own_closures(tmp_path: Path) -> None:
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    _repository(
        root,
        {
            "scripts/first.py": "value = 1\n",
            "scripts/second.py": "value = 1\n",
            "tests/test_first.py": "def test_first():\n    pass\n",
            "tests/test_second.py": "def test_second():\n    pass\n",
        },
    )
    first, second = "tests/test_first.py::test_first", "tests/test_second.py::test_second"
    calls: list[list[str]] = []

    def collect(chosen: list[str]) -> list[dict[str, object]]:
        calls.append(chosen)
        covered = {
            first: ["scripts/first.py", "tests/test_first.py"],
            second: ["scripts/second.py", "tests/test_second.py"],
        }
        return [
            {
                "status": 0,
                "tests": {},
                "durations": dict.fromkeys(chosen, 1),
                "cpu": 1,
                "dependencies": {entry: covered[entry] for entry in chosen},
            }
        ]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, [first, second], [], collect)
    (root / "scripts/second.py").write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, [first, second], [], collect)
    (root / "scripts/first.py").write_text("value = 2\n")
    collect_cached_stats(root, cache, {}, [first, second], [], collect)
    assert calls == [[first, second], [second], [first]]


@pytest.mark.feature("mutation-stats-shared-closure")
def test_the_discovery_digest_of_one_input_set_is_computed_once(tmp_path: Path) -> None:
    from scripts.ci_mutation.stats_cache import StatsInputs

    root = tmp_path / "repo"
    _repository(
        root,
        {
            "scripts/scan.py": "from pathlib import Path\n\n\ndef names():\n    return sorted(Path('data').glob('*.json'))\n",
            "scripts/plain.py": "value = 1\n",
            "data/a.json": "{}\n",
        },
    )
    reads: Counter[str] = Counter()

    class Counted(dict[str, set[str]]):
        def __getitem__(self, key: str) -> set[str]:
            reads[key] += 1
            return super().__getitem__(key)

    resources = {"data/": {"data/a.json"}, "data/a.json": {"data/a.json"}, "a.json": {"data/a.json"}}
    inputs = StatsInputs(root, {}, resources, {}, discovered=Counted())
    shared = {"scripts/scan.py": "same", "scripts/plain.py": "same"}
    digest = inputs.discovery(shared)
    assert inputs.discovery(dict(reversed(shared.items()))) == digest
    assert reads == {"scripts/scan.py": 1, "scripts/plain.py": 1}
    assert inputs.discovery({"scripts/plain.py": "same"}) != digest
    assert reads == {"scripts/scan.py": 1, "scripts/plain.py": 2}


@pytest.mark.feature("mutation-stats-lookup-digests")
def test_one_stats_lookup_hashes_each_input_file_once_across_its_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    from scripts.ci_mutation import stats_dependencies
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    files = [f"tests/test_{index}.py" for index in range(3)]
    sources = {
        "scripts/shared.py": "value = 1\n",
        **{
            path: f"from scripts.shared import value\n\n\ndef test_{index}():\n    assert value\n"
            for index, path in enumerate(files)
        },
    }
    _repository(root, sources)
    nodes = {path: f"{path}::test_{index}" for index, path in enumerate(files)}
    hashed: Counter[bytes] = Counter()
    sha256 = hashlib.sha256

    def counted(data: bytes) -> "hashlib._Hash":
        hashed[data] += 1
        return sha256(data)

    monkeypatch.setattr(stats_dependencies, "hashlib", SimpleNamespace(**{**vars(hashlib), "sha256": counted}))
    calls: list[list[str]] = []

    def collect(chosen: list[str]) -> list[dict[str, object]]:
        calls.append(chosen)
        return [{"status": 0, "tests": {}, "durations": {nodes[path]: 1 for path in chosen}, "cpu": 1}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, files, [], collect)
    hashed.clear()
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files]
    assert {path: hashed[text.encode()] for path, text in sources.items()} == dict.fromkeys(sources, 1)
    (root / "scripts/shared.py").write_text("value = 2\n")
    hashed.clear()
    collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == [files, files]
    assert hashed[b"value = 2\n"] == 1


def test_conftest_candidates_name_every_parent_folder_conftest_nearest_first() -> None:
    from scripts.ci_mutation.stats_cache import conftest_candidates

    assert conftest_candidates("tests/swarm/test_cli.py") == (
        "tests/swarm/conftest.py",
        "tests/conftest.py",
        "conftest.py",
    )
    assert conftest_candidates("conftest.py") == ("conftest.py",)


def test_selected_test_ids_read_the_collected_stats_once_not_once_per_id(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    reads = []

    class Durations(dict):
        def items(self):
            reads.append("durations")
            return super().items()

    class Tests(list):
        def __iter__(self):
            reads.append("tests")
            return super().__iter__()

    root = tmp_path / "repo"
    count = 50
    _repository(root, {"tests/test_a.py": "".join(f"def test_{index}():\n    pass\n" for index in range(count))})
    entries = [f"tests/test_a.py::test_{index}" for index in range(count)]
    calls = []

    def collect(chosen):
        calls.append(chosen)
        tests = {"m.x_f": Tests(f"{entry}[1]" for entry in chosen), "m.x_g": Tests()}
        return [{"status": 0, "tests": tests, "durations": Durations({f"{entry}[1]": 1 for entry in chosen}), "cpu": 2}]

    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, entries, [], collect)
    assert reads.count("durations") <= 2
    assert reads.count("tests") <= 4
    warm = collect_cached_stats(root, cache, {}, entries, [], collect)
    assert calls == [entries]
    assert sorted(warm, key=str) == sorted(
        [
            {
                "status": 0,
                "tests": {"m.x_f": [f"{entry}[1]"], "m.x_g": []},
                "durations": {f"{entry}[1]": 1},
                "cpu": 0.04,
            }
            for entry in entries
        ],
        key=str,
    )


def test_selected_test_ids_keep_durations_recorded_with_an_at_suffix():
    from scripts.ci_mutation.stats_cache import fresh_by_path

    entries = ["tests/test_a.py::test_x", "tests/test_a.py::test_y[1]", "tests/test_a.py"]
    fresh = [
        {
            "cpu": 1,
            "tests": {
                "m.x_f": ["tests/test_a.py::test_x@1", "tests/test_a.py::test_y[1]@2", "tests/test_b.py::test_z"]
            },
            "dependencies": {"tests/test_a.py::test_x@1": ["m.py"], "tests/test_b.py::test_z": ["m.py"]},
            "durations": {
                "tests/test_a.py::test_x@1": 1,
                "tests/test_a.py::test_x@a@b": 3,
                "tests/test_a.py::test_y[1]@2": 2,
            },
        }
    ]
    sliced = fresh_by_path(entries, fresh)
    assert sliced["tests/test_a.py::test_x"][0]["durations"] == {
        "tests/test_a.py::test_x@1": 1,
        "tests/test_a.py::test_x@a@b": 3,
    }
    assert sliced["tests/test_a.py::test_y[1]"][0]["tests"] == {"m.x_f": ["tests/test_a.py::test_y[1]@2"]}
    assert sliced["tests/test_a.py"][0]["durations"] == fresh[0]["durations"]
    assert sliced["tests/test_a.py"][0]["dependencies"] == {"tests/test_a.py::test_x@1": ["m.py"]}


def test_cap_keeps_the_newest_records_up_to_the_byte_limit_and_drops_the_rest(tmp_path, monkeypatch):
    import os

    from scripts.ci_mutation import stats_dependencies

    monkeypatch.setattr(stats_dependencies, "CACHE_BYTES", 30)
    cache = tmp_path / "cache"
    cache.mkdir()
    for age, name in reversed(list(enumerate(["new", "middle", "old", "older"]))):
        path = cache / f"{name}.json"
        path.write_text("x" * 10)
        os.utime(path, (1000 - age, 1000 - age))
    (cache / "new.index").write_text("x" * 100)
    assert stats_dependencies.cap(cache) == 1
    assert sorted(path.name for path in cache.iterdir()) == ["middle.json", "new.index", "new.json", "old.json"]


def test_prune_caps_the_cache_before_reading_its_records(tmp_path, monkeypatch):
    import os

    from scripts.ci_mutation import stats_dependencies

    monkeypatch.setattr(stats_dependencies, "CACHE_BYTES", 10)
    cache = tmp_path / "cache"
    cache.mkdir()
    for age, name in enumerate(["new", "old"]):
        path = cache / f"{name}.json"
        path.write_text('{"a": 1}')
        os.utime(path, (1000 - age, 1000 - age))
    read = []
    monkeypatch.setattr(stats_dependencies.StoredStats, "read", staticmethod(read.append))
    stats_dependencies.StoredStats(cache, cache, {}, {}).prune()
    assert read == [cache / "new.json"]
    assert [path.name for path in cache.iterdir()] == ["new.json"]


def test_collected_stats_are_capped_before_the_cache_is_saved(tmp_path, monkeypatch):
    from scripts.ci_mutation import stats_dependencies
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    _repository(root, {"module.py": "value = 1\n", "tests/test_module.py": "def test_one():\n    pass\n"})

    def collect(chosen):
        return [{"status": 0, "tests": {}, "durations": {f"{path}::test_one": 1 for path in chosen}, "cpu": 1}]

    monkeypatch.setattr(stats_dependencies, "CACHE_BYTES", 1)
    cache = tmp_path / "cache"
    collect_cached_stats(root, cache, {}, ["tests/test_module.py"], [], collect)
    assert list(cache.glob("*.index"))
    assert not list(cache.glob("*.json"))


def test_merged_stats_presence_check_handles_missing_empty_and_nested_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text())
    check = next(
        step["run"]
        for step in workflow["jobs"]["mutation-shard"]["steps"]
        if step.get("name") == "Check for merged test stats"
    )
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    for case in ("missing", "empty", "directories", "empty_file", "populated_file"):
        root = tmp_path / case
        root.mkdir()
        if case != "missing":
            cache = root / ".mutation-stats-cache" / "nested"
            cache.mkdir(parents=True)
            if case in {"empty_file", "populated_file"}:
                (cache / "stats.json").write_text("{}" if case == "populated_file" else "")
        output.write_text("")
        subprocess.run(["bash", "-e", "-c", check], cwd=root, check=True, timeout=2)
        assert output.read_text() == ("found=true\n" if case in {"empty_file", "populated_file"} else "")
