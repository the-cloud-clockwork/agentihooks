import ast
import inspect
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from hooks.context import blocker_window, shell_commands

BASE = "1460acf79f"
HELPERS = ("_words", "_command", "_extends", "_dispatches", "_targets", "_ref", "_pushed", "_named", "_destinations")
ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.feature("panic-fix-spare-reads-target")


@pytest.fixture
def original() -> ModuleType:
    source = subprocess.run(
        ["git", "show", f"{BASE}:hooks/context/blocker_window.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    module = ModuleType("panic_parser_original")
    exec(compile(source, "panic_parser_original", "exec"), module.__dict__)
    module.__dict__["_source"] = source
    return module


def corpus() -> list[list[str]]:
    tree = ast.parse((ROOT / "tests/swarm/test_panic.py").read_text())
    tokens: list[list[str]] = [[]]
    for command in (
        "git push origin HEAD:fix",
        "git -C 'folder with spaces' push origin HEAD:dev",
        "git push lab -o branch=fix HEAD",
        "git push lab --push-option=branch=dev HEAD",
        "env NAME=value bash -c 'git push origin HEAD'",
        "cd nested && git push origin fix && gh run rerun 5",
        "git push origin dev 2>/dev/null",
        "git push origin fix 2>&1",
        "gh workflow run tests.yml --ref fix",
        "gh workflow run tests.yml -rdev",
        "gh api -X POST repos/o/r/actions/workflows/tests.yml/dispatches",
        "gh api --field ref=fix repos/o/r/dispatches?x=y",
        "gh api --header value repos/o/r/actions/runs",
        "kubectl -n swarm rollout restart deployment/hub",
        "helm --namespace swarm upgrade release chart",
        "argocd app --server host sync swarm",
    ):
        tokens.extend(words for words, _ in shell_commands.located(command))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.List)
            and node.elts
            and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts)
        ):
            tokens.append(ast.literal_eval(node))
    return tokens


def test_the_nine_helpers_move_verbatim(original: ModuleType) -> None:
    for name in (
        "_FROZEN_COMMANDS",
        "_GLOBAL_VALUES",
        "_PUSH_WIDENS",
        "_PUSH_VALUES",
        "_API_VALUES",
        "_GH_REPO",
        "_REDIRECTION",
    ):
        assert getattr(shell_commands, name) == getattr(original, name), name
    for name in HELPERS:
        before = next(
            node
            for node in ast.parse(original.__dict__["_source"]).body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        after = ast.parse(inspect.getsource(getattr(shell_commands, name))).body[0]
        assert ast.dump(before) == ast.dump(after), name


def test_parser_outputs_match(original: ModuleType) -> None:
    cases = corpus()
    for tokens in cases:
        for name in HELPERS:
            assert getattr(shell_commands, name)(tokens) == getattr(original, name)(tokens), (name, tokens)
    print(f"Parser equivalence: {len(cases)} token cases, {len(HELPERS)} helpers, zero differences")


@pytest.mark.parametrize("branch", ["fix", "dev", "successor"])
@pytest.mark.parametrize("routed", [False, True])
def test_panic_verdicts_match(original: ModuleType, monkeypatch: pytest.MonkeyPatch, branch: str, routed: bool) -> None:
    def git(cwd: str, *args: str) -> str:
        return {
            ("branch", "--show-current"): branch,
            ("config", "--list"): "push.default=matching" if routed else "push.default=simple",
            ("remote", "get-url", "origin"): "https://github.com/o/r.git",
            ("remote", "get-url", "--push", "--all", "origin"): "https://github.com/o/r.git",
            ("config", f"branch.{branch}.remote"): "origin",
            ("config", f"branch.{branch}.merge"): "refs/heads/fix" if branch == "successor" else f"refs/heads/{branch}",
        }.get(args, "")

    for module in (original, blocker_window):
        monkeypatch.setattr(module, "_git", git)
    cases = corpus()
    for tokens in cases:
        assert blocker_window._freezes(tokens) == original._freezes(tokens), tokens
        for cwd in ("", "/repo"):
            assert blocker_window._spared(tokens, cwd, ("https://github.com/o/r/tree/fix",)) == original._spared(
                tokens, cwd, ("https://github.com/o/r/tree/fix",)
            ), tokens
    print(f"Panic equivalence: {len(cases)} token cases, branch={branch}, routed={routed}, zero differences")
