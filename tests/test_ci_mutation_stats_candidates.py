import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from scripts.ci_mutation import stats_candidates


def select(tmp_path: Path, source: str, *, narrow: bool = True) -> dict[str, list[str]]:
    (tmp_path / "app.py").write_text("def target(value):\n    return value + 1\n")
    (tmp_path / "test_sample.py").write_text("import pytest\nfrom app import target\n" + source)
    (tmp_path / "conftest.py").write_text(
        "import json\nfrom pathlib import Path\n"
        f"from {stats_candidates.__name__} import candidate_tests\n"
        "result = {}\n"
        "def pytest_collection_modifyitems(config, items):\n"
        "    result['original'] = [item.name for item in items]\n"
        + ("    items[:] = candidate_tests(items, Path.cwd(), {'app.py': [2]})\n" if narrow else "")
        + "    result['selected'] = [item.name for item in items]\n"
        "def pytest_sessionfinish(session):\n"
        "    Path('selection.json').write_text(json.dumps(result))\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "test_sample.py", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((tmp_path / "selection.json").read_text())


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
    ],
)
def test_statistics_candidates_preserve_indirect_reach_and_order(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    result = select(tmp_path, source)
    assert result["selected"] == expected


@pytest.mark.feature("mutation-stats-trace-candidates")
def test_statistics_candidates_retain_items_without_source(tmp_path: Path) -> None:
    item = cast(pytest.Item, object())
    assert stats_candidates.candidate_tests([item], tmp_path, {"app.py": [2]}) == [item]
