import argparse
import ast
from collections.abc import Iterator
from pathlib import Path

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
CLOCKS = {"time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"}
BOUNDS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def qualified(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{qualified(node.value)}.{node.attr}"
    return ""


def statements(node: ast.AST) -> Iterator[ast.AST]:
    pending = [*ast.iter_child_nodes(node)][::-1]
    while pending:
        child = pending.pop()
        if not isinstance(child, (*FUNCTIONS, ast.ClassDef)):
            yield child
            pending.extend([*ast.iter_child_nodes(child)][::-1])


def aliases_for(tree: ast.Module, nodes: list[ast.AST]) -> dict[str, str]:
    aliases = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            aliases.update((name.asname or name.name, name.name) for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            aliases.update((name.asname or name.name, f"{node.module}.{name.name}") for name in node.names)
    for node in tree.body:
        if isinstance(node, ast.Assign) and (value := qualified(node.value)):
            aliases.update((target.id, value) for target in node.targets if isinstance(target, ast.Name))
    return aliases


def bindings_for(function: ast.AST) -> dict:
    bindings = {}
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and node.value is not None:
                    bindings.setdefault(target.id, []).append(node.value)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for index, item in enumerate(target.elts):
                        if isinstance(item, ast.Name):
                            bindings.setdefault(item.id, []).append(
                                ast.Subscript(value=node.value, slice=ast.Constant(index))
                            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and node.args
        ):
            bindings.setdefault(qualified(node.func.value), []).append(node.args[0])
        if isinstance(node, ast.comprehension):
            for target in ast.walk(node.target):
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, []).append(node.iter)
    return bindings


class Timing:
    def __init__(self, functions: dict, helpers: dict, aliases: dict) -> None:
        self.functions = dict(functions)
        self.functions.update((name, node) for name, (node, _) in helpers.items())
        self.aliases = aliases
        self.helper_aliases = {node: aliases for node, aliases in helpers.values()}
        self.bindings = {}

    def bindings_for(self, function: ast.AST) -> dict:
        if function not in self.bindings:
            self.bindings[function] = bindings_for(function)
        return self.bindings[function]

    def arguments(self, call: ast.Call, helper: ast.AST, caller: ast.AST, seen: frozenset) -> frozenset:
        positional = [*helper.args.posonlyargs, *helper.args.args]
        arguments = {parameter.arg: value for parameter, value in zip(positional, call.args)}
        arguments.update((keyword.arg, keyword.value) for keyword in call.keywords if keyword.arg)
        return frozenset((helper, name, self.value(value, caller, seen)) for name, value in arguments.items())

    def value(self, node: ast.AST, function: ast.AST, seen: frozenset = frozenset()) -> int:
        key = (node, function)
        if key in seen:
            return 0
        seen = seen | {key}
        if isinstance(node, ast.Name):
            arguments = [
                item[2]
                for item in seen
                if len(item) == 3 and item[0] is function and item[1] == node.id and isinstance(item[2], int)
            ]
            if arguments:
                return max(arguments)
            return max(
                (self.value(value, function, seen) for value in self.bindings_for(function).get(node.id, [])), default=0
            )
        if isinstance(node, ast.BinOp):
            left, right = self.value(node.left, function, seen), self.value(node.right, function, seen)
            return 2 if isinstance(node.op, ast.Sub) and left == right == 1 else max(left, right)
        if isinstance(node, ast.Call):
            name = qualified(node.func)
            first, _, rest = name.partition(".")
            aliases = self.helper_aliases.get(function, self.aliases)
            name = aliases.get(first, first) + (f".{rest}" if rest else "")
            if any(name.endswith(f"time.{clock}") for clock in CLOCKS):
                return 1
            if qualified(node.func) in self.bindings_for(function):
                binding = (function, qualified(node.func), "call")
                if binding in seen:
                    return 0
                seen = seen | {binding}
                return max(
                    (
                        self.value(ast.Call(func=value, args=[], keywords=[]), function, seen)
                        for value in self.bindings_for(function)[qualified(node.func)]
                        if not isinstance(value, ast.Call)
                    ),
                    default=0,
                )
            helper = self.functions.get(qualified(node.func))
            if helper is not None:
                seen = seen | self.arguments(node, helper, function, seen)
                return max(
                    (
                        self.value(item.value, helper, seen)
                        for item in statements(helper)
                        if isinstance(item, ast.Return)
                    ),
                    default=0,
                )
            return max((self.value(arg, function, seen) for arg in node.args), default=0)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            return self.indexed(node.value, node.slice.value, function, seen)
        return max((self.value(child, function, seen) for child in ast.iter_child_nodes(node)), default=0)

    def indexed(self, node: ast.AST, index: object, function: ast.AST, seen: frozenset) -> int:
        key = (node, function, index)
        if key in seen:
            return 0
        seen = seen | {key}
        if isinstance(node, ast.Name):
            return max(
                (self.indexed(value, index, function, seen) for value in self.bindings_for(function).get(node.id, [])),
                default=0,
            )
        if isinstance(node, ast.Call) and (helper := self.functions.get(qualified(node.func))):
            seen = seen | self.arguments(node, helper, function, seen)
            return max(
                (
                    self.indexed(item.value, index, helper, seen)
                    for item in statements(helper)
                    if isinstance(item, ast.Return)
                ),
                default=0,
            )
        if (
            isinstance(node, (ast.Tuple, ast.List))
            and isinstance(index, int)
            and -len(node.elts) <= index < len(node.elts)
        ):
            return self.value(node.elts[index], function, seen)
        return self.value(node, function, seen)

    def bounds(self, function: ast.AST) -> list[int]:
        return [
            node.lineno
            for node in statements(function)
            if isinstance(node, (ast.Assert, ast.Call))
            and (
                isinstance(node, ast.Call)
                and qualified(node.func).rsplit(".", 1)[-1]
                in {"assertLess", "assertLessEqual", "assertGreater", "assertGreaterEqual"}
                and any(self.value(arg, function) == 2 for arg in node.args)
                or isinstance(node, ast.Assert)
                and any(
                    isinstance(compare, ast.Compare)
                    and any(isinstance(op, BOUNDS) for op in compare.ops)
                    and any(self.value(value, function) == 2 for value in [compare.left, *compare.comparators])
                    for compare in ast.walk(node.test)
                )
            )
        ]


def marked(nodes: list[ast.AST]) -> bool:
    return any(
        qualified(node.func if isinstance(node, ast.Call) else node).endswith("mark.wall_clock") for node in nodes
    )


def indexed(tree: ast.Module) -> tuple:
    nodes = list(ast.walk(tree))
    imports = [node for node in nodes if isinstance(node, ast.ImportFrom) and str(node.module).startswith("tests.")]
    functions = {item.name: item for item in tree.body if isinstance(item, FUNCTIONS)}
    every = {node.name: node for node in nodes if isinstance(node, FUNCTIONS)}
    return imports, functions, aliases_for(tree, nodes), every


def helper_module(path: Path, modules: dict) -> tuple:
    if path not in modules:
        modules[path] = indexed(ast.parse(path.read_text(), filename=str(path)))[:3]
    return modules[path]


def imported_helpers(imports: list, root: Path, modules: dict, seen: frozenset = frozenset()) -> dict:
    helpers = {}
    for node in imports:
        path = root / (node.module.replace(".", "/") + ".py")
        if path in seen or not path.is_file():
            continue
        nested, functions, aliases = helper_module(path, modules)
        helpers.update(imported_helpers(nested, root, modules, seen | {path}))
        helpers.update(
            (name.asname or name.name, (functions[name.name], aliases)) for name in node.names if name.name in functions
        )
    return helpers


def discover(tree: ast.Module, root: Path, modules: dict) -> list[tuple[str, int, bool]]:
    imports, _, aliases, every = indexed(tree)
    timing = Timing(every, imported_helpers(imports, root, modules), aliases)

    def walk(scope, prefix="", inherited=False):
        assignments = [node for node in scope.body if isinstance(node, ast.Assign)]
        inherited = inherited or marked(
            [
                item
                for node in assignments
                if any(qualified(target) == "pytestmark" for target in node.targets)
                for item in ast.walk(node.value)
            ]
        )
        for node in scope.body:
            if isinstance(node, ast.ClassDef):
                yield from walk(node, f"{prefix}{node.name}.", inherited or marked(node.decorator_list))
            elif isinstance(node, FUNCTIONS) and node.name.startswith("test_"):
                for line in timing.bounds(node):
                    yield f"{prefix}{node.name}", line, inherited or marked(node.decorator_list)

    return list(walk(tree))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    files = sorted((args.root / "tests").rglob("*.py"))
    problems, count, modules = [], 0, {}
    if not files:
        problems.append("No Python test files found")
    for path in files:
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (SyntaxError, UnicodeError) as error:
            problems.append(f"{path.relative_to(args.root)}: cannot parse: {error}")
            continue
        for name, line, has_marker in discover(tree, args.root, modules):
            count += 1
            if not has_marker:
                problems.append(f"{path.relative_to(args.root)}:{line}: {name} requires pytest.mark.wall_clock")
    print(f"Wall clock markers: {len(files)} files, {count} elapsed bounds, {len(problems)} problems")
    for problem in problems:
        print(f"::error::{problem}")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
