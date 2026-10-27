import ast
import os
import posixpath
import re
from collections.abc import Callable
from pathlib import Path

from hooks.context.shell_commands import located

ALLOW_LOCAL_TEST_RUN_ENV = "AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN"
_FALSE = "false"
_TRUE = "true"
_RUNNERS = frozenset(
    {
        "pytest",
        "py.test",
        "unittest",
        "tox",
        "tox4",
        "nox",
        "mutmut",
        "cosmic-ray",
        "cosmic_ray",
        "mutation",
        "mutatest",
        "mutpy",
        "stryker",
        "stryker-cli",
        "infection",
        "cargo-mutants",
        "vitest",
        "jest",
        "mocha",
        "ava",
        "tap",
        "nyc",
        "c8",
        "ctest",
        "rspec",
        "phpunit",
        "pest",
        "prove",
    }
)
_BUILD_TOOLS = frozenset(
    {
        "npm",
        "pnpm",
        "yarn",
        "bun",
        "go",
        "cargo",
        "make",
        "gmake",
        "just",
        "task",
        "playwright",
        "meson",
        "dotnet",
        "mvn",
        "mvnw",
        "gradle",
        "gradlew",
        "hatch",
    }
)
_TEST_TASK = re.compile(r"^(?:test|tests|coverage|mutation)(?:$|[:_.-])|^.*:test$")
_PYTHON = re.compile(r"^(?:python[\d.]*|pypy[\d.]*)$")
_PYTHON_VALUE_OPTIONS = frozenset({"-W", "-X", "-Q", "--check-hash-based-pycs"})
_PYTHON_TEST = re.compile(
    r"\b(?:pytest|unittest|tox|nox|mutmut|mutatest|mutpy)\b|scripts\.ci_mutation\b(?!\.clearances\b)"
)
_MUTATION_PATH = re.compile(r"(?:^|/)scripts/ci_mutation(?!/clearances\.py$)(?:/|$)")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(?P<q>['\"]?)(?P<end>\w+)(?P=q)")
_SCRIPT_NAME = re.compile(r"[^\s'\"<>|;&]+\.py\b")
_SCRIPT_BYTES = 1_000_000
_NODE_TEST = re.compile(r"\b(?:jest|vitest|mocha)\b|node:test")
_LITERAL = (
    r"(?P<module>\b(?:require|import)\s*\(\s*|\b(?:from|import)\s*)?"
    r"(?P<q>\"{3}|'{3}|[\"'`])(?:\\.|(?!(?P=q))[^\\])*(?P=q)"
)
_LAUNCH = re.compile(
    r"\b(?:\w+_)?(?:subprocess|system|popen|spawn|exec)\w*"
    r"|\b(?:eval|compile|Function|vm|_?run_module\w*|run_path|__import__|import_module|child_process)\b"
    r"|\brequire\s*\(\s*[A-Za-z_$]"
)
_NAME_LITERAL = re.compile(r"([\"'`])\w+\1")
_FORMAT_PREFIX = re.compile(r"(?i)(?<!\w)[rb]?f[rb]?$")
_READERS = frozenset({"getsource", "getsourcelines", "getsourcefile", "getfile", "getdoc", "signature"})
_RUNS_ON_IMPORT = frozenset({"unittest.__main__"})
_READ_NODES = (
    ast.Module,
    ast.Expr,
    ast.Import,
    ast.ImportFrom,
    ast.alias,
    ast.Call,
    ast.Attribute,
    ast.Name,
    ast.Constant,
    ast.Load,
)
_DENY = (
    "BLOCKED: Local test and mutation runs are disabled. Push and open a draft pull request so CI runs the tests. "
    "Set AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=true to opt in to local runs."
)


def local_tests_allowed() -> bool:
    return os.getenv(ALLOW_LOCAL_TEST_RUN_ENV, _FALSE).lower() == _TRUE


def _strip(source: str, comment: str, keep: Callable[[str, re.Match], bool]) -> str:
    return re.sub(
        f"(?s){_LITERAL}|{re.escape(comment)}[^\\n]*",
        lambda match: match[0] if keep(source, match) else match["q"],
        source,
    )


def _module_literal(source: str, match: re.Match) -> bool:
    return bool(match["module"])


def _named_literal(source: str, match: re.Match) -> bool:
    return bool(match["module"] or _NAME_LITERAL.fullmatch(match[0]) or _FORMAT_PREFIX.search(source, 0, match.start()))


def _launches(source: str, comment: str) -> bool:
    return bool(_LAUNCH.search(_strip(source, comment, _named_literal)))


def _code_runs_tests(source: str, runners: re.Pattern, comment: str) -> bool:
    return bool(runners.search(source if _launches(source, comment) else _strip(source, comment, _module_literal)))


def _imports(tree: ast.Module) -> list[tuple[str, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, alias.asname or alias.name.partition(".")[0]) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found += [(f"{node.module}.{alias.name}", alias.asname or alias.name) for alias in node.names]
    return found


def _is_reader(call: ast.Call, imports: list[tuple[str, str]]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return (
            func.attr in _READERS
            and isinstance(func.value, ast.Name)
            and (func.value.id, "inspect") in {(bound, path) for path, bound in imports}
        )
    return isinstance(func, ast.Name) and any(
        bound == func.id and path.startswith("inspect.") and path[8:] in _READERS for path, bound in imports
    )


def _read_roots(tree: ast.Module, imports: list[tuple[str, str]]) -> set[int]:
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_reader(node, imports):
            for arg in node.args:
                chain = [arg]
                while isinstance(chain[-1], ast.Attribute):
                    chain.append(chain[-1].value)
                if isinstance(chain[-1], ast.Name):
                    roots.update(id(link) for link in chain)
    return roots


def _only_reads(tree: ast.Module, imports: list[tuple[str, str]]) -> bool:
    return all(
        isinstance(node, _READ_NODES)
        and (
            not isinstance(node, ast.Call)
            or _is_reader(node, imports)
            or (isinstance(node.func, ast.Name) and node.func.id == "print")
        )
        for node in ast.walk(tree)
    )


def _node_text(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _reads_runner_source(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return False
    imports = _imports(tree)
    runners = {bound for path, bound in imports if _PYTHON_TEST.search(path)}
    if any(path in _RUNS_ON_IMPORT for path, _ in imports):
        return False
    roots = _read_roots(tree, imports)
    uses = [node for node in ast.walk(tree) if _node_text(node) in runners or _PYTHON_TEST.search(_node_text(node))]
    return bool(uses) and all(id(node) in roots for node in uses) and _only_reads(tree, imports)


def _python_code_runs_tests(code: str) -> bool:
    return _code_runs_tests(code, _PYTHON_TEST, "#") and (_launches(code, "#") or not _reads_runner_source(code))


def _python_script(args: list[str]) -> str | None:
    value = False
    for arg in args:
        if value:
            value = False
        elif arg in _PYTHON_VALUE_OPTIONS:
            value = True
        elif arg == "-" or arg.startswith(("-m", "-c")):
            return None
        elif not arg.startswith("-"):
            return arg
    return None


def _python_test(args: list[str]) -> bool:
    for index, arg in enumerate(args):
        if arg in {"-m", "-c"}:
            if index + 1 < len(args) and _python_code_runs_tests(args[index + 1]):
                return True
        elif arg.startswith(("-m", "-c")) and _python_code_runs_tests(arg[2:]):
            return True
    script = _python_script(args)
    return script is not None and (
        Path(script).name in _RUNNERS
        or (Path(script).name.startswith(("test_", "test-")) and script.endswith(".py"))
        or _MUTATION_PATH.search(posixpath.normpath(script)) is not None
    )


def _test_command(tokens: list[str]) -> bool:
    name = Path(tokens[0]).name.partition("@")[0]
    args = tokens[1:]
    if name in _RUNNERS or name.startswith("pytest-"):
        return True
    if _PYTHON.fullmatch(name):
        return _python_test(args)
    if name == "node":
        return any(arg == "--test" or arg.startswith("--test=") for arg in args) or (
            any(arg in {"-e", "--eval"} for arg in args) and _code_runs_tests(" ".join(args), _NODE_TEST, "//")
        )
    if name == "ruby":
        return "rspec" in args
    if name in _BUILD_TOOLS:
        return any(_TEST_TASK.search(arg) for arg in args) or (name == "cargo" and "nextest" in args)
    return name.startswith(("test-", "test_")) and name.endswith(".sh")


def _heredoc_scripts(command: str) -> dict[str, str]:
    written, lines = {}, iter(command.splitlines(keepends=True))
    for line in lines:
        marker = _HEREDOC.search(line)
        if not marker:
            continue
        body = []
        for content in lines:
            if content.strip() == marker["end"]:
                break
            body.append(content)
        written.update((Path(name).name, "".join(body)) for name in _SCRIPT_NAME.findall(line))
    return written


def _expanded(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path)))


def _script_on_disk(script: str, folders: list[str], cwd: str) -> str | None:
    path = Path(cwd).joinpath(*(_expanded(folder) for folder in folders), _expanded(script))
    try:
        return path.read_text(errors="ignore") if path.stat().st_size <= _SCRIPT_BYTES else None
    except OSError:
        return None


def _script_runs_tests(tokens: list[str], folders: list[str], written: dict[str, str], cwd: str) -> bool:
    if not _PYTHON.fullmatch(Path(tokens[0]).name.partition("@")[0]):
        return False
    script = _python_script(tokens[1:])
    if script is None:
        return False
    source = written.get(Path(script).name)
    if source is None:
        source = _script_on_disk(script, folders, cwd)
    return source is not None and _python_code_runs_tests(source)


def check_local_tests(payload: dict) -> None:
    if payload.get("tool_name") != "Bash" or local_tests_allowed():
        return
    from hooks.hook_manager import BlockAction

    try:
        tool_input = payload.get("tool_input", {})
        command = tool_input.get("command") or tool_input.get("cmd")
        if not command:
            return
        written, cwd = _heredoc_scripts(command), payload.get("cwd") or os.getcwd()
        runs_tests = any(
            _test_command(tokens) or _script_runs_tests(tokens, folders, written, cwd)
            for tokens, folders in located(command)
        )
    except ValueError:
        raise BlockAction(_DENY) from None
    if runs_tests:
        raise BlockAction(_DENY)
