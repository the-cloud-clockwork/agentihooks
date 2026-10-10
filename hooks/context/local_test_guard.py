import os
import re
from pathlib import Path

from hooks.context.shell_commands import commands

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
_PYTHON_TEST = re.compile(
    r"\b(?:pytest|unittest|tox|nox|mutmut|mutatest|mutpy)\b|scripts\.ci_mutation\b(?!\.clearances\b)"
)
_MUTATION_PATH = re.compile(r"(?:^|/)scripts/ci_mutation(?:/|$)")
_NODE_TEST = re.compile(r"\b(?:jest|vitest|mocha)\b|node:test")
_LITERAL = (
    r"(?P<module>\b(?:require|import)\s*\(\s*|\b(?:from|import)\s*)?"
    r"(?P<q>\"{3}|'{3}|[\"'`])(?:\\.|(?!(?P=q))[^\\])*(?P=q)"
)
_LAUNCH = re.compile(
    r"\b(?:\w+_)?(?:subprocess|system|popen|spawn|exec)\w*"
    r"|\b(?:eval|compile|Function|vm|run_module|run_path|__import__|import_module|child_process)\b"
    r"|\brequire\s*\(\s*[A-Za-z_$]"
)
_DENY = (
    "BLOCKED: Local test and mutation runs are disabled. Push and open a draft pull request so CI runs the tests. "
    "Set AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=true to opt in to local runs."
)


def local_tests_allowed() -> bool:
    return os.getenv(ALLOW_LOCAL_TEST_RUN_ENV, _FALSE).lower() == _TRUE


def _code_runs_tests(source: str, runners: re.Pattern, comment: str) -> bool:
    code = re.sub(
        f"(?s){_LITERAL}|{re.escape(comment)}[^\\n]*",
        lambda match: match[0] if match["module"] else match["q"],
        source,
    )
    return bool(runners.search(source if _LAUNCH.search(source) else code))


def _python_test(args: list[str]) -> bool:
    for index, arg in enumerate(args):
        if arg in {"-m", "-c"}:
            if index + 1 < len(args) and _code_runs_tests(args[index + 1], _PYTHON_TEST, "#"):
                return True
        elif arg.startswith(("-m", "-c")) and _code_runs_tests(arg[2:], _PYTHON_TEST, "#"):
            return True
    if any(arg.startswith(("-m", "-c")) for arg in args):
        return False
    return any(
        Path(arg).name in _RUNNERS
        or (Path(arg).name.startswith(("test_", "test-")) and arg.endswith(".py"))
        or _MUTATION_PATH.search(Path(arg).as_posix())
        for arg in args
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


def check_local_tests(payload: dict) -> None:
    if payload.get("tool_name") != "Bash" or local_tests_allowed():
        return
    from hooks.hook_manager import BlockAction

    try:
        tool_input = payload.get("tool_input", {})
        command = tool_input.get("command") or tool_input.get("cmd")
        if not command:
            return
        runs_tests = any(_test_command(tokens) for tokens in commands(command))
    except ValueError:
        raise BlockAction(_DENY) from None
    if runs_tests:
        raise BlockAction(_DENY)
