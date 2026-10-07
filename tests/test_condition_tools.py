"""Session-created conditions: the operator-prompt gate, the write guard, the
runtime and directory layers, repo trust, and the agentihooks condition tools."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import hooks.hook_manager as hm
from hooks.context import conditions, ledger_request, profile_chain
from hooks.hook_manager import BlockAction

pytestmark = pytest.mark.unit

SID = "sid-condition-tools"
_CONDITIONS_DOC = (Path(__file__).resolve().parents[1] / "docs" / "hooks" / "conditions.md").read_text()


def _git_repo(path: Path, remote: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


def _state(bundle: Path | None, profile: str = "alpha") -> None:
    state = profile_chain.state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    data = {"targets": {"global": {"claude": {"profile": profile}}}}
    if bundle is not None:
        data["bundle"] = {"path": str(bundle)}
    state.write_text(json.dumps(data))


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = _git_repo(tmp_path / "bundle", "git@github.com:the-cloud-clockwork/agentihooks-bundle.git")
    (root / "profiles" / "alpha").mkdir(parents=True)
    (root / "profiles" / "beta").mkdir(parents=True)
    _state(root, "alpha,beta")
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr("hooks.config.CONDITIONS_ENABLED", True)
    return root


class TestSignal:
    @pytest.mark.parametrize(
        "prompt",
        [
            "set a condition: after every kubectl call remind me of gitops",
            "please add a new condition for bash.git",
            "Remove the condition I added earlier",
            "create conditions for the edit tool",
            "update this condition",
            "set the no code edits conditions for master, planner and qa",
            "add the no-code-edits conditions",
            "remove the git guard condition",
            "set up a new kubectl gitops reminder condition",
            "set the cd one proof condition",
            "add the brand new guard condition",
        ],
    )
    def test_arms(self, prompt):
        assert conditions.contains_condition_signal(prompt)

    @pytest.mark.parametrize(
        "prompt",
        [
            "don't add a condition for this",
            "agents will NOT create conditions",
            "conditions are added to the attached bundle",
            "what are conditions?",
            "can the hooks mcp crate also the new conditions?",
            "",
            "don't set the no code edits conditions",
            "update the docs about conditions",
            "fix the tests that check conditions",
            "write tests for the new conditions",
            "make sure the hook conditions hold",
            "set the five words long name of the conditions",
            "add seven eight nine ten eleven conditions",
        ],
    )
    def test_does_not_arm(self, prompt):
        assert not conditions.contains_condition_signal(prompt)


class TestGate:
    def test_arm_disarm(self):
        assert not conditions.is_armed(SID)
        conditions.arm_gate(SID)
        assert conditions.is_armed(SID)
        conditions.disarm_gate(SID)
        assert not conditions.is_armed(SID)

    def test_expires(self):
        conditions.arm_gate(SID)
        past = time.time() - 7200
        os.utime(conditions._gate_path(SID), (past, past))
        assert not conditions.is_armed(SID)

    def test_prompt_arms_and_stop_disarms(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
        monkeypatch.setattr("hooks._async.fork_and_call", lambda *a, **k: None)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": SID, "cwd": "/tmp"}
        hm.on_user_prompt_submit({**payload, "prompt": "continue with the tests"})
        assert not conditions.is_armed(SID)
        hm.on_user_prompt_submit({**payload, "prompt": "set a condition that blocks rm -rf"})
        assert conditions.is_armed(SID)
        hm.on_stop({"hook_event_name": "Stop", "session_id": SID, "cwd": "/tmp", "transcript_path": ""})
        assert not conditions.is_armed(SID)

    def test_the_gate_records_the_source_that_opened_it(self, monkeypatch):
        logged = []
        monkeypatch.setattr("hooks.common.log", lambda message, payload=None: logged.append((message, payload)))
        assert conditions.gate_source(SID) == {}
        conditions.arm_gate(SID, "relay", "c-1")
        assert conditions.gate_source(SID) == {"source": "relay", "ref": "c-1"}
        assert logged == [("conditions: operator gate opened", {"session_id": SID, "source": "relay", "ref": "c-1"})]
        conditions.arm_gate(SID)
        assert conditions.gate_source(SID) == {"source": "typed", "ref": ""}
        past = time.time() - 7200
        os.utime(conditions._gate_path(SID), (past, past))
        assert conditions.gate_source(SID) == {}

    @pytest.mark.parametrize("content", ["1791290000", "{not json"])
    def test_a_gate_file_without_a_record_names_no_source(self, content):
        conditions.arm_gate(SID)
        conditions._gate_path(SID).write_text(content)
        assert conditions.is_armed(SID)
        assert conditions.gate_source(SID) == {}

    def test_only_a_prompt_the_operator_typed_opens_it(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
        monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "eng-1@demo")
        monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
        monkeypatch.setattr("hooks._async.fork_and_call", lambda *a, **k: None)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": SID, "cwd": "/tmp"}
        request = "set the no code edits conditions for master, planner and qa"
        hm.on_user_prompt_submit({**payload, "prompt": f"You are eng-1. Your task: {request}"})
        assert not conditions.is_armed(SID)
        hm.on_user_prompt_submit({**payload, "prompt": f"<task-notification> OPERATOR comment: {request}"})
        assert not conditions.is_armed(SID)
        hm.on_user_prompt_submit({**payload, "prompt": request})
        assert conditions.gate_source(SID) == {"source": "typed", "ref": ""}

    def test_a_watch_event_never_opens_it_in_a_plain_session(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
        monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
        monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
        monkeypatch.setattr("hooks._async.fork_and_call", lambda *a, **k: None)
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": SID, "cwd": "/tmp"}
        hm.on_user_prompt_submit({**payload, "prompt": "<task-notification> set a condition that blocks rm -rf"})
        assert not conditions.is_armed(SID)
        hm.on_user_prompt_submit({**payload, "prompt": "set a condition that blocks rm -rf"})
        assert conditions.gate_source(SID) == {"source": "typed", "ref": ""}


class TestWriteGuard:
    @pytest.mark.parametrize(
        "tool, tool_input",
        [
            ("mcp__agentihooks__condition_set", {"step": "pre"}),
            ("mcp__agentihooks__condition_clear", {"file": "x"}),
            ("Write", {"file_path": "/b/.claude/conditions/pre-bash-x.sh", "content": "exit 2"}),
            ("Edit", {"file_path": "/r/.agentihooks/conditions/pre-any-x.py", "old_string": "a", "new_string": "b"}),
            ("Bash", {"command": "cat > ~/.agentihooks/conditions/pre-any-x.sh <<'EOF'\nexit 0\nEOF"}),
            ("Bash", {"command": "python3 -c \"open('.claude/conditions/pre-a-b.sh','w')\""}),
            ("Bash", {"command": "cd ~/.agentihooks && tee conditions/pre-any-x.sh < /tmp/x"}),
            ("Bash", {"command": "rm .claude/conditions/pre-bash-x.sh"}),
            ("Bash", {"command": "find .claude/conditions -name '*.sh' -delete"}),
            ("Bash", {"command": "sed -i s/a/b/ .claude/conditions/pre-bash-x.sh"}),
            ("Bash", {"command": "sed -ni s/a/b/p .claude/conditions/pre-bash-x.sh"}),
            ("Bash", {"command": "sed --in-place s/a/b/ .claude/conditions/pre-bash-x.sh"}),
            ("Bash", {"command": "sed -n '1,9w .claude/conditions/pre-bash-y.sh' /tmp/x"}),
            ("Bash", {"command": "sed 's/a/b/w .claude/conditions/pre-bash-y.sh' /tmp/x"}),
            ("Bash", {"command": "git show HEAD:a.sh > .claude/conditions/pre-bash-x.sh"}),
            ("Bash", {"command": "git checkout dev -- .claude/conditions/pre-bash-x.sh"}),
            ("Bash", {"command": "touch .claude/conditions/pre-bash-x.sh"}),
            ("Bash", {"command": "cp /tmp/x .claude/conditions/pre-bash-x.sh"}),
            ("Bash", {"command": "for f in a b; do cp $f .claude/conditions/; done"}),
        ],
    )
    def test_blocked_until_armed(self, tool, tool_input):
        assert conditions.write_guard(tool, tool_input, SID) == conditions.GATE_MESSAGE
        conditions.arm_gate(SID)
        assert conditions.write_guard(tool, tool_input, SID) is None

    @pytest.mark.parametrize(
        "tool, tool_input",
        [
            ("Bash", {"command": "ls -la .claude/conditions"}),
            ("Bash", {"command": "cat .claude/conditions/pre-bash-x.sh 2>/dev/null | head"}),
            ("Bash", {"command": "git add .claude/conditions/pre-bash-x.sh && git commit -m 'add condition'"}),
            ("Bash", {"command": "agentihooks conditions list --tool Bash"}),
            ("Write", {"file_path": "/repo/hooks/context/conditions.py", "content": "x"}),
            ("Write", {"file_path": "/repo/docs/guide.md", "content": _CONDITIONS_DOC}),
            (
                "Bash",
                {"command": 'W="$(readlink -f ~/.claude/skills/wt.sh)"; $W new x && sed -i s/a/b/ hooks/conditions.py'},
            ),
            ("Edit", {"file_path": "/repo/docs/hooks/conditions.md", "old_string": "a", "new_string": "b"}),
            ("mcp__agentihooks__condition_list", {}),
        ],
    )
    def test_reads_and_unrelated_paths_pass(self, tool, tool_input):
        assert conditions.write_guard(tool, tool_input, SID) is None

    @pytest.mark.parametrize(
        "command",
        [
            "git show origin/dev:profiles/m/.claude/conditions/pre-bash-x.sh | sed -n 1,40p",
            "sed -n '/write/,/^$/p' .claude/conditions/pre-bash-x.sh",
            "sed -E 's/a/b/' .claude/conditions/pre-bash-x.sh",
            "nl .claude/conditions/pre-bash-x.sh",
            "cut -c1-80 .claude/conditions/pre-bash-x.sh",
            "ls .claude/conditions | tr '\\n' ' '",
            'for f in .claude/conditions/*; do echo "== $f"; cat "$f"; done',
            "if test -d .claude/conditions; then ls .claude/conditions; fi",
            "printf '%s\\n' x; printenv HOME; date; ls -la ~/.agentihooks/conditions/.gate/",
            "basename .claude/conditions/a.sh; dirname .claude/conditions/a.sh",
            "realpath .claude/conditions/a.sh; readlink -f .claude/conditions/a.sh",
            "sha256sum .claude/conditions/a.sh; md5sum .claude/conditions/a.sh",
            "true && ls .claude/conditions",
            "git branch --show-current && git ls-files profiles/e/.claude/conditions",
            "git rev-parse HEAD && git ls-tree HEAD .claude/conditions/",
            "git cat-file -p HEAD:.claude/conditions/a.sh",
            "git grep -n exit -- .claude/conditions",
        ],
    )
    def test_read_only_commands_on_condition_files_pass(self, command):
        assert conditions.write_guard("Bash", {"command": command}, SID) is None

    def test_pre_tool_use_blocks_the_mcp_call(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
        with pytest.raises(BlockAction, match="operator's own prompt"):
            hm.on_pre_tool_use(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": SID,
                    "tool_name": "mcp__agentihooks__condition_set",
                    "tool_input": {"step": "pre"},
                    "cwd": "/tmp",
                }
            )

    @staticmethod
    def _task_comments(tmp_path, monkeypatch, comments, **task):
        monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
        monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
        monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t1")
        members = {"master@a1-1": {"role": "orchestrator"}}
        tasks = [{"id": "t1", "comments": comments, **task}, {"id": "t2", "comments": []}]
        ledger = {"tasks": tasks, "_meta": {"members": members}}
        (tmp_path / "demo.json").write_text(json.dumps(ledger))

    def test_an_operator_comment_on_the_agents_task_opens_it(self, tmp_path, monkeypatch):
        at = int(time.time() * 1000)
        comment = {"id": "c-7", "by": "operator", "at": at, "text": "set the no code edits conditions"}
        self._task_comments(tmp_path, monkeypatch, [comment])
        assert conditions.write_guard("mcp__agentihooks__condition_set", {"step": "pre"}, SID) is None
        assert conditions.gate_source(SID) == {"source": "ledger", "ref": "c-7"}

    def test_an_approval_older_than_thirty_minutes_opens_it_for_its_task_until_the_task_closes(
        self, tmp_path, monkeypatch
    ):
        request = "set the no code edits conditions"
        marks = {"relayed_by": "master@a1-1", "relayed_from": "master pane", "quote": request}
        relayed = {"id": "c-8", "by": "operator", "at": int((time.time() - 7200) * 1000), "text": request, **marks}
        write = ("Write", {"file_path": "/b/.claude/conditions/pre-bash-x.sh", "content": "exit 2"})
        self._task_comments(tmp_path, monkeypatch, [relayed])
        assert conditions.write_guard(*write, SID) is None
        assert conditions.gate_source(SID) == {"source": "relay", "ref": "c-8"}
        conditions.disarm_gate(SID)
        monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t2")
        assert conditions.write_guard(*write, SID) == conditions.GATE_MESSAGE
        monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t1")
        self._task_comments(tmp_path, monkeypatch, [relayed], state="done", done=True)
        assert conditions.write_guard(*write, SID) == conditions.GATE_MESSAGE
        assert not conditions.is_armed(SID)

    def test_a_failed_lookup_refuses(self, monkeypatch):
        def broken(asks):
            raise RuntimeError("redis went away")

        monkeypatch.setattr(ledger_request, "find", broken)
        assert conditions.write_guard("mcp__agentihooks__condition_set", {}, SID) == conditions.GATE_MESSAGE


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def checkouts(tmp_path, bundle):
    primary = _git_repo(tmp_path / "repo")
    _git(primary, "checkout", "-q", "-b", "dev")
    _git(primary, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    worktree = tmp_path / "wt"
    _git(primary, "worktree", "add", "-q", "-b", "feat", str(worktree))
    on_dev = tmp_path / "wt-dev"
    _git(primary, "worktree", "add", "-q", "-b", "main", str(on_dev))
    _git(bundle, "checkout", "-q", "-b", "bundle-feat")
    return {"primary": primary, "worktree": worktree, "on_dev": on_dev, "bundle": bundle, "tmp": tmp_path}


class TestLiveFolders:
    @pytest.mark.parametrize(
        "tool, make_input, cwd",
        [
            ("Write", lambda c: {"file_path": f"{c['worktree']}/.claude/conditions/pre-bash-x.sh"}, None),
            ("Edit", lambda c: {"file_path": f"{c['worktree']}/profiles/a/.claude/conditions/pre-any-x.py"}, None),
            ("Bash", lambda c: {"command": f"cp /tmp/x {c['worktree']}/.claude/conditions/pre-bash-x.sh"}, "primary"),
            ("Bash", lambda c: {"command": f"{c['worktree']}/.claude/conditions/pre-bash-x.sh"}, None),
            ("Edit", lambda c: {"file_path": ".claude/conditions/pre-any-x.py"}, "worktree"),
            ("Bash", lambda c: {"command": "cd profiles && touch a/.claude/conditions/pre-a-b.sh"}, "worktree"),
            ("Bash", lambda c: {"command": "cat > .claude/conditions/pre-bash-x.sh <<'EOF'\nexit 0\nEOF"}, "worktree"),
            ("Bash", lambda c: {"command": "python3 -c \"open('.claude/conditions/pre-a-b.sh','w')\""}, "worktree"),
            (
                "Bash",
                lambda c: {"command": f"cd {c['worktree']} && chmod +x .claude/conditions/pre-a-b.sh"},
                "worktree",
            ),
            ("Bash", lambda c: {"command": "rm .agentihooks/conditions/pre-bash-x.sh"}, "worktree"),
        ],
    )
    def test_a_write_into_a_worktree_conditions_folder_passes(self, checkouts, tool, make_input, cwd):
        base = str(checkouts[cwd]) if cwd else None
        assert conditions.write_guard(tool, make_input(checkouts), SID, base) is None

    @pytest.mark.parametrize(
        "tool, make_input, cwd",
        [
            ("Write", lambda c: {"file_path": f"{c['bundle']}/.claude/conditions/pre-bash-x.sh"}, None),
            ("Write", lambda c: {"file_path": f"{c['bundle']}/profiles/alpha/.claude/conditions/pre-bash-x.sh"}, None),
            ("Write", lambda c: {"file_path": f"{conditions.runtime_dir()}/pre-bash-x.sh"}, None),
            ("Write", lambda c: {"file_path": f"{c['primary']}/.claude/conditions/pre-bash-x.sh"}, None),
            ("Write", lambda c: {"file_path": f"{c['on_dev']}/.claude/conditions/pre-bash-x.sh"}, None),
            ("Write", lambda c: {"file_path": f"{c['tmp']}/plain/.claude/conditions/pre-bash-x.sh"}, None),
            ("Edit", lambda c: {"file_path": ".claude/conditions/pre-bash-x.sh"}, "primary"),
            ("Bash", lambda c: {"command": "touch .claude/conditions/pre-bash-x.sh"}, "primary"),
            ("Bash", lambda c: {"command": "touch .claude/conditions/pre-bash-x.sh"}, None),
            ("Write", lambda c: {"content": "x", "target": f"{c['worktree']}/.claude/conditions/a.sh"}, "worktree"),
            (
                "Bash",
                lambda c: {"command": f"cp {c['bundle']}/.claude/conditions/x {c['worktree']}/.claude/conditions/"},
                "worktree",
            ),
            ("Bash", lambda c: {"command": f"cp {c['worktree']}/.claude/conditions/x $D/.claude/conditions/"}, None),
            (
                "Bash",
                lambda c: {"command": f"rsync -a {c['worktree']}/.claude/conditions {c['bundle']}/.claude/"},
                None,
            ),
            (
                "Bash",
                lambda c: {"command": f"cp {c['worktree']}/.claude/conditions/x {conditions.runtime_dir()}"},
                None,
            ),
            ("Bash", lambda c: {"command": f"cd {c['bundle']} && touch .claude/conditions/x"}, "worktree"),
            ("Bash", lambda c: {"command": "cd - && touch .claude/conditions/x"}, "worktree"),
            ("Bash", lambda c: {"command": f"cp x --target-directory={c['bundle']}/.claude/conditions"}, "worktree"),
        ],
    )
    def test_a_write_into_a_live_conditions_folder_needs_the_operators_words(self, checkouts, tool, make_input, cwd):
        base = str(checkouts[cwd]) if cwd else None
        tool_input = make_input(checkouts)
        assert conditions.write_guard(tool, tool_input, SID, base) == conditions.GATE_MESSAGE
        conditions.arm_gate(SID)
        assert conditions.write_guard(tool, tool_input, SID, base) is None

    @pytest.mark.parametrize("tool", ["mcp__agentihooks__condition_set", "mcp__agentihooks__condition_clear"])
    def test_condition_set_and_clear_need_the_operators_words_in_a_worktree(self, checkouts, tool):
        cwd = str(checkouts["worktree"])
        assert conditions.write_guard(tool, {"step": "pre"}, SID, cwd) == conditions.GATE_MESSAGE

    @pytest.mark.parametrize(
        "make_command",
        [
            lambda c: f"grep -n exit {conditions.runtime_dir()}/pre-any-x.sh >> {c['tmp']}/proof.md",
            lambda c: f"cat {c['bundle']}/.claude/conditions/pre-bash-x.sh > {c['tmp']}/notes.txt 2>&1",
            lambda c: f"ls {c['bundle']}/.claude/conditions | head >> ~/scratchpad/proof.md",
            lambda c: f"cat {c['bundle']}/.claude/conditions/pre-bash-x.sh > notes.txt",
            lambda c: f"echo checked .claude/conditions >> {c['worktree']}/.claude/conditions/notes.md",
        ],
    )
    def test_a_redirect_into_an_ordinary_file_passes(self, checkouts, make_command):
        assert (
            conditions.write_guard("Bash", {"command": make_command(checkouts)}, SID, str(checkouts["primary"])) is None
        )

    @pytest.mark.parametrize(
        "make_command",
        [
            lambda c: f"echo exit >> {c['bundle']}/.claude/conditions/pre-bash-x.sh",
            lambda c: f"cd {c['bundle']}/.claude/conditions && echo exit > pre-bash-x.sh",
            lambda c: f"cd {c['bundle']}/.claude && echo exit > conditions/pre-bash-x.sh",
            lambda c: f'cat {c["bundle"]}/.claude/conditions/a.sh > "$OUT"',
            lambda c: "echo exit > .claude/conditions/pre-bash-x.sh",
            lambda c: f'echo exit > "{c["bundle"]}/.claude/conditions/pre-bash-x.sh"',
            lambda c: f'echo exit > {c["bundle"]}/".claude"/conditions/pre-bash-x.sh',
            lambda c: f"echo exit > {c['bundle']}/.cla\\ude/conditions/pre-bash-x.sh",
            lambda c: f'cp /tmp/x "{str(c["bundle"])[:-2]}"le/.claude/conditions/pre-bash-x.sh',
            lambda c: f"echo exit > `echo {c['bundle']}`/.claude/conditions/pre-bash-x.sh",
        ],
    )
    def test_a_redirect_into_a_live_conditions_folder_needs_the_operators_words(self, checkouts, make_command):
        command = make_command(checkouts)
        assert conditions.write_guard("Bash", {"command": command}, SID, str(checkouts["primary"])) == (
            conditions.GATE_MESSAGE
        )

    def test_the_base_branch_setting_and_origins_head_are_live(self, checkouts, monkeypatch):
        write = {"file_path": f"{checkouts['worktree']}/.claude/conditions/pre-bash-x.sh"}
        monkeypatch.setenv("WT_BASE_BRANCH", "feat")
        assert conditions.write_guard("Write", write, SID) == conditions.GATE_MESSAGE
        monkeypatch.setenv("WT_BASE_BRANCH", "dev")
        assert conditions.write_guard("Write", write, SID) is None
        primary = checkouts["primary"]
        _git(primary, "update-ref", "refs/remotes/origin/feat", "HEAD")
        _git(primary, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/feat")
        assert conditions.write_guard("Write", write, SID) == conditions.GATE_MESSAGE

    def test_a_detached_head_is_live(self, checkouts):
        primary = checkouts["primary"]
        _git(primary, "update-ref", "refs/remotes/origin/main", "HEAD")
        _git(primary, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        _git(checkouts["worktree"], "checkout", "-q", "--detach")
        write = {"file_path": f"{checkouts['worktree']}/.claude/conditions/pre-bash-x.sh"}
        assert conditions.write_guard("Write", write, SID) == conditions.GATE_MESSAGE

    def test_a_branch_lookup_that_times_out_is_live(self, checkouts, monkeypatch):
        real_run = subprocess.run

        def slow_head(argv, **kwargs):
            if argv[-1] == "HEAD":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return real_run(argv, **kwargs)

        monkeypatch.setattr(conditions.subprocess, "run", slow_head)
        write = {"file_path": f"{checkouts['worktree']}/.claude/conditions/pre-bash-x.sh"}
        assert conditions.write_guard("Write", write, SID) == conditions.GATE_MESSAGE

    def test_every_branch_lookup_is_bounded_in_time(self, checkouts, monkeypatch):
        real_run, timeouts = subprocess.run, []

        def timed(argv, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return real_run(argv, **kwargs)

        monkeypatch.setattr(conditions.subprocess, "run", timed)
        conditions.write_guard("Write", {"file_path": f"{checkouts['worktree']}/.claude/conditions/a.sh"}, SID)
        assert timeouts and all(t is not None and 0 < t <= 5 for t in timeouts)

    def test_a_chained_profile_folder_not_created_yet_is_live(self, checkouts):
        _state(checkouts["bundle"], "alpha,gamma")
        write = {"file_path": f"{checkouts['bundle']}/profiles/gamma/.claude/conditions/pre-bash-x.sh"}
        assert conditions.write_guard("Write", write, SID) == conditions.GATE_MESSAGE
        write = {"file_path": f"{checkouts['bundle']}/profiles/delta/.claude/conditions/pre-bash-x.sh"}
        assert conditions.write_guard("Write", write, SID) is None

    def test_without_git_every_conditions_path_is_live(self, checkouts, monkeypatch):
        def missing(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(conditions.subprocess, "run", missing)
        write = {"file_path": f"{checkouts['worktree']}/.claude/conditions/pre-bash-x.sh"}
        assert conditions.write_guard("Write", write, SID) == conditions.GATE_MESSAGE

    @pytest.mark.parametrize("home", ["$HOME", "${HOME}", "~"])
    def test_the_home_variable_resolves_to_the_home_folder(self, checkouts, monkeypatch, home):
        monkeypatch.setenv("HOME", str(checkouts["tmp"]))
        command = f"cp /tmp/x {home}/wt/.claude/conditions/pre-bash-x.sh"
        assert conditions.write_guard("Bash", {"command": command}, SID, str(checkouts["primary"])) is None

    def test_pre_tool_use_passes_the_session_cwd(self, checkouts, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
        seen = {}
        monkeypatch.setattr(conditions, "write_guard", lambda *args: seen.setdefault("args", args) and None)
        monkeypatch.setattr(conditions, "pre_effect", lambda payload: None)
        payload = {"session_id": SID, "tool_name": "Read", "tool_input": {"file_path": "/x"}, "cwd": "/w"}
        try:
            hm.on_pre_tool_use({"hook_event_name": "PreToolUse", **payload})
        except BlockAction:
            pass
        assert seen["args"] == ("Read", {"file_path": "/x"}, SID, "/w")


def _create(**overrides):
    args = {"step": "pre", "matcher": "bash.git", "name": "guard", "script": "exit 0", "session_id": SID}
    return conditions.create_condition(**{**args, **overrides})


class TestCreate:
    def test_refused_without_the_operator_gate(self, bundle):
        with pytest.raises(conditions.ConditionError, match="operator's own prompt"):
            _create()

    def test_global_goes_to_the_bundle_and_is_live(self, bundle):
        conditions.arm_gate(SID)
        result = _create(script="echo guarded")
        path = Path(result["path"])
        assert path == bundle / ".claude" / "conditions" / "pre-bash.git-guard.sh"
        assert result["layer"] == "bundle"
        assert path.read_text().startswith("#!/usr/bin/env bash\necho guarded")
        assert os.access(path, os.X_OK)
        found = conditions.matching("pre", "Bash", {"command": "git status"})
        assert [e["file"] for e in found] == ["pre-bash.git-guard.sh"]

    def test_global_without_a_bundle_goes_to_runtime(self, tmp_path, monkeypatch):
        _state(None)
        conditions.arm_gate(SID)
        result = _create()
        assert result["layer"] == "runtime"
        assert Path(result["path"]).parent == conditions.runtime_dir()

    def test_profile_defaults_to_first_of_chain_or_named(self, bundle):
        conditions.arm_gate(SID)
        first = _create(scope="profile")
        named = _create(scope="profile", profile="beta", name="other")
        assert first["layer"] == "profile:alpha"
        assert Path(first["path"]).parent == bundle / "profiles" / "alpha" / ".claude" / "conditions"
        assert named["layer"] == "profile:beta"
        with pytest.raises(conditions.ConditionError, match="not in the active chain"):
            _create(scope="profile", profile="gamma")

    def test_directory_scope_writes_into_the_repo(self, bundle, tmp_path):
        repo = _git_repo(tmp_path / "work")
        conditions.arm_gate(SID)
        result = _create(scope="directory", cwd=str(repo / "src"))
        assert Path(result["path"]).parent == repo / ".agentihooks" / "conditions"

    def test_existing_file_needs_replace(self, bundle):
        conditions.arm_gate(SID)
        _create()
        with pytest.raises(conditions.ConditionError, match="replace=true"):
            _create()
        assert _create(replace=True, script="echo two")["file"] == "pre-bash.git-guard.sh"

    @pytest.mark.parametrize(
        "overrides, error",
        [
            ({"matcher": "bad*"}, "invalid characters"),
            ({"name": "has-dash"}, "letters, digits"),
            ({"language": "ruby"}, "language"),
            ({"step": "start"}, "unknown step"),
            ({"step": "stop"}, "expected stop-<name>"),
            ({"script": "  "}, "empty"),
            ({"scope": "everywhere"}, "scope must be"),
        ],
    )
    def test_validation(self, bundle, overrides, error):
        conditions.arm_gate(SID)
        with pytest.raises(conditions.ConditionError, match=error):
            _create(**overrides)

    def test_python_and_async_names(self, bundle):
        conditions.arm_gate(SID)
        result = _create(language="python", run_async=True, script="print('x')")
        assert result["file"] == "pre-bash.git-guard.async.py"

    @pytest.mark.parametrize("matcher", ["", "any"])
    def test_a_stop_condition_takes_no_matcher(self, bundle, matcher):
        conditions.arm_gate(SID)
        result = _create(step="stop", matcher=matcher, name="idle")
        assert (result["file"], result["step"], result["matcher"]) == ("stop-idle.sh", "stop", "any")


class TestLayers:
    def test_directory_overrides_runtime_overrides_profile_overrides_bundle(self, bundle, tmp_path):
        repo = _git_repo(tmp_path / "work")
        dirs = [
            bundle / ".claude" / "conditions",
            bundle / "profiles" / "alpha" / ".claude" / "conditions",
            conditions.runtime_dir(),
            repo / ".agentihooks" / "conditions",
        ]
        for directory in dirs:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "pre-bash-same.sh").write_text("echo x")
        found = conditions.matching("pre", "Bash", {}, cwd=str(repo))
        assert [e["source"] for e in found] == ["directory"]

    def test_untrusted_repo_is_skipped(self, bundle, tmp_path):
        repo = _git_repo(tmp_path / "cloned", "https://github.com/someone-else/tool.git")
        (repo / ".agentihooks" / "conditions").mkdir(parents=True)
        (repo / ".agentihooks" / "conditions" / "pre-any-evil.sh").write_text("echo pwned")
        assert conditions.matching("pre", "Bash", {}, cwd=str(repo)) == []
        layer = next(l for l in conditions.inventory(repo)["layers"] if l["source"] == "directory")
        assert layer["untrusted_owner"] == "someone-else"

    def test_bundle_owner_and_env_owners_are_trusted(self, bundle, tmp_path, monkeypatch):
        mine = _git_repo(tmp_path / "mine", "git@github.com:the-cloud-clockwork/tcc-qitp.git")
        other = _git_repo(tmp_path / "other", "https://github.com/partner/x")
        for repo in (mine, other):
            (repo / ".agentihooks" / "conditions").mkdir(parents=True)
            (repo / ".agentihooks" / "conditions" / "pre-any-ok.sh").write_text("echo ok")
        assert len(conditions.matching("pre", "Read", {}, cwd=str(mine))) == 1
        assert conditions.matching("pre", "Read", {}, cwd=str(other)) == []
        monkeypatch.setattr("hooks.config.CONDITIONS_TRUSTED_OWNERS", "partner")
        assert len(conditions.matching("pre", "Read", {}, cwd=str(other))) == 1

    def test_repo_without_remote_is_trusted(self, bundle, tmp_path):
        repo = _git_repo(tmp_path / "local-only")
        (repo / ".agentihooks" / "conditions").mkdir(parents=True)
        (repo / ".agentihooks" / "conditions" / "pre-any-ok.sh").write_text("echo ok")
        assert len(conditions.matching("pre", "Read", {}, cwd=str(repo))) == 1


class TestRemove:
    def test_gated_and_removes(self, bundle):
        conditions.arm_gate(SID)
        path = Path(_create()["path"])
        conditions.disarm_gate(SID)
        with pytest.raises(conditions.ConditionError, match="operator's own prompt"):
            conditions.remove_condition(file=path.name, session_id=SID)
        conditions.arm_gate(SID)
        assert conditions.remove_condition(file=path.name, session_id=SID)["layer"] == "bundle"
        assert not path.exists()

    def test_ambiguous_name_needs_scope(self, bundle):
        conditions.arm_gate(SID)
        _create()
        _create(scope="profile")
        with pytest.raises(conditions.ConditionError, match="several layers"):
            conditions.remove_condition(file="pre-bash.git-guard.sh", session_id=SID)
        assert conditions.remove_condition(file="pre-bash.git-guard.sh", session_id=SID, scope="profile")


class _CaptureMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(func):
            self.tools[func.__name__] = func
            return func

        return register


@pytest.fixture
def tools():
    from hooks.mcp import conditions as mcp_conditions

    capture = _CaptureMCP()
    mcp_conditions.register(capture)
    return capture.tools


class TestMCPTools:
    def test_set_refused_then_created_listed_shown_cleared(self, bundle, tools, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = {"step": "post", "matcher": "bash.kubectl", "name": "gitops", "script": "echo gitops", "session_id": SID}
        refused = json.loads(tools["condition_set"](**args))
        assert refused["success"] is False and "operator's own prompt" in refused["error"]

        conditions.arm_gate(SID)
        created = json.loads(tools["condition_set"](**args))
        assert created["success"] and created["file"] == "post-bash.kubectl-gitops.sh"

        listed = json.loads(tools["condition_list"]())
        assert [c["file"] for c in listed["conditions"]] == ["post-bash.kubectl-gitops.sh"]
        assert {layer["source"] for layer in listed["layers"]} >= {"bundle", "runtime"}

        shown = json.loads(tools["condition_show"]("post-bash.kubectl-gitops.sh"))
        assert shown["script"].endswith("echo gitops\n")

        cleared = json.loads(tools["condition_clear"]("post-bash.kubectl-gitops.sh", SID))
        assert cleared["success"] and cleared["layer"] == "bundle"
