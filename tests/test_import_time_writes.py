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
ALLOWED = {"ledger_guard.py": [50, 51, 53, 55], "redis_key_guard.py": [14]}
MUTATORS = frozenset({"update", "append", "extend", "insert", "add", "setdefault", "__setitem__"})


def _main_guard(node: ast.If) -> bool:
    """Whether the block is an `if __name__ == "__main__"` block."""
    return ast.unparse(node.test).replace("'", '"') == '__name__ == "__main__"'


def _run_on_import(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield every statement that runs when the module imports, class bodies and nested blocks included."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.If) and _main_guard(node):
            yield from _run_on_import(node.orelse)
            continue
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                yield from _run_on_import([child])
            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                yield from _run_on_import(child.body)


def _module_value(value: ast.expr, names: set[str]) -> bool:
    """Whether the expression is an imported name, an `import_module` or `__import__` call or a `sys.modules` item."""
    if isinstance(value, ast.Name):
        return value.id in names
    if isinstance(value, ast.Call):
        return ast.unparse(value.func).split(".")[-1] in {"import_module", "__import__"}
    return isinstance(value, ast.Subscript) and ast.unparse(value.value) == "sys.modules"


def _imported(statements: list[ast.stmt]) -> set[str]:
    """Names bound to an import, or to a module looked up or an alias of one, in statement order."""
    names: set[str] = set()
    for node in statements:
        if isinstance(node, ast.Import):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Assign) and _module_value(node.value, names):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _root(node: ast.expr) -> str | None:
    """The name an attribute or item expression starts from, reading through `vars` and `getattr` calls."""
    if not isinstance(node, (ast.Attribute, ast.Subscript)):
        return None
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in {"vars", "getattr"} and node.args):
                return None
            node = node.args[0]
        else:
            node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _copies(call: ast.Call, imported: set[str]) -> bool:
    """Whether a call's arguments read an attribute or item of an imported name."""
    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    return any(
        _root(node) in imported for argument in arguments for node in ast.walk(argument) if isinstance(node, ast.expr)
    )


def _targets(node: ast.stmt, imported: set[str]) -> Iterator[ast.expr]:
    """Every expression the statement stores into, deletes or sets, unpacked, and every receiver a mutating
    method call fills with an attribute read from an imported name."""
    call = node.value if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) else None
    pending: list[ast.expr]
    if isinstance(node, (ast.Assign, ast.Delete)):
        pending = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor)):
        pending = [node.target]
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        pending = [item.optional_vars for item in node.items if item.optional_vars]
    elif call and isinstance(call.func, ast.Name) and call.func.id in {"setattr", "delattr"} and call.args:
        pending = [ast.Attribute(call.args[0], "_", ast.Store())]
    elif call and isinstance(call.func, ast.Attribute) and call.func.attr in MUTATORS and _copies(call, imported):
        pending = [call.func]
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
    """Lines where code run at import stores into, deletes from, sets or fills an imported name's attribute."""
    statements = list(_run_on_import(ast.parse(source).body))
    imported = _imported(statements)
    return sorted(
        {node.lineno for node in statements for target in _targets(node, imported) if _root(target) in imported}
    )


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
        ("from scripts import b\n[b.LIMIT] = [3]\n", [2]),
        ("from scripts import b as c\nc.LIMIT = 3\n", [2]),
        ("from scripts import b\nfor b.LIMIT in range(3):\n    pass\n", [2]),
        ("from scripts import b\nwith open('x') as b.HANDLE:\n    pass\n", [2]),
        ("from scripts import b\nvars(b)['LIMIT'] = 3\n", [2]),
        ("from scripts import b\ngetattr(b, 'TABLE').x = 3\n", [2]),
        ("import importlib\nm = importlib.import_module('scripts.b')\nm.LIMIT = 3\n", [3]),
        ("m = __import__('scripts.b')\nm.LIMIT = 3\n", [2]),
        ("import sys\nm = sys.modules['scripts.b']\nm.LIMIT = 3\n", [3]),
        ("from scripts import b\nc = b\nc.LIMIT = 3\n", [3]),
        ("from hooks import a\nfrom scripts import b\nb.REGISTRY.update(a.TABLE)\n", [3]),
        ("from hooks import a\nfrom scripts import b\nvars(b).update(table=a.TABLE)\n", [3]),
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
        "import sys\nfrom pathlib import Path\nSCRIPTS = Path(__file__).parent\nsys.path.insert(0, str(SCRIPTS))\n",
        "import os\nos.environ.pop('X', None)\n",
        "from scripts import b\nb.helper(1)\nb.TABLE.update({'k': 1})\nb.TABLE.get(b.KEY)\n",
        "from scripts import b\nm = make()\nm.LIMIT = 3\nm2 = b.LIMIT\nm2.x = 3\n",
        "from scripts import b\nwith open('x') as handle:\n    pass\nfor item in b.ITEMS:\n    pass\n",
        "from scripts import b\nvars()['LIMIT'] = 3\nfunc(b).x = 3\n",
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
    assert offenders == ALLOWED
