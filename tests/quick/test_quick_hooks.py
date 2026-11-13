import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

import hooks.context.context_recycle as recycle
from hooks.context import quick_swarm
from hooks.lifecycle import handoff_close
from scripts import init_agent
from scripts.quick import member, paths, registry
from scripts.quick.guard import QuickError

pytestmark = pytest.mark.unit


def _quick(folder: Path, eng_state: str = registry.READY) -> dict[str, str]:
    seats = (
        registry.Seat(1, "master", "w1:p1", "claude", "quick-abc-master"),
        registry.Seat(2, "eng-1", "w1:p2", "claude", "quick-abc-eng-1", state=eng_state),
    )
    registry.save(folder, registry.Swarm("quick-abc", "/work", seats))
    paths.create(folder, ["master", "eng-1"])
    return {
        member.QUICK_ENV: "quick-abc",
        member.SEAT_ENV: "eng-1",
        member.DIR_ENV: str(folder),
        "HERDR_PANE_ID": "w1:p2",
        "AGENTIHOOKS_HOME": str(folder / "home"),
    }


@pytest.fixture(autouse=True)
def _recycle_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(recycle, "AGENTIHOOKS_HOME", tmp_path / "recycle")
    monkeypatch.setattr(recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(recycle, "HANDOFF_MARGIN", 50)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: None)
    monkeypatch.setattr(recycle, "codex_context", lambda session_id: None)


@pytest.mark.feature("quick-swarm")
def test_a_quick_member_gets_its_block_on_the_first_prompt_and_then_on_cadence(tmp_path):
    environ = _quick(tmp_path)
    first = quick_swarm.refocus("s1", "prompt", environ)
    assert first.startswith("QUICK SWARM") and "eng-1 of quick swarm quick-abc" in first
    assert quick_swarm.refocus("s1", "prompt", environ) == ""
    calls = [quick_swarm.refocus("s1", "tool", environ) for _ in range(quick_swarm.EVERY)]
    assert calls[:-1] == [""] * (quick_swarm.EVERY - 1) and calls[-1] == first


def test_the_block_returns_after_compaction_and_when_it_changes(tmp_path):
    environ = _quick(tmp_path)
    first = quick_swarm.refocus("s1", "prompt", environ)
    assert quick_swarm.refocus("s1", "compact", environ) == first
    paths.plan(tmp_path, "eng-1").write_text("- [ ] build\n")
    changed = quick_swarm.refocus("s1", "tool", environ)
    assert changed and changed != first and "next: build" in changed


def test_a_session_outside_any_quick_swarm_gets_nothing(tmp_path):
    assert quick_swarm.refocus("s1", "prompt", {"AGENTIHOOKS_HOME": str(tmp_path)}) == ""
    assert quick_swarm.refocus("", "prompt", _quick(tmp_path)) == ""


@pytest.mark.feature("quick-swarm")
def test_a_quick_member_at_the_compact_limit_is_told_to_hand_off_into_its_seat(monkeypatch, tmp_path):
    environ = _quick(tmp_path)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 610_000)
    text = recycle.directive("s1", environ)
    assert text is not None and text.startswith("HANDOFF PREPARATION")
    assert str(paths.handoff(tmp_path, "eng-1")) in text and "agentihooks quick handoff" in text
    assert str(paths.learned(tmp_path, "eng-1")) in text
    assert "seat:<seat>" in text and "agentihooks swarm" not in text


@pytest.mark.feature("quick-swarm")
def test_past_the_hard_gate_a_quick_member_may_only_read_write_its_handoff_and_hand_off(monkeypatch, tmp_path):
    environ = _quick(tmp_path)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 700_000)
    monkeypatch.setattr(recycle, "_in_scratchpad", lambda path: path.startswith(str(tmp_path)))
    blocked = recycle.gate("Bash", {"command": "pytest -q"}, "s1", environ)
    assert blocked is not None and blocked.startswith("BLOCKED") and "agentihooks quick handoff" in blocked
    assert recycle.gate("Bash", {"command": "agentihooks quick handoff"}, "s1", environ) is not None
    paths.handoff(tmp_path, "eng-1").write_text(
        "# Handoff v2\n## Intent\nx\n## Done\nNone\n## Stopped at\nx\n## Decisions and promises\nNone\n"
        "## Next\nx\n## Read first\nNone\n<!-- handoff complete -->\n"
    )
    assert recycle.gate("Bash", {"command": "agentihooks quick handoff"}, "s1", environ) is None
    assert recycle.gate("Bash", {"command": "cat notes"}, "s1", environ) is None
    handoff_write = {"file_path": str(paths.handoff(tmp_path, "eng-1")), "content": "x"}
    assert recycle.gate("Write", handoff_write, "s1", environ) is None


@pytest.mark.feature("quick-swarm")
def test_the_quick_gate_names_the_tokens_held_and_checks_a_named_handoff_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environ = _quick(tmp_path)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 700_000)
    blocked = recycle.gate("Bash", {"command": "pytest -q"}, "s1", environ)
    assert blocked is not None and "holds 700k tokens, at or over the 650k limit" in blocked
    document = tmp_path / "elsewhere.md"
    document.write_text("# Handoff v2\n## Intent\nx\n")
    refused = recycle.gate("Bash", {"command": f"agentihooks quick handoff {document}"}, "s1", environ)
    assert refused is not None and refused.startswith("BLOCKED")
    document.write_text(
        "# Handoff v2\n## Intent\nx\n## Done\nNone\n## Stopped at\nx\n## Decisions and promises\nNone\n"
        "## Next\nx\n## Read first\nNone\n<!-- handoff complete -->\n"
    )
    assert recycle.gate("Bash", {"command": f"agentihooks quick handoff {document}"}, "s1", environ) is None


def test_a_session_outside_a_quick_swarm_and_any_swarm_gets_no_directive(monkeypatch, tmp_path):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 700_000)
    assert recycle.directive("s1", {"AGENTIHOOKS_HOME": str(tmp_path)}) is None
    assert recycle.gate("Bash", {"command": "pytest"}, "s1", {"AGENTIHOOKS_HOME": str(tmp_path)}) is None


@pytest.mark.feature("quick-swarm")
def test_a_pending_quick_seat_closes_with_its_pane_kept_and_relaunches(monkeypatch, tmp_path):
    environ = _quick(tmp_path, eng_state=registry.PENDING)
    monkeypatch.setattr(handoff_close.shutil, "which", lambda name: "/bin/agentihooks")
    found = handoff_close.quick_pending(environ)
    assert found is not None and found.seat.seat == "eng-1"
    close = "/bin/agentihooks terminate-agent 42 --type any --keep-pane"
    assert handoff_close.close_command(42, keep_pane=True) == close.split()
    relaunch = handoff_close.relaunch_command(found)
    assert relaunch == [*"/bin/agentihooks quick relaunch eng-1 --folder".split(), str(tmp_path)]
    assert handoff_close.quick_pending({**environ, "HERDR_PANE_ID": "w1:p1"}) is None
    assert handoff_close.quick_pending(_quick(tmp_path)) is None


def test_a_quick_handoff_runs_the_close_then_the_relaunch(monkeypatch):
    ran: list[tuple[list[str], dict]] = []

    def run(argv, **kwargs):
        ran.append((argv, kwargs))
        return handoff_close.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(handoff_close.subprocess, "run", run)
    handoff_close.close_and_relaunch(["close"], ["relaunch"])
    assert ran == [
        (
            ["close"],
            {"stdin": handoff_close.subprocess.DEVNULL, "capture_output": True, "timeout": 120, "check": False},
        ),
        (
            ["relaunch"],
            {"stdin": handoff_close.subprocess.DEVNULL, "capture_output": True, "timeout": 600, "check": False},
        ),
    ]


@pytest.mark.feature("quick-swarm")
def test_init_agent_runs_in_a_given_pane_without_opening_or_owning_it(monkeypatch, tmp_path):
    existing_calls: list[tuple[str, dict]] = []
    run_calls: list[tuple] = []
    monkeypatch.setattr(init_agent.herdr_host, "ensure_server", lambda environ: False)
    monkeypatch.setattr(init_agent.herdr_host, "open_pane", lambda *a: pytest.fail("opened a pane"))
    monkeypatch.setattr(init_agent.herdr_panes, "record", lambda *a: pytest.fail("recorded the pane"))

    def existing_pane(pane, environ):
        existing_calls.append((pane, environ))
        return SimpleNamespace(workspace_id="w1", tab_id="t1", pane_id=pane)

    def run(pane, launcher, environ, env=None):
        run_calls.append((pane, launcher, environ, env))

    monkeypatch.setattr(init_agent.herdr_host, "existing_pane", existing_pane)
    monkeypatch.setattr(init_agent.herdr_host, "run", run)
    args = argparse.Namespace(title="", placement="tab", workspace="", resume="", pane="w1:p3")
    launcher = tmp_path / "eng.sh"
    report = init_agent._start_herdr(launcher, tmp_path, "eng", args, "claude", {"E": "1"})
    assert existing_calls == [("w1:p3", {"E": "1"})]
    assert run_calls == [("w1:p3", launcher, {"E": "1"}, {"HERDR_AGENT": "claude"})]
    assert "pane_id=w1:p3" in report


def test_init_agent_accepts_a_pane_option():
    assert init_agent._parser().parse_args(["--pane", "w1:p3"]).pane == "w1:p3"
    assert init_agent._parser().parse_args([]).pane == ""


def test_init_agent_pane_option_help_text_is_exact():
    action = next(a for a in init_agent._parser()._actions if "--pane" in a.option_strings)
    assert action.help == "herdr: run in this existing pane instead of opening one"


@pytest.mark.feature("quick-swarm")
def test_a_quick_seat_launcher_exports_its_quick_variables(tmp_path):
    env = {"XDG_RUNTIME_DIR": str(tmp_path), member.QUICK_ENV: "quick-abc", member.SEAT_ENV: "eng-1"}
    env[member.DIR_ENV] = str(tmp_path / "q")
    launcher, _ = init_agent._write_launcher(tmp_path, "quick-abc-eng-1", "", [], env, init_agent.AgentSpec())
    text = launcher.read_text()
    assert f"export {member.QUICK_ENV}=quick-abc\n" in text and f"export {member.SEAT_ENV}=eng-1\n" in text
    assert f"export {member.DIR_ENV}={tmp_path / 'q'}\n" in text
    plain, _ = init_agent._write_launcher(tmp_path, "plain", "", [], {"XDG_RUNTIME_DIR": str(tmp_path)})
    assert member.QUICK_ENV not in plain.read_text()


def test_the_quick_folder_name_comes_from_the_master_session(monkeypatch, tmp_path):
    environ = {"CLAUDE_CODE_SESSION_ID": "d039e169-c93c-4613-a495-d6dfb5284ff5", "HOME": str(tmp_path)}
    assert paths.quick_id(environ) == "quick-d039e169"
    monkeypatch.setattr(paths.herdr_host, "repo_label", lambda directory: "agentihooks")
    assert paths.folder_for(environ, "quick-d039e169", tmp_path) == tmp_path / "scratchpad/agentihooks/quick-d039e169"


def test_a_quick_swarm_needs_the_master_session_id():
    with pytest.raises(QuickError, match="no session id"):
        paths.quick_id({"CLAUDE_CODE_SESSION_ID": "abc"})
