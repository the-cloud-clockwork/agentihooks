"""Tests for hooks.context.serena_edit_guard."""

import pytest

from hooks.context.serena_edit_guard import check
from hooks.hook_manager import BlockAction


@pytest.fixture
def trees(tmp_path):
    primary = tmp_path / "repo"
    (primary / ".git").mkdir(parents=True)
    (primary / "pkg").mkdir()
    (primary / "pkg" / "mod.py").write_text("x = 1\n")
    worktree = tmp_path / "worktrees" / "repo" / "task"
    (worktree / "pkg").mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {primary}/.git/worktrees/task\n")
    (worktree / "pkg" / "mod.py").write_text("x = 1\n")
    (worktree / "README.md").write_text("doc\n")
    return primary, worktree


def _tool(name, cwd, **tool_input):
    return {"tool_name": name, "tool_input": tool_input, "cwd": str(cwd)}


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit", "Write"])
def test_built_in_edits_of_worktree_python_are_blocked(trees, tool):
    _, worktree = trees
    with pytest.raises(BlockAction, match="activate_project"):
        check(_tool(tool, worktree, file_path=str(worktree / "pkg" / "mod.py")))


def test_writing_a_new_python_file_is_allowed(trees):
    _, worktree = trees
    check(_tool("Write", worktree, file_path=str(worktree / "pkg" / "new.py")))


def test_primary_checkout_and_non_python_files_are_allowed(trees):
    primary, worktree = trees
    check(_tool("Edit", primary, file_path=str(primary / "pkg" / "mod.py")))
    check(_tool("Edit", worktree, file_path=str(worktree / "README.md")))


def test_serena_tools_are_allowed(trees):
    _, worktree = trees
    check(_tool("mcp__serena__replace_content", worktree, relative_path="pkg/mod.py"))


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 's/1/2/' pkg/mod.py",
        "sed --in-place 's/1/2/' pkg/mod.py",
        "cp other.py pkg/mod.py",
        "mv /tmp/new.py pkg/mod.py && echo moved",
        "cd pkg && sed -i s/1/2/ mod.py",
        "perl -pi -e 's/1/2/' pkg/mod.py",
        "dd if=/dev/null of=pkg/mod.py",
        "echo 'y = 2' >> pkg/mod.py",
        "cat > pkg/mod.py <<'EOF'\nx = 2\nEOF",
        "printf 'x' | tee pkg/mod.py",
        "python3 - <<'EOF'\nopen('pkg/mod.py', 'w').write('x = 2')\nEOF",
        "python3 -c \"from pathlib import Path; Path('pkg/mod.py').write_text('x')\"",
    ],
)
def test_shell_rewrites_of_worktree_python_are_blocked(trees, command):
    _, worktree = trees
    with pytest.raises(BlockAction, match="Serena"):
        check(_tool("Bash", worktree, command=command))


@pytest.mark.parametrize(
    "command",
    [
        "sed -n 1,5p pkg/mod.py",
        "cat pkg/mod.py",
        "echo 'x' > pkg/new.py",
        "python3 -m pytest -q",
        'git commit -m "move helper -> pkg/mod.py"',
        'echo "renamed a -> pkg/mod.py" >> notes.md',
        "ruff format pkg/mod.py",
    ],
)
def test_shell_reads_and_new_files_are_allowed(trees, command):
    _, worktree = trees
    check(_tool("Bash", worktree, command=command))


def test_the_guard_can_be_turned_off(trees, monkeypatch):
    _, worktree = trees
    monkeypatch.setattr("hooks.config.SERENA_EDIT_GUARD_ENABLED", False)
    check(_tool("Edit", worktree, file_path=str(worktree / "pkg" / "mod.py")))
