"""Tests for hooks.context.serena_edit_guard."""

import json
import subprocess
import sys

import pytest

from hooks.context.serena_edit_guard import check
from hooks.hook_manager import BlockAction
from hooks.targets.normalizer import _codex_tool_call


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
        "install -m 644 other.py pkg/mod.py",
        "sudo install -m 644 other.py pkg/mod.py",
        "echo copying && cp other.py pkg/mod.py",
        "sudo -u dev cp other.py pkg/mod.py",
        "env LC_ALL=C cp other.py pkg/mod.py",
        "echo other.py | xargs -I{} cp {} pkg/mod.py",
        "(cp other.py pkg/mod.py && echo copied)",
        "cd pkg && sed -i s/1/2/ mod.py",
        "(cd pkg && sed -i s/1/2/ mod.py)",
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
        "git add scripts/install.py pkg/mod.py",
        "git add scripts/cp.py lib/mv.py pkg/mod.py",
        "sudo git add scripts/install.py pkg/mod.py",
    ],
)
def test_shell_reads_and_new_files_are_allowed(trees, command):
    _, worktree = trees
    check(_tool("Bash", worktree, command=command))


def _patch(*actions):
    body = "\n".join(["*** Begin Patch", *actions, "*** End Patch"])
    return _codex_tool_call("apply_patch", {"command": body})


@pytest.fixture
def sibling(trees):
    primary, worktree = trees
    other = worktree.parent / "other"
    (other / "pkg").mkdir(parents=True)
    (other / ".git").write_text(f"gitdir: {primary}/.git/worktrees/other\n")
    (other / "pkg" / "mod.py").write_text("x = 1\n")
    return other


@pytest.mark.parametrize(
    "command", ["git rm pkg/mod.py", "git rm -f pkg/mod.py", "rm pkg/mod.py", "rm -f pkg/mod.py && echo gone"]
)
def test_shell_deletion_of_own_worktree_python_is_allowed(trees, command):
    _, worktree = trees
    check(_tool("Bash", worktree, command=command))


def test_patch_deletion_of_own_worktree_python_is_allowed(trees):
    _, worktree = trees
    name, tool_input = _patch(f"*** Delete File: {worktree / 'pkg' / 'mod.py'}")
    check({"tool_name": name, "tool_input": tool_input, "cwd": str(worktree)})


def test_patch_updating_worktree_python_beside_a_deletion_is_blocked(trees):
    _, worktree = trees
    (worktree / "pkg" / "gone.py").write_text("y = 1\n")
    name, tool_input = _patch(
        f"*** Delete File: {worktree / 'pkg' / 'gone.py'}",
        f"*** Update File: {worktree / 'pkg' / 'mod.py'}",
        "@@",
        "-x = 1",
        "+x = 2",
    )
    with pytest.raises(BlockAction, match="activate_project"):
        check({"tool_name": name, "tool_input": tool_input, "cwd": str(worktree)})


@pytest.mark.parametrize(
    "command", ["git rm {path}", "rm -f {path}", "cd {root} && rm pkg/mod.py && cd -", "echo x; git rm -q {path}"]
)
def test_shell_deletion_of_python_in_another_worktree_is_blocked(trees, sibling, command):
    _, worktree = trees
    command = command.format(path=sibling / "pkg" / "mod.py", root=sibling)
    with pytest.raises(BlockAction, match="another checkout"):
        check(_tool("Bash", worktree, command=command))


def test_patch_deletion_of_python_in_another_worktree_is_blocked(trees, sibling):
    _, worktree = trees
    name, tool_input = _patch(f"*** Delete File: {sibling / 'pkg' / 'mod.py'}")
    with pytest.raises(BlockAction, match="another checkout"):
        check({"tool_name": name, "tool_input": tool_input, "cwd": str(worktree)})


@pytest.mark.parametrize("command", ["git rm {path}", "rm '{path}'"])
def test_shell_deletion_of_primary_checkout_python_from_a_worktree_is_blocked(trees, command):
    primary, worktree = trees
    command = command.format(path=primary / "pkg" / "mod.py")
    with pytest.raises(BlockAction, match="another checkout"):
        check(_tool("Bash", worktree, command=command))


def test_shell_deletion_of_a_quoted_path_with_spaces_in_another_worktree_is_blocked(trees, sibling):
    _, worktree = trees
    (sibling / "my dir").mkdir()
    (sibling / "my dir" / "mod.py").write_text("x = 1\n")
    with pytest.raises(BlockAction, match="another checkout"):
        check(_tool("Bash", worktree, command=f'rm "{sibling / "my dir" / "mod.py"}"'))


def test_write_with_a_patch_shaped_body_is_blocked(trees):
    _, worktree = trees
    target = worktree / "pkg" / "mod.py"
    content = f"*** Begin Patch\n*** Delete File: {target}\n*** End Patch"
    with pytest.raises(BlockAction, match="activate_project"):
        check(_tool("Write", worktree, file_path=str(target), content=content))


def test_patch_deleting_then_adding_the_same_python_file_is_blocked(trees):
    _, worktree = trees
    target = worktree / "pkg" / "mod.py"
    name, tool_input = _patch(f"*** Delete File: {target}", f"*** Add File: {target}", "+x = 2")
    with pytest.raises(BlockAction, match="activate_project"):
        check({"tool_name": name, "tool_input": tool_input, "cwd": str(worktree)})


def test_the_guard_can_be_turned_off(trees, monkeypatch):
    _, worktree = trees
    monkeypatch.setattr("hooks.config.SERENA_EDIT_GUARD_ENABLED", False)
    check(_tool("Edit", worktree, file_path=str(worktree / "pkg" / "mod.py")))


@pytest.mark.parametrize("profile", ["anton", "engineer"])
@pytest.mark.parametrize("value", [None, "", "true", "0", "banana", "FALSE", " false ", "false"])
def test_agentihooks_serena_switch_preserves_shared_enforcement(trees, monkeypatch, profile, value):
    primary, worktree = trees
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", profile)
    monkeypatch.setenv("SERENA_EDIT_GUARD_ENABLED", "false")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    if value is None:
        monkeypatch.delenv("AGENTIHOOKS_SERENA", raising=False)
    else:
        monkeypatch.setenv("AGENTIHOOKS_SERENA", value)
    payloads = [
        _tool("Edit", primary, file_path=str(primary / "pkg" / "mod.py")),
        _tool("mcp__serena__replace_content", worktree, relative_path="pkg/mod.py"),
        _tool("Edit", worktree, file_path=str(worktree / "pkg" / "mod.py")),
    ]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; from hooks.context.serena_edit_guard import check; "
            "[check(payload) for payload in json.loads(sys.argv[1])]",
            json.dumps(payloads),
        ],
        capture_output=True,
        text=True,
    )
    assert result.stdout == ""
    if value == "false":
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
    else:
        assert result.returncode == 1
        assert "activate_project" in result.stderr
