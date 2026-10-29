import ast
import importlib
import inspect

import pytest

pytestmark = pytest.mark.unit

MOVED = ("scripts.swarm.metrics", "scripts.swarm.metrics_fleet")


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


def _client_names(module):
    tree = ast.parse(inspect.getsource(importlib.import_module(module)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (
                alias.name
                for alias in node.names
                if alias.name.split(".")[0] == "redis" or alias.name == "hooks._redis"
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] == "redis" or node.module == "hooks._redis":
                yield node.module
            yield from (alias.name for alias in node.names if alias.name in ("redis_client", "get_redis", "_redis"))


@pytest.mark.parametrize("module", ("scripts.swarm.timer", "hooks.context.operator_words"))
def test_the_timer_and_operator_words_import_no_redis_client(module):
    assert list(_client_names(module)) == []
