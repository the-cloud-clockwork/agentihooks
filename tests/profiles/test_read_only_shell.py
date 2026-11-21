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
        ("X=../dev/x; rm ~/scratchpad/$X", "~/scratchpad/$X"),
        ("rm ~/scratchpad/$(echo ../dev/x)", "~/scratchpad/$(echo ../dev/x)"),
        ("rm ~/scratchpad/`echo x`", "~/scratchpad/`echo x`"),
        ("rm -rf ~/scratchpad/.*", "~/scratchpad/.*"),
        ("rm -rf ~/scratchpad/.?", "~/scratchpad/.?"),
        ("rm -rf ~/scratchpad/.[a-z]*", "~/scratchpad/.[a-z]*"),
        ("(cd ~/scratchpad); touch app.py", "app.py"),
        ("cd ~/scratchpad | true; touch app.py", "app.py"),
        ("cd ~/scratchpad & touch app.py", "app.py"),
        ("cd $X && touch notes.md", "notes.md"),
        ("cd -P /tmp && echo x > out.txt", "out.txt"),
        ("pushd /tmp && echo x > out.txt", "out.txt"),
        ("cd ~/scratchpad && popd && touch notes.md", "notes.md"),
        ("pushd && touch notes.md", "notes.md"),
        ("cd ~/scratchpad && cd - && touch notes.md", "notes.md"),
        ("cd ~/scratchpad && pushd +1 && touch notes.md", "notes.md"),
        ("cd ~/scratchpad && echo x > $X/../../repo/app.py", "$X/../../repo/app.py"),
        ("echo x > $(echo /repo)/app.py", "$(echo /repo)/app.py"),
        ('echo $(echo ")" ; rm app.py)', "app.py"),
        ("cat <<EOF\n$(rm app.py)\nEOF", "app.py"),
        ("setsid rm app.py", "app.py"),
        ("builtin rm app.py", "app.py"),
        ("stdbuf -oL rm app.py", "app.py"),
        ("stdbuf -o L rm app.py", "app.py"),
        ("stdbuf -i 0 rm app.py", "app.py"),
        ("stdbuf -e 0 rm app.py", "app.py"),
        ("ionice -c 3 rm app.py", "app.py"),
        ("ionice -n 7 rm app.py", "app.py"),
        ("ionice -p 1 rm app.py", "app.py"),
        ("flock /tmp/lock rm app.py", "app.py"),
        ("flock -w 5 /tmp/lock rm app.py", "app.py"),
        ("flock -E 1 /tmp/lock rm app.py", "app.py"),
        ("flock --timeout 5 /tmp/lock rm app.py", "app.py"),
        ("flock --conflict-exit-code 1 /tmp/lock rm app.py", "app.py"),
        ("find . -fprint app.py", "app.py"),
        ("find . -fprint0 app.py", "app.py"),
        ("find . -fls app.py", "app.py"),
        ("find . -fprintf app.py %p", "app.py"),
        ("sort -o app.py x", "app.py"),
        ("sort --output=app.py x", "app.py"),
        ("sort --output app.py x", "app.py"),
        ("uniq a app.py", "app.py"),
        ("gzip app.py", "app.py"),
        ("gunzip app.py", "app.py"),
        ("bzip2 app.py", "app.py"),
        ("bunzip2 app.py", "app.py"),
        ("xz app.py", "app.py"),
        ("unxz app.py", "app.py"),
        ("zstd app.py", "app.py"),
        ("unzstd app.py", "app.py"),
        ("curl -o app.py https://example.com", "app.py"),
        ("curl --output app.py https://example.com", "app.py"),
        ("curl -O https://example.com/a", UNKNOWN),
        ("curl --remote-name https://example.com/a", UNKNOWN),
        ("curl --remote-name-all https://example.com/a", UNKNOWN),
        ("wget https://example.com/a", UNKNOWN),
        ("wget -O app.py https://example.com", "app.py"),
        ("wget --output-document app.py https://example.com", "app.py"),
        ("tar -C /repo -xf x.tar", UNKNOWN),
        ("tar xf x.tar", UNKNOWN),
        ("tar -czf out.tgz src", UNKNOWN),
        ("tar -rf a.tar f", UNKNOWN),
        ("tar -uf a.tar f", UNKNOWN),
        ("tar -Af a.tar b.tar", UNKNOWN),
        ("tar --extract -f x.tar", UNKNOWN),
        ("tar --get -f x.tar", UNKNOWN),
        ("tar --create -f x.tar src", UNKNOWN),
        ("tar --append -f x.tar f", UNKNOWN),
        ("tar --update -f x.tar f", UNKNOWN),
        ("tar --delete -f x.tar f", UNKNOWN),
        ("tar --concatenate -f a.tar b.tar", UNKNOWN),
        ("unzip x.zip", UNKNOWN),
        ("unzip -d /repo x.zip", UNKNOWN),
        ("touch ~nosuchuser9/x", "~nosuchuser9/x"),
        ("touch a\x00b", "a\x00b"),
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
        ("git ls-files | xargs -0 rm", UNKNOWN),
        ("echo '' > app.py", "app.py"),
        ('echo "\\"" > app.py', "app.py"),
        ("echo 'x\\' > app.py", "app.py"),
        ("echo 'x'>app.py", "app.py"),
        ("echo $(true)>app.py", "app.py"),
        ("echo \\x$(rm app.py)", "app.py"),
        ("echo x\\y>app.py", "app.py"),
        ("rm $(echo a; echo b)", "$(echo a; echo b)"),
        ("diff <(rm app.py) x", "app.py"),
        ("ls | cd ~/scratchpad; touch notes.md", "notes.md"),
        ("ls >&1X", "1X"),
        ("sort --output=app=1.py x", "app=1.py"),
        ("sed --expression=s/a/b/ -i app.py", "app.py"),
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
        ("/bin/r? app.py", "runs a command named by /bin/r?"),
        ("/bin/r* app.py", "runs a command named by /bin/r*"),
        ("/bin/[r]m app.py", "runs a command named by /bin/[r]m"),
        ("env -C /repo rm app.py", "runs env -C, which hides the folder or the command it runs"),
        ("env --chdir=/repo rm app.py", "runs env --chdir=/repo, which hides the folder or the command it runs"),
        ("env -S 'rm app.py'", "runs env -S, which hides the folder or the command it runs"),
        ("env --split-string x", "runs env --split-string, which hides the folder or the command it runs"),
        ("gh pr co 12", "runs gh pr co"),
        ("gh --hostname github.com pr checkout 12", "runs gh pr checkout"),
        ("gh --hostname=github.com pr checkout 12", "runs gh pr checkout"),
        ("git stash push", "runs git stash"),
        ("git branch -a fix", "runs git branch"),
        ("git branch -d fix", "runs git branch"),
        ("git tag -d v1", "runs git tag"),
        ("git remote add o https://example.com/r.git", "runs git remote"),
        ("git config user.name x", "runs git config"),
        ("git config", "runs git config"),
        ("git reflog expire --all", "runs git reflog"),
        ("git submodule update", "runs git submodule"),
        ("git worktree", "runs git worktree"),
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
        "rm ~/scratchpad/*.md",
        "git branch --show-current && git branch -a && git branch -r -v && git branch -vv && git branch -l",
        "git branch --list && git branch --all && git branch --remotes && git branch",
        "git tag -l && git tag --list && git tag",
        "git remote -v && git remote get-url origin && git remote show origin && git remote",
        "git config --get user.name && git config --get-all x && git config --get-regexp x",
        "git config --list && git config -l",
        "git stash list && git stash show && git worktree list && git reflog show && git reflog",
        "git submodule status && git submodule",
        "patch --dry-run -p1 < fix.diff",
        "gzip -c app.py && gzip -l a.gz && gzip -t a.gz && gzip -dc a.gz",
        "gzip --stdout app.py && gunzip --to-stdout a.gz && xz --list a.xz && zstd --test a.zst",
        "curl -o - https://example.com && curl -s https://example.com",
        "wget -O - https://example.com && wget -qO- https://example.com",
        "wget --spider https://example.com && wget --output-document=- https://example.com",
        "tar -tvf x.tar && tar tf x.tar && tar --list --file=x.tar",
        "unzip -l x.zip && unzip -t x.zip && unzip -p x.zip a && unzip -v x.zip && unzip -Z x.zip && unzip -z x.zip",
        "find . -name '*.py' -print",
        "sort -k 2 x && uniq -c x",
        "[ -f app.py ] && [[ -d src ]]",
        "env | grep PATH && env FOO=1 ls",
        "grep -c x < app.py",
        'echo "x\\" && rm app.py"',
        "cat <<'EOF'\n$(rm app.py)\nEOF",
        "cd -P ~/scratchpad && touch notes.md",
        "pushd ~/scratchpad && touch notes.md",
        "(cd ~/scratchpad && touch notes.md)",
        "cd ~/scratchpad || true; touch notes.md",
        "cd ~/scratchpad >| /dev/null; touch notes.md",
        "cd ~/scratchpad 2>&1; touch notes.md",
        "cd ~/scratchpad &> /dev/null; touch notes.md",
        "cd ~/scratchpad <&0; touch notes.md",
        "ls); cd ~/scratchpad && touch notes.md",
        "cd ~/scratchpad && ls | grep x; touch notes.md",
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
        "'a > b'",
        "echo $(true)$HOME",
        'echo "\\$(rm app.py)"',
        "echo x\\>y",
        "rm ~/scratchpad/x 2>/dev/null",
        "rm ~/scratchpad/x 0</dev/null",
        "ls 2>&1-",
        "cd ../scratchpad && touch notes.md",
        "sed -i --expression s/a/b/ --expression s/c/d/ ~/scratchpad/n.md",
        "sed -i --file a.sed --file b.sed ~/scratchpad/n.md",
        "sed -i -e s/a/b/ -e s/c/d/ ~/scratchpad/n.md",
        "sed -i -ne s/a/b/ -ne s/c/d/ ~/scratchpad/n.md",
        "sed -i -e",
        "tar",
        "tar --exclude=x a.tar",
        "uniq a ~/scratchpad/out extra",
        "cp app.py b.py ~/scratchpad/d/",
        "bash check.sh 'rm app.py'",
        "sudo -u",
        "git ls-files | xargs -I",
        "gh pr view -R",
        "git -C",
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
def test_a_bare_pushd_or_popd_leaves_the_folder_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    elsewhere, roots = tmp_path / "elsewhere", [(tmp_path / "home").resolve()]

    assert read_only_shell.write_reason("pushd && touch notes.md", elsewhere, roots) == "writes notes.md"
    assert read_only_shell.write_reason("popd && touch notes.md", elsewhere, roots) == "writes notes.md"


@pytest.mark.feature("read-only-roles-refuse-shell-writes")
def test_a_nested_script_judges_relative_paths_from_the_shell_folder(home: Path) -> None:
    scratch = home / "scratchpad"

    assert _found(home, "bash -c 'touch notes.md'", scratch) == ""
    assert _found(home, "bash -c 'touch notes.md'") == "writes notes.md"


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
