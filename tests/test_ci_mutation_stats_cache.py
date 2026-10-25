import subprocess
from pathlib import Path

import pytest


def test_unchanged_test_stats_are_reused_and_only_the_changed_file_is_collected(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

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


def test_collection_parts_reuse_stats_and_keep_the_new_head_identity(tmp_path, monkeypatch):
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

    def collect(engine, test_runner, buckets, work):
        calls.append(buckets)
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
        "runtime",
        "dependency",
        "package_version",
    ],
)
def test_incompatible_source_fixture_and_runtime_stats_are_recollected(tmp_path, monkeypatch, changed):
    import sys
    from importlib import metadata
    from types import SimpleNamespace

    from scripts.ci_mutation.stats_cache import collect_cached_stats

    root = tmp_path / "repo"
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
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
        ["git", "add", "scripts/sample.py", "tests/test_sample.py", "tests/conftest.py", "tests/test_shared.py"],
        cwd=root,
        check=True,
    )
    if changed == "imported_fixture_through_helper":
        subprocess.run(["git", "add", "tests/helper.py"], cwd=root, check=True)
    package = "agentihooks" if changed == "package_version" else "sample"
    monkeypatch.setattr(metadata, "distributions", lambda: [SimpleNamespace(metadata={"Name": package}, version="1")])
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
    elif changed == "runtime":
        monkeypatch.setattr(sys, "version", "changed")
    else:
        monkeypatch.setattr(
            metadata, "distributions", lambda: [SimpleNamespace(metadata={"Name": package}, version="2")]
        )
    collect_cached_stats(root, cache, changes, files, options, collect)
    assert calls == [files, files]


@pytest.mark.parametrize(
    ("package", "before", "after", "recollected"),
    [
        ("sample-project", "1.2.dev3+gabc", "1.2.dev4+gdef", False),
        ("sample-project", "1.2.dev3", "1.2.dev4", False),
        ("sample-project", "1.2+gabc", "1.2+gdef", False),
        ("sample-project", "1.2.dev3+gabc", "1.3.dev1+gdef", True),
        ("sample-project", "1.2", "1.3", True),
        ("dependency", "1.2+gabc", "1.2+gdef", True),
        ("dependency", "1.2.dev3", "1.2.dev4", True),
    ],
)
def test_the_project_build_number_alone_keeps_stats(tmp_path, monkeypatch, package, before, after, recollected):
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
        monkeypatch.setattr(metadata, "distributions", lambda distribution=distribution: [distribution])
        collect_cached_stats(root, cache, {}, files, [], collect)
    assert calls == ([files, files] if recollected else [files])


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
        subprocess.run(["git", "rm", "--", "other.py"], cwd=root, check=True)
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
    assert "Mutation stats cache: reused 0 test files; collect 2" in capsys.readouterr().out
    (root / files[1]).write_text("def test_sample():\n    assert True\n")
    warm = collect_cached_stats(root, cache, {}, files, [], collect)
    assert "Mutation stats cache: reused 1 test files; collect 1" in capsys.readouterr().out
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


def test_real_fixture_imports_do_not_invalidate_measured_unrelated_test_stats(tmp_path):
    from scripts.ci_mutation.stats_cache import collect_cached_stats

    source = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=source, text=True).split("\0")
    content = {path: (source / path).read_text() for path in tracked if path.endswith(".py")}
    content.update(
        {
            "scripts/cache_unrelated_probe.py": "value = 1\n",
            "scripts/cache_covered_probe.py": "value = 1\n",
            "tests/test_cache_measured_probe.py": "from scripts.cache_covered_probe import value\n",
        }
    )
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


@pytest.mark.parametrize("changed", ["module", "scope", "deleted", "stale"])
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
    else:
        next(folder.glob("*.json")).write_text('{"key":"stale","status":0,"inputs":{},"scope":{},"measured":true}')
    assert cache.load("tests/test_module.py") is None


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
