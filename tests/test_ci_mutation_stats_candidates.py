import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from scripts.ci_mutation import stats_candidates

pytest_plugins = ["pytester"]


def select(pytester: pytest.Pytester, source: str, *, plugin: str = "") -> dict[str, list[str]]:
    root = pytester.path
    pytester.makepyfile(app="def target(value):\n    return value + 1\ndef other(value):\n    return value\n")
    pytester.makeini("[pytest]\nenable_assertion_pass_hook = true\n")
    pytester.makepyfile(test_sample="import pytest\nfrom app import target\n" + source)
    pytester.makeconftest(
        "import json\nfrom pathlib import Path\n"
        f"from {stats_candidates.__name__} import candidate_tests\n"
        "result = {}\n"
        "def pytest_collection_modifyitems(config, items):\n"
        "    result['original'] = [item.name for item in items]\n"
        "    items[:] = candidate_tests(items, Path.cwd(), {'app.py': [2]})\n"
        "    result['selected'] = [item.name for item in items]\n"
        "def pytest_sessionfinish(session):\n"
        "    Path('selection.json').write_text(json.dumps(result))\n" + plugin
    )
    result = pytester.runpytest_inprocess("test_sample.py", "-q", "-p", "no:cacheprovider")
    assert result.ret == pytest.ExitCode.OK, result.stdout.str()
    return json.loads((root / "selection.json").read_text())


@pytest.mark.feature("mutation-stats-trace-candidates")
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "def test_unrelated():\n    assert True\ndef test_reaches():\n    assert target(1) == 2\n",
            ["test_reaches"],
        ),
        (
            "from app import target as alias\n"
            "def helper():\n    return alias(1)\n"
            "def test_unrelated():\n    assert True\n"
            "@pytest.mark.parametrize('value', [1, 2])\n"
            "def test_reaches(value):\n    assert helper() == 2\n",
            ["test_reaches[1]", "test_reaches[2]"],
        ),
        (
            "@pytest.fixture(scope='session')\n"
            "def shared():\n    return target(1)\n"
            "@pytest.fixture\n"
            "def indirect(shared):\n    return shared\n"
            "def test_first(indirect):\n    assert indirect == 2\n"
            "def test_unrelated():\n    assert True\n"
            "def test_second(indirect):\n    assert indirect == 2\n",
            ["test_first", "test_second"],
        ),
        (
            "class TestCase:\n"
            "    def setup_method(self):\n        self.value = target(1)\n"
            "    def test_first(self):\n        assert self.value == 2\n"
            "    def test_second(self):\n        assert self.value == 2\n",
            ["test_first", "test_second"],
        ),
        (
            "@pytest.fixture(autouse=True, params=[1, 2])\n"
            "def automatic(request):\n    assert target(request.param) > 1\n"
            "def test_first():\n    assert True\n"
            "def test_second():\n    assert True\n",
            ["test_first[1]", "test_first[2]", "test_second[1]", "test_second[2]"],
        ),
        (
            "assert target(1) == 2\ndef test_first():\n    assert True\ndef test_second():\n    assert True\n",
            ["test_first", "test_second"],
        ),
        (
            "import importlib\n"
            "def test_unknown():\n"
            "    name = ''.join(['ap', 'p'])\n"
            "    assert importlib.import_module(name).target(1) == 2\n",
            ["test_unknown"],
        ),
        (
            "@pytest.fixture\n"
            "def hidden():\n    return target(1)\n"
            "def test_dynamic_fixture(request):\n"
            "    name = ''.join(['hid', 'den'])\n"
            "    assert request.getfixturevalue(name) == 2\n",
            ["test_dynamic_fixture"],
        ),
        (
            "from pathlib import Path\n"
            "from importlib import import_module as load\n"
            "Path('boot.py').write_text('from app import target\\ntarget(1)\\n')\n"
            "def test_loader_alias():\n"
            "    name = ''.join(['bo', 'ot'])\n"
            "    load(name)\n",
            ["test_loader_alias"],
        ),
        (
            "@pytest.fixture\n"
            "def hidden():\n    return target(1)\n"
            "def test_fixture_alias(request):\n"
            "    resolve = request.getfixturevalue\n"
            "    name = ''.join(['hid', 'den'])\n"
            "    assert resolve(name) == 2\n",
            ["test_fixture_alias"],
        ),
        (
            "from pathlib import Path\n"
            "Path('boot.py').write_text(\"from app import target\\ndef __getattr__(name):\\n    if name == 'missing':\\n        return target(1)\\n    raise AttributeError(name)\\n\")\n"
            "from boot import missing\n"
            "def test_dynamic_export():\n    assert missing == 2\n",
            ["test_dynamic_export"],
        ),
        (
            "def setup_module():\n    assert target(1) == 2\ndef test_module_setup():\n    assert True\n",
            ["test_module_setup"],
        ),
        (
            "def teardown_function():\n    assert target(1) == 2\ndef test_function_teardown():\n    assert True\n",
            ["test_function_teardown"],
        ),
        (
            "import app\n"
            "def test_reassigned():\n"
            "    name = 'target'\n"
            "    assert getattr(app, name)(1) == 2\n"
            "    name = 'other'\n",
            ["test_reassigned"],
        ),
        (
            "import app\n"
            "def test_conditional():\n"
            "    if True:\n"
            "        name = 'target'\n"
            "    else:\n"
            "        name = 'other'\n"
            "    assert getattr(app, name)(1) == 2\n",
            ["test_conditional"],
        ),
        (
            "from pathlib import Path\n"
            "Path('pkg').mkdir()\n"
            "Path('pkg/__init__.py').write_text('')\n"
            "Path('pkg/boot.py').write_text('from app import target\\ntarget(1)\\n')\n"
            "def test_child_import():\n"
            "    import pkg.boot\n",
            ["test_child_import"],
        ),
        (
            "class Helper:\n"
            "    def __init__(self):\n"
            "        target(1)\n"
            "def test_constructor():\n"
            "    Helper()\n"
            "def test_unrelated():\n"
            "    assert True\n",
            ["test_constructor"],
        ),
        (
            "class Helper:\n"
            "    def __enter__(self):\n        return target(1)\n"
            "    def __exit__(self, *args):\n        return False\n"
            "def test_context():\n    with Helper() as value:\n        assert value == 2\n",
            ["test_context"],
        ),
        (
            "class Helper:\n"
            "    @property\n"
            "    def value(self):\n        return target(1)\n"
            "@pytest.fixture\n"
            "def helper():\n    return Helper()\n"
            "def test_property(helper):\n    assert helper.value == 2\n",
            ["test_property"],
        ),
        (
            "from pathlib import Path\n"
            "def test_file_unrelated():\n"
            "    with Path('runs.txt').open('a') as stream:\n"
            "        stream.write('run\\n')\n"
            "def test_reaches():\n"
            "    assert target(1) == 2\n",
            ["test_reaches"],
        ),
    ],
    ids=[
        "direct",
        "alias-helper-parameters",
        "shared-fixtures",
        "class-setup",
        "autouse",
        "import",
        "dynamic-import",
        "dynamic-fixture",
        "loader-alias",
        "fixture-alias",
        "dynamic-export",
        "module-setup",
        "function-teardown",
        "later-reassignment",
        "conditional-reassignment",
        "child-module-initialization",
        "constructor-reach",
        "context-manager-reach",
        "property-reach",
        "unrelated-file-methods",
    ],
)
def test_statistics_candidates_preserve_indirect_reach_and_order(
    pytester: pytest.Pytester, source: str, expected: list[str]
) -> None:
    result = select(pytester, source)
    assert result["selected"] == expected


@pytest.mark.feature("mutation-stats-trace-candidates")
@pytest.mark.parametrize(
    "hook",
    [
        "def pytest_pyfunc_call(pyfuncitem):\n    from app import target\n    target(1)\n",
        "def pytest_assertion_pass(item, lineno, orig, expl):\n    from app import target\n    target(1)\n",
        "class Recorder:\n"
        "    def __init__(self):\n        self.calls = {}\n"
        "    def pytest_runtest_logreport(self, report):\n"
        "        self.calls[report.nodeid] = report.passed or self.calls.get(report.nodeid, False)\n"
        "        self.record()\n"
        "    def record(self):\n        from app import target\n        target(1)\n"
        "def pytest_configure(config):\n    config.pluginmanager.register(Recorder())\n",
        "class Recorder:\n"
        "    def pytest_runtest_logreport(self, report):\n        self.record()\n"
        "    def record(self):\n        from app import target\n        target(1)\n"
        "    def unused(self):\n        self.record = print\n"
        "def pytest_configure(config):\n    config.pluginmanager.register(Recorder())\n",
        "class Base:\n"
        "    def pytest_runtest_logreport(self, report):\n        self.record()\n"
        "    def record(self):\n        pass\n"
        "class Recorder(Base):\n"
        "    def record(self):\n        from app import target\n        target(1)\n"
        "def pytest_configure(config):\n    config.pluginmanager.register(Recorder())\n",
    ],
    ids=["function-call", "assertion-pass", "bound-method", "possible-field-replacement", "inherited-hook"],
)
def test_statistics_candidates_keep_runtime_hook_reach(pytester: pytest.Pytester, hook: str) -> None:
    result = select(
        pytester,
        "def test_first():\n    assert True\ndef test_second():\n    assert True\n",
        plugin=hook,
    )
    assert result["selected"] == ["test_first", "test_second"]


@pytest.mark.feature("mutation-stats-trace-candidates")
def test_statistics_candidates_ignore_unrelated_bound_hook_state(pytester: pytest.Pytester) -> None:
    result = select(
        pytester,
        "def test_unrelated():\n    assert True\ndef test_reaches():\n    assert target(1) == 2\n",
        plugin="class Recorder:\n"
        "    def __init__(self):\n        self.calls = {}\n"
        "    def pytest_runtest_logreport(self, report):\n"
        "        self.calls[report.nodeid] = report.passed or self.calls.get(report.nodeid, False)\n"
        "def pytest_configure(config):\n    config.pluginmanager.register(Recorder())\n",
    )
    assert result["selected"] == ["test_reaches"]


@pytest.mark.feature("mutation-stats-trace-candidates")
def test_stats_bucket_registers_candidate_selection_for_its_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.ci_mutation.selection import collect_shard_stats

    monkeypatch.setenv("MUTANT_UNDER_TEST", os.environ.get("MUTANT_UNDER_TEST", ""))
    monkeypatch.chdir(tmp_path)
    scope = tmp_path / "changed-lines.json"
    scope.write_text("{}")
    arguments: list[str] = []

    class Runner:
        _pytest_add_cli_args: list[str] = []

        def run_stats(self, *, tests: list[str]) -> int:
            arguments.extend(self._pytest_add_cli_args)
            return 0

    engine = SimpleNamespace(tests_by_mangled_function_name={}, duration_by_test={})
    collect_shard_stats(SimpleNamespace(mutmut=engine), Runner(), ["test_sample.py"], tmp_path / "out.json", "base")
    index = arguments.index("scripts.ci_mutation.stats_candidates")
    assert arguments[index - 1 : index + 2] == [
        "-p",
        "scripts.ci_mutation.stats_candidates",
        f"--mutation-stats-scope={scope}",
    ]


@pytest.mark.feature("mutation-stats-trace-candidates")
def test_statistics_candidates_retain_items_without_source(tmp_path: Path) -> None:
    item = cast(pytest.Item, object())
    assert stats_candidates.candidate_tests([item], tmp_path, {"app.py": [2]}) == [item]
