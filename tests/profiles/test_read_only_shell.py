"""The qa and reviewer package roles refuse shell writes and git write commands and keep shell reads open."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.context import read_only_shell
from profiles.package.roles._guards import no_code_edits

ROLES = Path(__file__).resolve().parents[2] / "profiles" / "package" / "roles"
SHELL_GUARD = "pre-bash-no_shell_writes.py"
READ_ONLY = ("qa", "reviewer")
SWARM = {"AGENTIHOOKS_SWARM": "test-swarm", "AGENTIHOOKS_SWARM_TASK": "task1"}
UNKNOWN = read_only_shell.UNKNOWN
TAIL = ". Reads, ~/scratchpad and the task folder stay open; send the change to its author."


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


def _found(home: Path, command: str, cwd: Path | None = None) -> str:
    roots = [(home / "scratchpad").resolve(), _task_folder(home).resolve()]
    return read_only_shell.write_reason(command, cwd or home / "repo", roots)


def _reason(home: Path, command: str, role: str = "reviewer") -> str:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(home / "repo")}
    return no_code_edits.deny_reason(payload, role, home)


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
@pytest.mark.parametrize("role", READ_ONLY)
def test_a_read_only_seat_refuses_a_shell_redirect_into_the_repository(tmp_path: Path, role: str) -> None:
    refused = _shim(tmp_path, role, "echo patched > app.py")

    assert refused.returncode == 2
    assert f"{role} cannot write through the shell: the command writes app.py{TAIL}" in refused.stderr


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
@pytest.mark.parametrize("role", READ_ONLY)
def test_a_read_only_seat_refuses_a_git_commit(tmp_path: Path, role: str) -> None:
    refused = _shim(tmp_path, role, "git commit -am 'fix the review finding'")

    assert refused.returncode == 2
    assert f"{role} cannot write through the shell: the command runs git commit{TAIL}" in refused.stderr


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
@pytest.mark.parametrize("role", READ_ONLY)
def test_a_read_only_seat_keeps_shell_reads(tmp_path: Path, role: str) -> None:
    read = _shim(tmp_path, role, "git diff origin/dev...HEAD && cat README.md | grep -n '>' 2>/dev/null")

    assert (read.returncode, read.stderr) == (0, "")


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
@pytest.mark.parametrize("role", READ_ONLY)
def test_a_read_only_seat_writes_its_scratchpad_and_task_folder(tmp_path: Path, role: str) -> None:
    scratch = _shim(tmp_path, role, "echo note >> ~/scratchpad/review/notes.md")
    proof = _shim(tmp_path, role, f"cat >> {_task_folder(tmp_path)}/proof.md <<'EOF'\nread > wrote, it's fine\nEOF")

    assert (scratch.returncode, proof.returncode) == (0, 0)


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
def test_only_the_read_only_roles_carry_the_shell_guard() -> None:
    carried = sorted(role.name for role in ROLES.iterdir() if (role / ".claude" / "conditions" / SHELL_GUARD).exists())

    assert carried == sorted(READ_ONLY)


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("printf x >> notes.md", "notes.md"),
        ("echo x >| app.py", "app.py"),
        ("make &> build.log", "build.log"),
        ("make &>> build.log", "build.log"),
        ("ls >&listing.txt", "listing.txt"),
        ("ls 2> errors.txt", "errors.txt"),
        ("(ls)>out.txt", "out.txt"),
        ("ls 2>&1 > out.txt", "out.txt"),
        ("echo x | tee -a app.py", "app.py"),
        ("rm \\\napp.py", "app.py"),
        ("rm 'app.py", "'app.py"),
        ("cp ~/scratchpad/a.py app.py", "app.py"),
        ("cp -t build ~/scratchpad/a", "build"),
        ("cp --target-directory build ~/scratchpad/a", "build"),
        ("cp --target-directory=build ~/scratchpad/a", "build"),
        ("mv app.py ~/scratchpad/app.py", "app.py"),
        ("rm -rf build", "build"),
        ("rmdir build", "build"),
        ("touch app.py", "app.py"),
        ("mkdir -p build", "build"),
        ("ln -s ~/scratchpad/a app.py", "app.py"),
        ("chmod 755 run.sh", "run.sh"),
        ("chown me run.sh", "run.sh"),
        ("truncate -s 0 app.py", "app.py"),
        ("install -m 644 ~/scratchpad/a app.py", "app.py"),
        ("rsync -a ~/scratchpad/a/ build/", "build/"),
        ("unlink app.py", "app.py"),
        ("dd if=~/scratchpad/a of=app.py", "app.py"),
        ("patch -p1 < fix.diff", UNKNOWN),
        ("sed -i 's/a/b/' app.py", "app.py"),
        ("sed -i.bak 's/a/b/' app.py", "app.py"),
        ("sed --in-place=.bak 's/a/b/' app.py", "app.py"),
        ("sed -i -e 's/a/b/' app.py", "app.py"),
        ("sed -i --expression 's/a/b/' app.py", "app.py"),
        ("sed -i --expression='s/a/b/' app.py", "app.py"),
        ("sed -i -f ~/scratchpad/fix.sed app.py", "app.py"),
        ("sed -i --file ~/scratchpad/fix.sed app.py", "app.py"),
        ("sed -i --file=fix.sed app.py", "app.py"),
        ("perl -pi -e 's/a/b/' app.py", "app.py"),
        ("perl -i -pe 's/a/b/' app.py", "app.py"),
        ("cd /tmp && echo x > out.txt", "out.txt"),
        ("sudo rm app.py", "app.py"),
        ("sudo -u root rm app.py", "app.py"),
        ("sudo -g wheel rm app.py", "app.py"),
        ("env -u HOME rm app.py", "app.py"),
        ("nice -n 5 rm app.py", "app.py"),
        ("command rm app.py", "app.py"),
        ("nohup rm app.py", "app.py"),
        ("exec rm app.py", "app.py"),
        ("time rm app.py", "app.py"),
        ("timeout 30 rm app.py", "app.py"),
        ("timeout -s KILL 30 rm app.py", "app.py"),
        ("timeout -k 5 30 rm app.py", "app.py"),
        ("timeout --signal KILL 30 rm app.py", "app.py"),
        ("timeout --kill-after 5 30 rm app.py", "app.py"),
        ("FOO=1 rm app.py", "app.py"),
        ("/bin/rm app.py", "app.py"),
        ("if rm app.py; then :; fi", "app.py"),
        ("if true; then rm app.py; fi", "app.py"),
        ("if false; then :; else rm app.py; fi", "app.py"),
        ("if false; then :; elif rm app.py; then :; fi", "app.py"),
        ("while rm app.py; do :; done", "app.py"),
        ("until rm app.py; do :; done", "app.py"),
        ("for f in a; do rm app.py; done", "app.py"),
        ("! rm app.py", "app.py"),
        ("{ rm app.py; }", "app.py"),
        ("echo $(rm app.py)", "app.py"),
        ("echo `rm app.py`", "app.py"),
        ('echo "$(rm app.py)"', "app.py"),
        ('echo "`rm app.py`"', "app.py"),
        ('echo "it\'s $(rm app.py)"', "app.py"),
        ("echo $(echo $(rm app.py))", "app.py"),
        ("zsh -c 'rm app.py'", "app.py"),
        ("dash -c 'rm app.py'", "app.py"),
        ("bash -lc 'rm app.py'", "app.py"),
        ('sh -c "echo x > app.py"', "app.py"),
        ("find . -exec rm {} \\;", UNKNOWN),
        ("find . -execdir rm {} +", UNKNOWN),
        ("find . -ok rm {} \\;", UNKNOWN),
        ("find . -okdir rm {} \\;", UNKNOWN),
        ("git ls-files | xargs rm", UNKNOWN),
        ("git ls-files | xargs -I {} rm {}", UNKNOWN),
        ("git ls-files | xargs -n 1 rm", UNKNOWN),
        ("git ls-files | xargs -P 4 rm", UNKNOWN),
        ("git ls-files | xargs -d x rm", UNKNOWN),
        ("git ls-files | xargs -L 1 rm", UNKNOWN),
        ("git ls-files | xargs -s 100 rm", UNKNOWN),
        ("git ls-files | xargs -E eof rm", UNKNOWN),
        ("xargs -a list rm", UNKNOWN),
    ],
)
def test_a_reviewer_refuses_each_shell_file_write(home: Path, command: str, target: str) -> None:
    assert _found(home, command) == f"writes {target}"


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
@pytest.mark.parametrize(
    ("command", "found"),
    [
        ("git commit -m x", "runs git commit"),
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
        ("git pull", "runs git pull"),
        ("git clone https://example.com/r.git", "runs git clone"),
        ("git -C ../other commit -m x", "runs git commit"),
        ("git -c user.name=x commit -m x", "runs git with -c configuration"),
        ("git -c diff.external=touch diff", "runs git with -c configuration"),
        ("git --config-env=core.pager=PAGER log", "runs git with -c configuration"),
        ("git --config-env core.pager=PAGER log", "runs git with -c configuration"),
        ("git diff --output=app.patch", "writes a file with git --output"),
        ("git log --output app.log", "writes a file with git --output"),
        ("env GIT_PAGER=cat git pull", "runs git pull"),
        ("timeout 30 git push", "runs git push"),
        ("gh pr checkout 12", "runs gh pr checkout"),
        ("gh repo clone owner/repo", "runs gh repo clone"),
        ("gh -R owner/repo pr checkout 12", "runs gh pr checkout"),
        ("gh --repo owner/repo pr checkout 12", "runs gh pr checkout"),
        ("gh --repo=owner/repo pr checkout 12", "runs gh pr checkout"),
        ("bash -c 'git commit -m x'", "runs git commit"),
        ("eval git push", "runs git push"),
        ("find . -name '*.pyc' -delete", "deletes files with find -delete"),
        ("bash <<'EOF'\ngit commit -m x\nEOF", "feeds a script to bash on its input"),
        ("echo 'git push' | sh", "feeds a script to sh on its input"),
        ("$(echo rm) app.py", "runs a command named by $(echo rm)"),
        ("`echo rm` app.py", "runs a command named by `echo rm`"),
        ("c=rm; $c app.py", "runs a command named by $c"),
    ],
)
def test_a_reviewer_refuses_git_writes_and_hidden_writes(home: Path, command: str, found: str) -> None:
    assert _found(home, command) == found


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
@pytest.mark.parametrize(
    "command",
    [
        "git status && git log --oneline -5 && git show HEAD:README.md && git diff HEAD",
        "git -C /repo fetch origin dev && git merge-base HEAD origin/dev && git rev-parse HEAD",
        "git blame app.py; git grep -n foo; git ls-files | head",
        "git rev-list --count HEAD && git ls-tree HEAD && git cat-file -p HEAD && git describe --tags",
        "git ls-remote origin && git shortlog -s && git for-each-ref && git show-ref && git name-rev HEAD",
        "git range-diff a...b && git diff-tree -r HEAD && git cherry origin/dev && git version && git help log",
        "git --no-pager diff --stat",
        'git diff "$(git merge-base HEAD origin/dev)"',
        "git",
        "echo \"a > b\" && printf '%s\\n' 'x>y'",
        "echo 'it''s $(rm app.py)'",
        "make test > /dev/null 2>&1",
        "ls > /dev/stdout; ls > /dev/stderr; ls > /dev/tty; ls 2>&-; ls >&2",
        "gh pr view 12 --json body > ~/scratchpad/pr.json",
        "gh pr comment 12 --body-file ~/scratchpad/review.md",
        "gh pr diff 12 | grep -n tool_name",
        "gh -R owner/repo pr view 12",
        'agentihooks msg send master@swarm "done > next"',
        "cd ~/scratchpad && echo x > notes.md",
        "cp app.py ~/scratchpad/app.py",
        "cp -t ~/scratchpad/dir app.py",
        "rsync -a app.py ~/scratchpad/",
        "dd if=app.py of=~/scratchpad/copy",
        "mkdir -p ~/scratchpad/t && touch ~/scratchpad/t/n.md && rm ~/scratchpad/t/n.md",
        "sed -n 1,20p app.py",
        "sed -i 's/a/b/' ~/scratchpad/n.md",
        "sed -i -e 's/a/b/' ~/scratchpad/n.md",
        "perl -pi -e 's/a/b/' ~/scratchpad/n.md",
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
    assert _found(home, command) == ""


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
def test_a_placeholder_never_counts_as_a_seat_folder(home: Path) -> None:
    scratch = home / "scratchpad"

    assert _found(home, "find /repo -exec rm {} \\;", scratch) == f"writes {UNKNOWN}"
    assert _found(home, "git ls-files | xargs rm", scratch) == f"writes {UNKNOWN}"
    assert _found(home, "patch -p1 < fix.diff", scratch) == f"writes {UNKNOWN}"


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
def test_a_bare_cd_moves_to_the_home_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    elsewhere, roots = tmp_path / "elsewhere", [(tmp_path / "home").resolve()]

    assert read_only_shell.write_reason("cd && touch notes.md", elsewhere, roots) == ""
    assert read_only_shell.write_reason("touch notes.md", elsewhere, roots) == "writes notes.md"


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
def test_a_reviewer_appends_to_its_task_folder_with_a_heredoc(home: Path) -> None:
    command = f"cat >> {_task_folder(home)}/proof.md <<'EOF'\nline with > and it's quoted\nEOF"
    other = command.replace("task1/proof.md", "task2/proof.md")

    assert _reason(home, command) == ""
    assert _reason(home, other) == (
        f"reviewer cannot write through the shell: the command writes {_task_folder(home).parent}/task2/proof.md{TAIL}"
    )


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
@pytest.mark.parametrize("role", ["master", "planner", "qa", "reviewer"])
def test_edit_tool_payloads_keep_the_edit_path(home: Path, role: str) -> None:
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(home / "repo" / "app.py")}, "cwd": str(home)}
    scratch = {"tool_name": "Write", "tool_input": {"file_path": str(home / "scratchpad" / "n.md")}, "cwd": str(home)}
    refusal = f"{role} cannot edit outside ~/scratchpad; send the change to its author."

    assert no_code_edits.deny_reason(edit, role, home) == refusal
    assert no_code_edits.deny_reason(scratch, role, home) == ""
