import ast
import importlib
import inspect

import pytest

pytestmark = pytest.mark.unit

MOVED = ("scripts.swarm.metrics", "scripts.swarm.lease")


def _imported(module):
    tree = ast.parse(inspect.getsource(importlib.import_module(module)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


@pytest.mark.parametrize("module", MOVED)
def test_a_module_on_the_storage_protocol_imports_no_redis_client(module):
    assert [name for name in _imported(module) if name.split(".")[0] == "redis"] == []
