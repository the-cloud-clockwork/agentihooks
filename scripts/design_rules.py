import argparse
import ast
import builtins
import re
from dataclasses import dataclass
from pathlib import Path

from scripts.size_limits import GradeError, files

UNGRADED = ("tests/", ".semgrep/")
ROOTS = frozenset(
    {"ABC", "Protocol", "NamedTuple", "TypedDict", "Generic", "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
)
PLAIN_DICTS = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})
CONFIG_NAME = re.compile(r"(^|_)(config|conf|cfg|settings|options|opts|params)$")
RECEIVERS = frozenset({"self", "cls"})


@dataclass
class Module:
    name: str
    package: str
    classes: dict[str, ast.ClassDef]
    imports: dict[str, tuple[str, str | None]]


@dataclass(frozen=True)
class Unit:
    signature: tuple[str, ...]
    node: ast.AST
    public: bool


def _module_name(rel: str) -> tuple[str, str]:
    parts = rel.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
        return ".".join(parts), ".".join(parts)
    return ".".join(parts), ".".join(parts[:-1])


def _imported(module: Module, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module
    package = module.package.split(".") if module.package else []
    package = package[: len(package) - node.level + 1]
    return ".".join([*package, *([node.module] if node.module else [])])


def _index_module(module: Module, tree: ast.Module) -> None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            module.classes[node.name] = node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.name if alias.asname else alias.name.split(".")[0]
                module.imports[alias.asname or bound] = (bound, None)
        elif isinstance(node, ast.ImportFrom):
            source = _imported(module, node)
            for alias in node.names:
                module.imports[alias.asname or alias.name] = (source, alias.name)


class Index:
    def __init__(self, tree: Path, rels: list[str]) -> None:
        self.tree = tree
        self.paths = {_module_name(rel)[0]: rel for rel in rels}
        self.loaded: dict[str, tuple[ast.Module, Module]] = {}

    def load(self, rel: str) -> tuple[ast.Module, Module]:
        if rel not in self.loaded:
            name, package = _module_name(rel)
            syntax = ast.parse((self.tree / rel).read_text(), filename=rel)
            module = Module(name, package, {}, {})
            _index_module(module, syntax)
            self.loaded[rel] = (syntax, module)
        return self.loaded[rel]

    def get(self, name: str) -> Module | None:
        rel = self.paths.get(name)
        return None if rel is None else self.load(rel)[1]


def _lookup(index: Index, module: str, name: str, depth: int = 0) -> tuple[str, str] | None:
    found = index.get(module)
    if found is None or depth > 5:
        return None
    if name in found.classes:
        return module, name
    source = found.imports.get(name)
    if source is not None and source[1] is not None:
        return _lookup(index, source[0], source[1], depth + 1)
    return None


def resolve(index: Index, module: str, base: ast.expr) -> tuple[str, str] | None:
    if isinstance(base, ast.Subscript):
        return resolve(index, module, base.value)
    if isinstance(base, ast.Name):
        return _lookup(index, module, base.id)
    if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
        source = index.get(module).imports.get(base.value.id)
        if source is not None:
            return _lookup(index, source[0] if source[1] is None else f"{source[0]}.{source[1]}", base.attr)
    return None


def _terminal(expr: ast.expr) -> str:
    if isinstance(expr, ast.Subscript):
        return _terminal(expr.value)
    if isinstance(expr, ast.Call):
        return _terminal(expr.func)
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return ast.unparse(expr)


def _builtin_exception(name: str) -> bool:
    found = getattr(builtins, name, None)
    return isinstance(found, type) and issubclass(found, BaseException)


def _declared_free(node: ast.ClassDef) -> bool:
    if any(_terminal(decorator) == "dataclass" for decorator in node.decorator_list):
        return True
    if any(keyword.arg == "metaclass" and _terminal(keyword.value) == "ABCMeta" for keyword in node.keywords):
        return True
    methods = (child for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return any(_terminal(decorator) == "abstractmethod" for method in methods for decorator in method.decorator_list)


def shareable(index: Index, target: tuple[str, str], seen: frozenset = frozenset()) -> bool:
    if target in seen:
        return False
    node = index.get(target[0]).classes[target[1]]
    if _declared_free(node):
        return True
    for base in node.bases:
        name = _terminal(base)
        if name in ROOTS or _builtin_exception(name):
            return True
        project = resolve(index, target[0], base)
        if project is not None and shareable(index, project, seen | {target}):
            return True
    return False


def _signature(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.ClassDef):
        parts = [*node.decorator_list, *node.bases, *node.keywords]
    else:
        parts = [*node.decorator_list, node.args, *([node.returns] if node.returns else [])]
    return tuple(ast.dump(part) for part in parts)


def units(syntax: ast.Module, rel: str) -> dict[str, Unit]:
    found, seen = {}, {}

    def walk(node, prefix, public):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                continue
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, prefix, public)
                continue
            name = f"{prefix}{child.name}"
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name}#{seen[name]}"
            visible = public and not child.name.startswith("_")
            found[f"{rel}::{name}"] = Unit(_signature(child), child, visible)
            walk(child, f"{name}.", visible and isinstance(child, ast.ClassDef))

    walk(syntax, "", True)
    return found


def untyped(unit: Unit) -> list[str]:
    args = unit.node.args
    params = [*args.posonlyargs, *args.args]
    if params and params[0].arg in RECEIVERS:
        params = params[1:]
    params += [arg for arg in (args.vararg, *args.kwonlyargs, args.kwarg) if arg is not None]
    missing = [arg.arg for arg in params if arg.annotation is None]
    return missing + ([] if unit.node.returns else ["return"])


def plain_configs(unit: Unit) -> list[str]:
    args = unit.node.args
    found = []
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if arg.annotation is not None and _terminal(arg.annotation) in PLAIN_DICTS and CONFIG_NAME.search(arg.arg):
            found.append(arg.arg)
    return found + ([f"**{args.kwarg.arg}"] if args.kwarg else [])


def violations(index: Index, module: str, key: str, unit: Unit) -> list[str]:
    if isinstance(unit.node, ast.ClassDef):
        errors = []
        for base in unit.node.bases:
            project = resolve(index, module, base)
            if project is not None and not shareable(index, project):
                errors.append(
                    f"inheritance: {key} inherits the concrete project class {'.'.join(project)}; "
                    "inherit an abstract base or Protocol and compose the shared behaviour"
                )
        return errors
    if not unit.public:
        return []
    errors = []
    if missing := untyped(unit):
        errors.append(f"untyped: public {key} lacks type hints on {', '.join(missing)}")
    if bags := plain_configs(unit):
        errors.append(
            f"config: public {key} takes {', '.join(bags)} as a plain dict; take a typed configuration object"
        )
    return errors


def _graded(rel: str) -> bool:
    return not rel.startswith(UNGRADED)


def _file_errors(index: Index, rel: str, old: str | None) -> tuple[int, list[str]]:
    syntax, module = index.load(rel)
    before = {} if old is None else units(ast.parse(old, filename=rel), rel)
    changed, errors = 0, []
    for key, unit in units(syntax, rel).items():
        previous = before.get(key)
        if previous is None or previous.signature != unit.signature:
            changed += 1
            errors += violations(index, module.name, key, unit)
    return changed, errors


def grade(base: Path, head: Path) -> tuple[int, list[str]]:
    base, head = base.resolve(), head.resolve()
    rels = [path.relative_to(head).as_posix() for path in files(head) if path.suffix == ".py"]
    index = Index(head, rels)
    changed, errors = 0, []
    for rel in filter(_graded, rels):
        previous = base / rel
        old = previous.read_text() if previous.is_file() else None
        if old == (head / rel).read_text():
            continue
        count, found = _file_errors(index, rel, old)
        changed, errors = changed + count, errors + found
    return changed, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        changed, errors = grade(args.base, args.head)
    except (GradeError, SyntaxError, ValueError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    print(f"{changed} new or changed units graded on the design rules, {len(errors)} errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
