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
_TEST_TASK = re.compile(r"^(?:test|tests|check|coverage|mutation)(?:$|[:_.-])|^.*:test$")
_PYTHON = re.compile(r"^(?:python[\d.]*|pypy[\d.]*)$")
_PYTHON_TEST = re.compile(r"\b(?:pytest|unittest|tox|nox|mutmut|mutatest|mutpy)\b|scripts\.ci_mutation\b")
_NODE_TEST = re.compile(r"\b(?:jest|vitest|mocha)\b|require\(['\"]node:test['\"]\)")
_DENY = (
    "BLOCKED: Local test and mutation runs are disabled. Push and open a draft pull request so CI runs the tests. "
    "Set AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=true to opt in to local runs."
)


def local_tests_allowed() -> bool:
    return os.getenv(ALLOW_LOCAL_TEST_RUN_ENV, _FALSE).lower() == _TRUE


def _python_test(args: list[str]) -> bool:
    for flag in ("-m", "-c"):
        if flag in args:
            index = args.index(flag) + 1
            return index < len(args) and bool(_PYTHON_TEST.search(args[index]))
    return any(Path(arg).name.startswith(("test_", "test-")) and arg.endswith(".py") for arg in args)


def _test_command(tokens: list[str]) -> bool:
    name = Path(tokens[0]).name.split("@", 1)[0]
    args = tokens[1:]
    if name in _RUNNERS or name.startswith("pytest-"):
        return True
    if _PYTHON.fullmatch(name):
        return _python_test(args)
    if name == "node":
        return any(arg == "--test" or arg.startswith("--test=") for arg in args) or (
            any(arg in {"-e", "--eval"} for arg in args) and bool(_NODE_TEST.search(" ".join(args)))
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
        runs_tests = any(_test_command(tokens) for tokens in commands(payload.get("tool_input", {}).get("command", "")))
    except ValueError:
        raise BlockAction(_DENY) from None
    if runs_tests:
        raise BlockAction(_DENY)
