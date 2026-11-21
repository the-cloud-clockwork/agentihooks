"""The qa and reviewer package roles refuse shell writes and git write commands and keep shell reads open."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from profiles.package.roles._guards import no_code_edits

ROLES = Path(__file__).resolve().parents[2] / "profiles" / "package" / "roles"
SHELL_GUARD = "pre-bash-no_shell_writes.py"
READ_ONLY = ("qa", "reviewer")
FEATURE = "read-only-roles-refuse-shell-writes"
SWARM = {"AGENTIHOOKS_SWARM": "test-swarm", "AGENTIHOOKS_SWARM_TASK": "task1"}


def _task_folder(home: Path) -> Path:
    return home / ".agentihooks" / "swarm" / "test-swarm" / "tasks" / "task1"


def _shim(home: Path, role: str, command: str) -> subprocess.CompletedProcess[str]:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(home / "repo")}
    env = {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / "config"), "PATH": "/usr/bin:/bin", **SWARM}
    script = ROLES / role / ".claude" / "conditions" / SHELL_GUARD
    return subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload), capture_output=True, text=True, env=env
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for name, value in SWARM.items():
        monkeypatch.setenv(name, value)
    return tmp_path


def _reason(home: Path, command: str, role: str = "reviewer") -> str:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(home / "repo")}
    return no_code_edits.deny_reason(payload, role, home)


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("role", READ_ONLY)
def test_a_read_only_seat_refuses_a_shell_redirect_into_the_repository(tmp_path: Path, role: str) -> None:
    refused = _shim(tmp_path, role, "echo patched > app.py")

    assert refused.returncode == 2
    assert f"{role} cannot write through the shell: the command writes app.py" in refused.stderr


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("role", READ_ONLY)
def test_a_read_only_seat_refuses_a_git_commit(tmp_path: Path, role: str) -> None:
    refused = _shim(tmp_path, role, "git commit -am 'fix the review finding'")

    assert refused.returncode == 2
    assert f"{role} cannot write through the shell: the command runs git commit" in refused.stderr


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("role", READ_ONLY)
def test_a_read_only_seat_keeps_shell_reads(tmp_path: Path, role: str) -> None:
    read = _shim(tmp_path, role, "git diff origin/dev...HEAD && cat README.md | grep -n '>' 2>/dev/null")

    assert (read.returncode, read.stderr) == (0, "")


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("role", READ_ONLY)
def test_a_read_only_seat_writes_its_scratchpad_and_task_folder(tmp_path: Path, role: str) -> None:
    scratch = _shim(tmp_path, role, "echo note >> ~/scratchpad/review/notes.md")
    proof = _shim(tmp_path, role, f"cat >> {_task_folder(tmp_path)}/proof.md <<'EOF'\nread > wrote, it's fine\nEOF")

    assert (scratch.returncode, proof.returncode) == (0, 0)


@pytest.mark.feature(FEATURE)
def test_only_the_read_only_roles_carry_the_shell_guard() -> None:
    carried = sorted(role.name for role in ROLES.iterdir() if (role / ".claude" / "conditions" / SHELL_GUARD).exists())

    assert carried == sorted(READ_ONLY)


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize(
    ("command", "found"),
    [
        ("printf x >> notes.md", "writes notes.md"),
        ("echo x >| app.py", "writes app.py"),
        ("make &> build.log", "writes build.log"),
        ("ls >&listing.txt", "writes listing.txt"),
        ("ls 2> errors.txt", "writes errors.txt"),
        ("echo x | tee -a app.py", "writes app.py"),
        ("cp ~/scratchpad/a.py app.py", "writes app.py"),
        ("mv app.py ~/scratchpad/app.py", "writes app.py"),
        ("rm -rf build", "writes build"),
        ("rmdir build", "writes build"),
        ("touch app.py", "writes app.py"),
        ("mkdir -p build", "writes build"),
        ("ln -s ~/scratchpad/a app.py", "writes app.py"),
        ("chmod 755 run.sh", "writes run.sh"),
        ("chown me run.sh", "writes run.sh"),
        ("truncate -s 0 app.py", "writes app.py"),
        ("install -m 644 ~/scratchpad/a app.py", "writes app.py"),
        ("rsync -a ~/scratchpad/a/ build/", "writes build/"),
        ("unlink app.py", "writes app.py"),
        ("sed -i 's/a/b/' app.py", "writes app.py"),
        ("sed --in-place=.bak 's/a/b/' app.py", "writes app.py"),
        ("perl -pi -e 's/a/b/' app.py", "writes app.py"),
        ("cd /tmp && echo x > out.txt", "writes out.txt"),
        ("sudo rm app.py", "writes app.py"),
        ("FOO=1 rm app.py", "writes app.py"),
        ("echo $(rm app.py)", "writes app.py"),
        ("echo `rm app.py`", "writes app.py"),
    ],
)
def test_a_reviewer_refuses_each_shell_file_write(home: Path, command: str, found: str) -> None:
    assert _reason(home, command) == (
        f"reviewer cannot write through the shell: the command {found}. "
        "Reads, ~/scratchpad and the task folder stay open; send the change to its author."
    )


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize(
    ("command", "found"),
    [
        ("git push origin HEAD", "runs git push"),
        ("git add -A", "runs git add"),
        ("git checkout -b fix", "runs git checkout"),
        ("git switch dev", "runs git switch"),
        ("git restore app.py", "runs git restore"),
        ("git reset --hard origin/dev", "runs git reset"),
        ("git merge origin/dev", "runs git merge"),
        ("git rebase origin/dev", "runs git rebase"),
        ("git cherry-pick abc123", "runs git cherry-pick"),
        ("git stash", "runs git stash"),
        ("git branch fix", "runs git branch"),
        ("git tag v1", "runs git tag"),
        ("git apply fix.diff", "runs git apply"),
        ("git worktree add ../w", "runs git worktree"),
        ("git -C ../other commit -m x", "runs git commit"),
        ("git -c user.name=x commit -m x", "runs git commit"),
        ("env GIT_PAGER=cat git pull", "runs git pull"),
        ("timeout 30 git push", "runs git push"),
        ("if true; then git commit -m x; fi", "runs git commit"),
        ("gh pr checkout 12", "runs gh pr checkout"),
        ("gh repo clone owner/repo", "runs gh repo clone"),
        ("bash -c 'git commit -m x'", "runs git commit"),
        ('sh -c "echo x > app.py"', "writes app.py"),
        ("eval git push", "runs git push"),
        ("find . -name '*.pyc' -delete", "deletes files with find -delete"),
        ("find . -name '*.pyc' -exec rm {} \\;", "writes {}"),
        ("git ls-files | xargs rm", "writes {}"),
        ("bash <<'EOF'\ngit commit -m x\nEOF", "feeds a script to bash on its input"),
        ("echo 'git push' | sh", "feeds a script to sh on its input"),
    ],
)
def test_a_reviewer_refuses_git_writes_and_hidden_writes(home: Path, command: str, found: str) -> None:
    assert f"the command {found}." in _reason(home, command)


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize(
    "command",
    [
        "git status && git log --oneline -5 && git show HEAD:README.md",
        "git -C /repo fetch origin dev && git merge-base HEAD origin/dev && git rev-parse HEAD",
        "git blame app.py; git grep -n foo; git ls-files | head",
        "git rev-list --count HEAD && git ls-tree HEAD && git cat-file -p HEAD && git describe --tags",
        "git ls-remote origin && git shortlog -s && git for-each-ref && git show-ref && git name-rev HEAD",
        "git range-diff a...b && git diff-tree -r HEAD && git cherry origin/dev && git version && git help log",
        "git --no-pager diff --stat",
        "echo \"a > b\" && printf '%s\\n' 'x>y'",
        "make test > /dev/null 2>&1",
        "ls 2>/dev/null >/dev/stderr",
        "gh pr view 12 --json body > ~/scratchpad/pr.json",
        "gh pr comment 12 --body-file ~/scratchpad/review.md",
        "gh pr diff 12 | grep -n tool_name",
        'agentihooks msg send master@swarm "done > next"',
        "cd ~/scratchpad && echo x > notes.md",
        "cp app.py ~/scratchpad/app.py",
        "mkdir -p ~/scratchpad/t && touch ~/scratchpad/t/n.md && rm ~/scratchpad/t/n.md",
        "sed -n 1,20p app.py",
        "grep -c x <<< 'a > b'",
        "bash -c 'git diff'",
        "find . -name '*.py' -exec grep -l x {} +",
        "git ls-files | xargs wc -l",
        "bash scripts/check.sh",
        'echo "unterminated',
        "",
    ],
)
def test_a_reviewer_keeps_reads_and_writes_inside_its_own_folders(home: Path, command: str) -> None:
    assert _reason(home, command) == ""


@pytest.mark.feature(FEATURE)
def test_a_reviewer_appends_to_its_task_folder_with_a_heredoc(home: Path) -> None:
    command = f"cat >> {_task_folder(home)}/proof.md <<'EOF'\nline with > and it's quoted\nEOF"

    assert _reason(home, command) == ""
    assert _reason(home, command.replace("task1/proof.md", "task2/proof.md"))


@pytest.mark.feature(FEATURE)
def test_edit_tool_payloads_keep_the_edit_path(home: Path) -> None:
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(home / "repo" / "app.py")}, "cwd": str(home)}
    scratch = {"tool_name": "Write", "tool_input": {"file_path": str(home / "scratchpad" / "n.md")}, "cwd": str(home)}

    assert no_code_edits.deny_reason(edit, "reviewer", home) == (
        "reviewer cannot edit outside ~/scratchpad; send the change to its author."
    )
    assert no_code_edits.deny_reason(scratch, "reviewer", home) == ""
