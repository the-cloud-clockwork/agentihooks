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


def _block_text(payload):
    with pytest.raises(BlockAction) as caught:
        check(payload)
    return str(caught.value)


def test_built_in_edit_refusal_names_the_path_root_and_allowed_deletion(trees):
    _, worktree = trees
    assert _block_text(_tool("Edit", worktree, file_path="pkg/mod.py")) == (
        f"BLOCKED: pkg/mod.py is Python in the worktree {worktree}, which is edited through Serena "
        "(Development Manifesto §7).\n"
        f"Call mcp__serena__activate_project with {worktree}, then edit with replace_symbol_body, "
        "insert_after_symbol / insert_before_symbol, rename_symbol or replace_content. "
        "A new .py file may be created with Write; a whole file in this worktree is deleted with git rm or rm."
    )


def test_foreign_deletion_refusal_names_the_path_and_checkout(trees, sibling):
    _, worktree = trees
    target = sibling / "pkg" / "mod.py"
    expected = (
        f"BLOCKED: {target} is Python in another checkout, {sibling}. "
        "A session deletes Python files only inside its own checkout."
    )
    assert _block_text(_tool("Bash", worktree, command=f"rm {target}")) == expected
    name, tool_input = _patch(f"*** Delete File: {target}")
    assert _block_text({"tool_name": name, "tool_input": tool_input, "cwd": str(worktree)}) == expected


def test_shell_deletion_with_an_unclosed_quote_still_checks_its_paths(trees, sibling):
    _, worktree = trees
    with pytest.raises(BlockAction, match="another checkout"):
        check(_tool("Bash", worktree, command=f'rm {sibling / "pkg" / "mod.py"} "unclosed'))


def test_a_file_tool_call_without_a_path_is_allowed(trees):
    _, worktree = trees
    check(_tool("Write", worktree, content="x = 1\n"))


@pytest.mark.parametrize(
    "command",
    [
        "find {root} -name '*.py' -delete",
        "find {root}/pkg -type f -exec rm {{}} \\;",
        "find {root}/pkg -execdir /bin/rm {{}} +",
        "find {root}/pkg -exec sudo rm {{}} +",
        "find {root} -name mod.py | xargs rm",
        "echo {root}/pkg/mod.py | xargs rm -f",
        "cd {root} && ls pkg/mod.py | xargs rm",
        "git -C {root} rm pkg/mod.py",
        "git -C '{root}' rm -q pkg/mod.py",
        "rm -r {root}/pkg",
        "rm -rf {root}",
        "sudo rm {root}/pkg/mod.py",
        "rm {root}/pkg/*.py",
        "rm -rf {root}/*",
        "/bin/rm {root}/pkg/mod.py",
        "\\rm {root}/pkg/mod.py",
        "command rm {root}/pkg/mod.py",
        "exec rm {root}/pkg/mod.py",
        "time rm {root}/pkg/mod.py",
        "rm {root}/pkg/mod.py; rm build.log",
        "rm {root}/pkg/mod.py; ls | xargs rm",
        "rm {root}/pkg/mod.py; find . -name '*.log' -delete",
        "cd {root} && find \\( -name '*.py' \\) -delete",
        "cd {root} && find ! -name '*.md' -delete",
    ],
)
def test_other_deletion_forms_of_python_in_another_worktree_are_blocked(trees, sibling, command):
    _, worktree = trees
    with pytest.raises(BlockAction, match="another checkout"):
        check(_tool("Bash", worktree, command=command.format(root=sibling)))


def test_deleting_a_python_file_whose_name_reads_as_a_glob_in_another_worktree_is_blocked(trees, sibling):
    _, worktree = trees
    (sibling / "pkg" / "x[1].py").write_text("x = 1\n")
    with pytest.raises(BlockAction, match="another checkout"):
        check(_tool("Bash", worktree, command=f"rm '{sibling / 'pkg' / 'x[1].py'}'"))


@pytest.mark.parametrize(
    "command",
    [
        "find . -name '*.py' -delete",
        "find pkg -exec rm {{}} +",
        "find . -newer {other}/pkg/mod.py -delete",
        "diff {other}/pkg/mod.py pkg/mod.py; ls | xargs rm -f build.log",
        "ls pkg/mod.py | xargs rm",
        "git -C {root} rm pkg/mod.py",
        "rm -r pkg",
        "rm pkg/*.py",
        "rm {other}/*.md",
        "rm {other}/README.md",
        "rm -r {other}/docs",
        "rm {loose}",
        "rm {other}/pkg/missing.py",
    ],
)
def test_deletions_inside_the_worktree_or_of_no_python_are_allowed(trees, sibling, command):
    _, worktree = trees
    (sibling / "README.md").write_text("doc\n")
    (sibling / "docs").mkdir()
    (sibling / "docs" / "guide.md").write_text("doc\n")
    loose = worktree.parent.parent.parent / "loose.py"
    loose.write_text("x = 1\n")
    check(_tool("Bash", worktree, command=command.format(root=worktree, other=sibling, loose=loose)))


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


@pytest.fixture
def launch(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@323133-2196")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    checkout = tmp_path / "agentihooks"
    (checkout / ".git").mkdir(parents=True)
    return checkout


def _repo_worktree(base, repo, name):
    root = base / repo / name
    (root / "pkg").mkdir(parents=True)
    (root / ".git").write_text(f"gitdir: /src/{repo}/.git/worktrees/{name}\n")
    (root / "pkg" / "mod.py").write_text("x = 1\n")
    return root


@pytest.mark.parametrize(
    ("repo", "name"),
    [
        ("antoncore", "engineer-323133-2196"),
        ("antoncore", "engineer-323133-2196-2"),
        ("antoncore/_tmp", "engineer-323133-2196-tmp-1"),
    ],
)
def test_deleting_python_in_the_sessions_own_worktree_of_another_repo_is_allowed(tmp_path, launch, repo, name):
    own = _repo_worktree(tmp_path / "worktrees", repo, name)
    check(_tool("Bash", launch, command=f"git -C {own} rm pkg/mod.py"))
    check(_tool("Bash", launch, command=f"rm {own / 'pkg' / 'mod.py'}"))
    patch_name, tool_input = _patch(f"*** Delete File: {own / 'pkg' / 'mod.py'}")
    check({"tool_name": patch_name, "tool_input": tool_input, "cwd": str(launch)})


def test_a_symlinked_worktree_root_still_matches(tmp_path, launch, monkeypatch):
    own = _repo_worktree(tmp_path / "worktrees", "antoncore", "engineer-323133-2196")
    (tmp_path / "link").symlink_to(tmp_path / "worktrees")
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "link"))
    check(_tool("Bash", launch, command=f"rm {own / 'pkg' / 'mod.py'}"))


def test_the_worktree_root_defaults_to_dev_worktrees_in_home(tmp_path, launch, monkeypatch):
    monkeypatch.delenv("WORKTREE_ROOT")
    monkeypatch.setenv("HOME", str(tmp_path))
    own = _repo_worktree(tmp_path / "dev" / "worktrees", "antoncore", "engineer-323133-2196")
    check(_tool("Bash", launch, command=f"rm {own / 'pkg' / 'mod.py'}"))


def test_edits_in_the_sessions_own_worktree_of_another_repo_follow_the_worktree_rule(tmp_path, launch):
    own = _repo_worktree(tmp_path / "worktrees", "antoncore", "engineer-323133-2196")
    (own / "README.md").write_text("doc\n")
    check(_tool("Edit", launch, file_path=str(own / "README.md")))
    target = own / "pkg" / "mod.py"
    assert _block_text(_tool("Edit", launch, file_path=str(target))) == (
        f"BLOCKED: {target} is Python in the worktree {own}, which is edited through Serena "
        "(Development Manifesto §7).\n"
        f"Call mcp__serena__activate_project with {own}, then edit with replace_symbol_body, "
        "insert_after_symbol / insert_before_symbol, rename_symbol or replace_content. "
        "A new .py file may be created with Write; a whole file in this worktree is deleted with git rm or rm."
    )


@pytest.mark.parametrize(
    ("base", "name"),
    [
        ("worktrees", "engineer-323133-2195"),
        ("worktrees", "engineer-323133-2196-tmp-1"),
        ("elsewhere", "engineer-323133-2196"),
    ],
)
def test_deleting_python_in_a_foreign_worktree_of_another_repo_is_blocked(tmp_path, launch, base, name):
    other = _repo_worktree(tmp_path / base, "antoncore", name)
    target = other / "pkg" / "mod.py"
    assert _block_text(_tool("Bash", launch, command=f"git rm {target}")) == (
        f"BLOCKED: {target} is Python in another checkout, {other}. "
        "A session deletes Python files only inside its own checkout."
    )


def test_a_session_without_an_agent_name_owns_no_other_worktree(tmp_path, launch, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    own = _repo_worktree(tmp_path / "worktrees", "antoncore", "engineer-323133-2196")
    target = own / "pkg" / "mod.py"
    assert _block_text(_tool("Bash", launch, command=f"rm {target}")) == (
        f"BLOCKED: {target} is Python in another checkout, {own}. "
        "A session deletes Python files only inside its own checkout."
    )
