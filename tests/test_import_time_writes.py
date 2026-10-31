"""Refuse test code that writes into an imported module or object while its own module imports.

The test dependency record links a file a module reads at import only to the tests whose imports reach that module.
A test module that copies one module's import time value into another at its own import hands that value to tests
no import connects, so the record misses the file for them.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TESTS = Path(__file__).resolve().parent
ALLOWED = frozenset({"ledger_guard.py", "redis_key_guard.py"})


def _main_guard(node: ast.stmt) -> bool:
    """Whether the statement is an `if __name__ == "__main__"` block."""
    return isinstance(node, ast.If) and ast.unparse(node.test).replace("'", '"') == '__name__ == "__main__"'


def _run_on_import(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield every statement that runs when the module imports, class bodies and nested blocks included."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _main_guard(node):
            yield from _run_on_import(node.orelse)
            continue
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                yield from _run_on_import([child])
            elif isinstance(child, (ast.excepthandler, ast.match_case)):
                yield from _run_on_import(child.body)


def _imported(statements: list[ast.stmt]) -> set[str]:
    """Names the import statements among the given statements bind."""
    names = set()
    for node in statements:
        if isinstance(node, ast.Import):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _root(node: ast.expr) -> str | None:
    """The name an attribute or item expression starts from, if it is a nested attribute or item of one."""
    if not isinstance(node, (ast.Attribute, ast.Subscript)):
        return None
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _targets(node: ast.stmt) -> Iterator[ast.expr]:
    """Every expression the statement stores into or deletes, unpacked, or the object `setattr` or `delattr` changes."""
    if isinstance(node, (ast.Assign, ast.Delete)):
        pending = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        pending = [node.target]
    elif (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in {"setattr", "delattr"}
        and node.value.args
    ):
        pending = [ast.Attribute(node.value.args[0], "_", ast.Store())]
    else:
        pending = []
    while pending:
        target = pending.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        else:
            yield target


def writes(source: str) -> list[int]:
    """Lines where code run at import stores into, deletes from or sets an attribute of an imported name."""
    statements = list(_run_on_import(ast.parse(source).body))
    imported = _imported(statements)
    return sorted({node.lineno for node in statements for target in _targets(node) if _root(target) in imported})


def _offenders() -> dict[str, list[int]]:
    """Every test tree file with an import time write, keyed by its path relative to the tests folder."""
    found = {}
    for path in sorted(TESTS.rglob("*.py")):
        lines = writes(path.read_text(encoding="utf-8"))
        if lines:
            found[path.relative_to(TESTS).as_posix()] = lines
    return found


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("from hooks import a\nfrom scripts import b\nb.LIMIT = a.LIMIT\n", [3]),
        ("import scripts.b\nimport hooks.a\nscripts.b.LIMIT = hooks.a.LIMIT\n", [3]),
        ("import scripts.b as other\nother.LIMIT = 3\n", [2]),
        ("from scripts import b\nfrom hooks import a\nsetattr(b, 'LIMIT', a.LIMIT)\n", [3]),
        ("from scripts import b\ndelattr(b, 'LIMIT')\n", [2]),
        ("from scripts import b\ndel b.LIMIT\n", [2]),
        ("import sys\nsys.modules['x'] = object()\n", [2]),
        ("from scripts import b\nb.TABLE['k'].x += 1\n", [2]),
        ("from scripts import b\nb.LIMIT: int = 3\n", [2]),
        ("from scripts import b\nx, (y, *b.REST) = 1, (2, 3)\n", [2]),
        ("from scripts import b\nclass T:\n    b.LIMIT = 3\n", [3]),
        ("from scripts import b\nif True:\n    b.LIMIT = 3\nelse:\n    b.LIMIT = 4\n", [3, 5]),
        ("try:\n    from scripts import b\nexcept ImportError:\n    b.LIMIT = 3\n", [4]),
        ("from scripts import b\nwith open('x'):\n    b.LIMIT = 3\n", [3]),
        ("from scripts import b\nif __name__ == '__main__':\n    pass\nelse:\n    b.LIMIT = 3\n", [5]),
        ("from scripts import b\nmatch 1:\n    case 1:\n        b.LIMIT = 3\n", [4]),
    ],
)
def test_writes_finds_import_time_stores_into_imported_names(source: str, lines: list[int]) -> None:
    assert writes(source) == lines


@pytest.mark.parametrize(
    "source",
    [
        "from scripts import b\ndef test_x():\n    b.LIMIT = 3\n",
        "from scripts import b\nasync def test_x():\n    setattr(b, 'LIMIT', 3)\n",
        "from scripts import b\nclass T:\n    def test_x(self):\n        b.LIMIT = 3\n",
        "from scripts import b\nif __name__ == '__main__':\n    b.LIMIT = 3\n",
        'from scripts import b\nif __name__ == "__main__":\n    b.LIMIT = 3\n',
        "from scripts import b\nbox = b.Box()\nbox.LIMIT = 3\nCASES = {}\nCASES['k'] = b.LIMIT\n",
        "def helper():\n    from scripts import b\nb = object()\n",
        "from scripts import b\nsetattr(box, 'LIMIT', b.LIMIT)\ngetattr(b, 'LIMIT')\nsetattr()\n",
        "from scripts import b\nb.helper().LIMIT\nf().x = 3\n",
        "from scripts import b\nLIMIT = b.LIMIT\nb2: int = 3\nb3 = b\n",
    ],
)
def test_writes_ignores_test_time_and_local_stores(source: str) -> None:
    assert writes(source) == []


def test_no_test_module_writes_into_an_imported_name_at_import() -> None:
    offenders = _offenders()
    assert {path: lines for path, lines in offenders.items() if path not in ALLOWED} == {}
    assert ALLOWED - offenders.keys() == set()
