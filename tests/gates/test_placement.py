"""The placement gate: on an editable install a write into a profile, condition, rule, hook or skill folder of the
package or the bundle waits for the operator's answer, and each answer opens only its own destination."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts.gates import entry, placement
from scripts.gates.base import Call, Who
from scripts.gates.placement import BUNDLE, PACKAGE, PlacementGate, editable_source

SID = "sid-placement"


class FakeDist:
    def __init__(self, direct_url):
        self.direct_url = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return None if self.direct_url is None else json.dumps(self.direct_url)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path):
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


@pytest.fixture
def world(tmp_path):
    pkg, bundle = _repo(tmp_path / "pkg"), _repo(tmp_path / "bundle")
    _git("worktree", "add", "-q", str(tmp_path / "wt"), cwd=pkg)
    gate = PlacementGate(source=lambda: pkg, bundle=lambda: bundle, home=tmp_path / "answers")
    return gate, tmp_path


def write(path, cwd="/"):
    return Call(tool="Write", tool_input={"file_path": str(path), "content": "x"}, cwd=str(cwd), session=SID)


def answer(home, *chosen):
    payload = {
        "tool_name": "AskUserQuestion",
        "session_id": SID,
        "tool_response": {"answers": {placement.QUESTION: ", ".join(chosen)}},
    }
    return placement.heard(payload, home)


class TestEditableDetection:
    def test_editable_install_names_its_source(self):
        dist = FakeDist({"url": "file:///src/agentihooks", "dir_info": {"editable": True}})
        assert editable_source(dist) == Path("/src/agentihooks")

    @pytest.mark.parametrize(
        "direct_url",
        [
            {"url": "file:///src/agentihooks", "dir_info": {}},
            {"url": "file:///src/agentihooks", "dir_info": {"editable": False}},
            {"url": "https://files.example/agentihooks.whl", "archive_info": {}},
            {"dir_info": {"editable": True}},
            None,
        ],
    )
    def test_non_editable_install_has_no_source(self, direct_url):
        assert editable_source(FakeDist(direct_url)) is None

    def test_the_installed_agentihooks_distribution_is_read(self, monkeypatch):
        asked = []
        dist = FakeDist({"url": "file:///src/agentihooks", "dir_info": {"editable": True}})
        monkeypatch.setattr(placement.metadata, "distribution", lambda name: asked.append(name) or dist)
        assert editable_source() == Path("/src/agentihooks")
        assert asked == ["agentihooks"]

    def test_a_missing_distribution_has_no_source(self, monkeypatch):
        def missing(name):
            raise placement.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(placement.metadata, "distribution", missing)
        assert editable_source() is None

    def test_non_editable_install_keeps_todays_behaviour(self, tmp_path):
        pkg = _repo(tmp_path / "pkg")
        gate = PlacementGate(source=lambda: None, bundle=lambda: None, home=tmp_path / "answers")
        assert gate.decide(write(pkg / "profiles" / "x" / "CLAUDE.md"), Who(), None).allowed


class TestGate:
    @pytest.mark.parametrize(
        "where",
        [
            "pkg/profiles/package/rules/r.md",
            "pkg/hooks/context/new.py",
            "wt/profiles/package/skills/s/SKILL.md",
            "bundle/.claude/conditions/pre-bash.x.sh",
            "bundle/profiles/engineer/.claude/rules/r.md",
            "bundle/enforcements.json",
        ],
    )
    def test_refuses_a_placement_write_without_the_answer(self, world, where):
        gate, root = world
        decision = gate.decide(write(root / where), Who(), None)
        assert not decision.allowed
        assert placement.QUESTION in decision.reason
        assert f'"{PACKAGE}"' in decision.reason and f'"{BUNDLE}"' in decision.reason
        assert "recommendation" in decision.reason

    @pytest.mark.parametrize("where", ["pkg/scripts/x.py", "pkg/tests/hooks_test.py", "bundle/docs/x.md", "elsewhere"])
    def test_passes_writes_outside_placement_folders(self, world, where):
        gate, root = world
        _repo(root / "elsewhere")
        target = root / where / "profiles" / "p.md" if where == "elsewhere" else root / where
        assert gate.decide(write(target), Who(), None).allowed

    def test_package_answer_opens_the_package_only(self, world):
        gate, root = world
        assert answer(gate.home, f"{PACKAGE} (Recommended)")
        assert gate.decide(write(root / "pkg/profiles/package/rules/r.md"), Who(), None).allowed
        assert gate.decide(write(root / "wt/hooks/context/x.py"), Who(), None).allowed
        refused = gate.decide(write(root / "bundle/profiles/engineer/CLAUDE.md"), Who(), None)
        assert not refused.allowed and BUNDLE in refused.reason

    def test_bundle_answer_opens_the_bundle_only(self, world):
        gate, root = world
        assert answer(gate.home, BUNDLE)
        assert gate.decide(write(root / "bundle/profiles/engineer/CLAUDE.md"), Who(), None).allowed
        refused = gate.decide(write(root / "pkg/profiles/package/rules/r.md"), Who(), None)
        assert not refused.allowed and PACKAGE in refused.reason

    def test_an_answer_belongs_to_its_session(self, world):
        gate, root = world
        answer(gate.home, PACKAGE)
        other = Call(tool="Write", tool_input={"file_path": str(root / "pkg/rules/r.md")}, session="other")
        assert not gate.decide(other, Who(), None).allowed

    def test_unrelated_answer_records_nothing(self, world):
        gate, root = world
        assert not answer(gate.home, "something else")
        assert placement.answered(SID, gate.home) == set()

    @pytest.mark.parametrize(
        "payload",
        [
            {"tool_name": "Write", "session_id": SID, "tool_response": {"answers": {"q": PACKAGE}}},
            {"tool_name": "AskUserQuestion", "tool_response": {"answers": {"q": PACKAGE}}},
            {"tool_name": "AskUserQuestion", "session_id": SID, "tool_response": PACKAGE},
        ],
    )
    def test_only_a_session_question_answer_is_heard(self, tmp_path, payload):
        assert placement.heard(payload, tmp_path) is False
        assert placement.answered(SID, tmp_path) == set()

    def test_answers_accumulate_across_questions_and_values(self, tmp_path):
        home = tmp_path / "deep" / "answers"
        assert answer(home, PACKAGE)
        payload = {
            "tool_name": "AskUserQuestion",
            "session_id": SID,
            "tool_response": {"answers": {"first": "neither", "second": BUNDLE.upper()}},
        }
        assert placement.heard(payload, home) is True
        assert placement.answered(SID, home) == {PACKAGE, BUNDLE}

    def test_writes_outside_any_repository_pass(self, world):
        gate, root = world
        assert gate.decide(write(root / "loose" / "profiles" / "p.md"), Who(), None).allowed

    def test_the_refusal_names_each_destination_and_the_first_paths(self):
        reason = placement.refusal([PACKAGE, BUNDLE], ["a", "b", "c", "d"])
        assert reason.startswith(
            "placement: agentihooks is installed editable and this write lands in the agentihooks package and the "
            "bundle profile extension (a, b, c). Before it, ask the operator one AskUserQuestion,"
        )
        assert reason.endswith("Then write only into the destination the operator chose.")


class TestHomes:
    def test_answers_live_under_the_agentihooks_home(self, tmp_path, monkeypatch):
        import hooks.config

        monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
        assert placement.answers_home() == tmp_path / "placement"

    def test_the_linked_bundle_comes_from_the_install_state(self, tmp_path, monkeypatch):
        from hooks.context import profile_chain

        bundle = tmp_path / "bundle"
        bundle.mkdir()
        monkeypatch.setattr(profile_chain, "read_state", lambda: {"bundle": {"path": str(bundle)}})
        assert placement.linked_bundle() == bundle

    def test_the_post_tool_use_handler_records_the_answer(self, tmp_path, monkeypatch):
        import hooks.config
        from hooks import hook_manager

        monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
        monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *args: None)
        hook_manager.on_post_tool_use(
            {
                "session_id": SID,
                "cwd": "/",
                "tool_name": "AskUserQuestion",
                "tool_response": {"answers": {placement.QUESTION: PACKAGE}},
            }
        )
        assert placement.answered(SID) == {PACKAGE}

    def test_a_failing_answer_record_is_logged_and_the_handler_goes_on(self, monkeypatch):
        from hooks import hook_manager

        def boom(payload):
            raise OSError("disk full")

        logged = []
        monkeypatch.setattr(placement, "heard", boom)
        monkeypatch.setattr(hook_manager, "log", lambda *args, **kwargs: logged.append(args))
        monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *args: None)
        hook_manager.on_post_tool_use(
            {"session_id": SID, "cwd": "/", "tool_name": "AskUserQuestion", "tool_response": {"answers": {}}}
        )
        assert ("placement answer record failed", {"error": "disk full"}) in logged

    def test_serena_edit_resolves_against_the_session_cwd(self, world):
        gate, root = world
        call = Call(
            tool="mcp__serena__replace_symbol_body",
            tool_input={"relative_path": "hooks/hook_manager.py", "name_path": "x", "body": "x"},
            cwd=str(root / "wt"),
            session=SID,
        )
        assert gate.matches(call)
        assert not gate.decide(call, Who(), None).allowed
        answer(gate.home, PACKAGE)
        assert gate.decide(call, Who(), None).allowed

    def test_matches_only_file_writes(self, world):
        gate, _ = world
        assert gate.matches(Call(tool="Edit"))
        assert not gate.matches(Call(tool="Bash", tool_input={"command": "ls"}))
        assert not gate.matches(Call(tool="mcp__serena__find_symbol"))


class TestEntry:
    def test_the_condition_shim_reaches_the_gate_by_name(self):
        assert entry.main(["placement"], stdin=_Stdin({"tool_name": "Bash"}), environ={}) == 0
        assert entry.main(["no-such-gate"], stdin=_Stdin({}), environ={}) == 1


class _Stdin:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload)


def test_the_post_tool_hook_records_the_operators_answer(tmp_path):
    import os
    import sys

    home = tmp_path / "ahome"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "AGENTIHOOKS_HOME": str(home),
        "AGENTIHOOKS_TARGET": "claude",
        "BRAIN_ENABLED": "false",
        "BROADCAST_ENABLED": "false",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "AGENTIHOOKS_SWARM_REDIS_URL": "redis://127.0.0.1:1/0",
    }
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": SID,
        "cwd": str(tmp_path),
        "tool_name": "AskUserQuestion",
        "tool_input": {"questions": [{"question": placement.QUESTION}]},
        "tool_response": {"answers": {placement.QUESTION: BUNDLE}},
    }
    subprocess.run([sys.executable, "-m", "hooks"], input=json.dumps(payload), text=True, env=env, timeout=60)
    assert placement.answered(SID, home / "placement") == {BUNDLE}
