"""Session-created conditions: the operator-prompt gate, the write guard, the
runtime and directory layers, repo trust, and the agentihooks condition tools."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import hooks.hook_manager as hm
from hooks.context import conditions, ledger_request, operator_words, profile_chain
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
    def _task_comments(tmp_path, monkeypatch, comments):
        monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
        monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
        monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t1")
        members = {"master@a1-1": {"role": "orchestrator"}}
        ledger = {"tasks": [{"id": "t1", "comments": comments}], "_meta": {"members": members}}
        (tmp_path / "demo.json").write_text(json.dumps(ledger))

    def test_an_operator_comment_on_the_agents_task_opens_it(self, tmp_path, monkeypatch):
        at = int(time.time() * 1000)
        comment = {"id": "c-7", "by": "operator", "at": at, "text": "set the no code edits conditions"}
        self._task_comments(tmp_path, monkeypatch, [comment])
        assert conditions.write_guard("mcp__agentihooks__condition_set", {"step": "pre"}, SID) is None
        assert conditions.gate_source(SID) == {"source": "ledger", "ref": "c-7"}

    def test_a_master_relay_opens_it_only_with_the_operators_typed_words(self, tmp_path, monkeypatch):
        request = "set the no code edits conditions"
        marks = {"relayed_by": "master@a1-1", "relayed_from": "master pane", "quote": request}
        relayed = {"id": "c-8", "by": "operator", "at": int(time.time() * 1000), "text": request, **marks}
        self._task_comments(tmp_path, monkeypatch, [relayed])
        write = ("Write", {"file_path": "/b/.claude/conditions/pre-bash-x.sh", "content": "exit 2"})
        assert conditions.write_guard(*write, SID) == conditions.GATE_MESSAGE
        assert not conditions.is_armed(SID)
        operator_words.record("master@a1-1", request)
        assert conditions.write_guard(*write, SID) is None
        assert conditions.gate_source(SID) == {"source": "relay", "ref": "c-8"}

    def test_a_failed_lookup_refuses(self, monkeypatch):
        def broken(asks):
            raise RuntimeError("redis went away")

        monkeypatch.setattr(ledger_request, "find", broken)
        assert conditions.write_guard("mcp__agentihooks__condition_set", {}, SID) == conditions.GATE_MESSAGE


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
            ({"step": "stop"}, "unknown step"),
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
