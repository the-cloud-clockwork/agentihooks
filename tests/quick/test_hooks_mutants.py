import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hooks.context.context_recycle as recycle
import hooks.hook_manager as hook_manager
from hooks.context import quick_swarm
from hooks.lifecycle import handoff_close
from scripts.quick import member, paths, registry

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
def _recycle_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(recycle, "AGENTIHOOKS_HOME", tmp_path / "recycle")
    monkeypatch.setattr(recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(recycle, "HANDOFF_MARGIN", 50)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: None)
    monkeypatch.setattr(recycle, "codex_context", lambda session_id: None)


def test_quick_refusal_reads_the_named_document_and_resolves_against_its_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, tuple[str, object]] = {}
    read_kwargs: list[dict] = []
    original_read_text = Path.read_text

    def spy_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        read_kwargs.append({"encoding": encoding, "errors": errors})
        return original_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(recycle.quick_handoff, "resolver", lambda folder: f"resolver:{folder}")
    monkeypatch.setattr(
        recycle.handoff_check,
        "problems",
        lambda text, resolver: seen.update(problems=(text, resolver)) or ["p1"],
    )
    monkeypatch.setattr(recycle.handoff_check, "refusal", lambda problems: f"refusal:{problems}")
    doc = tmp_path / "doc.md"
    doc.write_text("TEXT", encoding="utf-8")
    seat = registry.Seat(1, "eng-1", "w1:p2", "claude", "quick-abc-eng-1")
    found = member.Member(tmp_path, registry.Swarm("quick-abc", "/work", (seat,)), seat)
    result = recycle._quick_refusal([str(doc), "ignored"], found)
    assert seen["problems"] == ("TEXT", f"resolver:{tmp_path}")
    assert result == "BLOCKED: refusal:['p1']"
    assert read_kwargs[-1] == {"encoding": "utf-8", "errors": None}


def test_quick_instructions_cites_this_seats_learned_path(tmp_path: Path) -> None:
    seat = registry.Seat(1, "eng-1", "w1:p2", "claude", "quick-abc-eng-1")
    found = member.Member(tmp_path, registry.Swarm("quick-abc", "/work", (seat,)), seat)
    text = recycle._quick_instructions(found)
    assert str(paths.learned(tmp_path, "eng-1")) in text


def test_the_quick_gate_boundary_is_strict_and_the_head_shows_exact_k_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environ = _quick(tmp_path)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 649_999)
    assert recycle.gate("Bash", {"command": "pytest"}, "s1", environ) is None
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 650_000)
    blocked = recycle.gate("Bash", {"command": "pytest"}, "s1", environ)
    assert blocked is not None and "holds 650k tokens, at or over the 650k limit" in blocked


def test_a_quick_member_with_no_usage_data_is_never_gated(tmp_path: Path) -> None:
    environ = _quick(tmp_path)
    assert recycle.gate("Bash", {"command": "pytest"}, "s1", environ) is None


def test_the_quick_gate_checks_this_sessions_own_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    environ = _quick(tmp_path)
    monkeypatch.setattr(recycle, "_used", lambda session_id: {"s1": 700_000}.get(session_id))
    assert recycle.gate("Bash", {"command": "pytest"}, "s1", environ) is not None
    assert recycle.gate("Bash", {"command": "pytest"}, "other", environ) is None


def test_a_non_handoff_write_past_the_hard_gate_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    environ = _quick(tmp_path)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 700_000)
    other_write = {"file_path": str(tmp_path / "elsewhere.txt"), "content": "x"}
    blocked = recycle.gate("Write", other_write, "s1", environ)
    assert blocked is not None and blocked.startswith("BLOCKED")


def test_read_tools_bypass_the_quick_gate_even_past_the_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    environ = _quick(tmp_path)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 700_000)
    assert recycle.gate("Read", {"file_path": "x"}, "s1", environ) is None


def test_refocus_blocks_calls_the_quick_refocus_with_this_sessions_id_and_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hooks.context import quick_swarm as quick_swarm_mod
    from hooks.context import swarm_refocus

    monkeypatch.setattr(swarm_refocus, "refocus_context", lambda session_id, event: "")
    calls: list[tuple[str, str]] = []

    def fake_refocus(session_id: str, event: str) -> str:
        calls.append((session_id, event))
        return "QUICK"

    monkeypatch.setattr(quick_swarm_mod, "refocus", fake_refocus)
    result = hook_manager._refocus_blocks("s1", "tool")
    assert calls == [("s1", "tool")]
    assert result == ["QUICK"]


def _refocus_state_path(home: Path, session_id: str) -> Path:
    return home / quick_swarm.STATE_DIR / f"{session_id}.json"


def test_refocus_cadence_tracks_this_sessions_exact_calls_and_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.quick import member as quick_member_mod
    from scripts.quick import paths as quick_paths_mod
    from scripts.quick import seeds as quick_seeds_mod

    monkeypatch.setattr(quick_member_mod, "find", lambda environ: SimpleNamespace())
    monkeypatch.setattr(quick_seeds_mod, "block", lambda found: "BLOCK")
    monkeypatch.setattr(quick_paths_mod, "home", lambda environ: tmp_path)
    digest = hashlib.sha256(b"BLOCK").hexdigest()
    state_path = _refocus_state_path(tmp_path, "s1")
    state_path.parent.mkdir(parents=True)

    state_path.write_text(json.dumps({"hash": digest, "calls": 39, "at": 25}))
    assert quick_swarm.refocus("s1", "tool", {}) == ""
    assert json.loads(state_path.read_text()) == {"hash": digest, "calls": 40, "at": 25}

    state_path.write_text(json.dumps({"hash": digest, "calls": 5}))
    assert quick_swarm.refocus("s1", "tool", {}) == ""
    assert json.loads(state_path.read_text()) == {"hash": digest, "calls": 6, "at": 0}

    state_path.write_text(json.dumps({"hash": digest, "calls": 39}))
    assert quick_swarm.refocus("s1", "tool", {}) == "BLOCK"
    assert json.loads(state_path.read_text()) == {"hash": digest, "calls": 40, "at": 40}

    state_path.write_text(json.dumps({"hash": digest, "at": 0}))
    assert quick_swarm.refocus("s1", "tool", {}) == ""
    assert json.loads(state_path.read_text()) == {"hash": digest, "calls": 1, "at": 0}


def test_a_nested_stop_event_never_checks_for_a_quick_pending_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(handoff_close, "_home", lambda: tmp_path)
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: 4242)
    monkeypatch.setattr("hooks.context.broadcast._load_sessions", lambda: {"sid-1": {"status": "alive"}})
    calls: list[int] = []
    monkeypatch.setattr(handoff_close, "quick_pending", lambda environ: calls.append(1) or None)
    assert handoff_close.on_stop({"session_id": "sid-1", "agent_id": "a1"}) is False
    assert calls == []
    assert handoff_close.on_stop({"session_id": "sid-1", "stop_hook_active": True}) is False
    assert calls == []
    assert handoff_close.on_stop({"session_id": "sid-1"}) is False
    assert calls == [1]


def test_a_missing_session_id_defaults_to_empty_and_schedules_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(handoff_close, "_home", lambda: tmp_path)
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: 4242)
    monkeypatch.setattr("hooks.context.broadcast._load_sessions", lambda: {})
    calls: list[int] = []
    monkeypatch.setattr(handoff_close, "quick_pending", lambda environ: calls.append(1) or None)
    assert handoff_close.on_stop({}) is False
    assert calls == []


def test_the_claim_uses_this_sessions_own_pid_and_session_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(handoff_close, "_home", lambda: tmp_path)
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: 4242)
    monkeypatch.setattr("hooks.context.broadcast._load_sessions", lambda: {"sid-1": {"status": "handed_off"}})
    monkeypatch.setattr(handoff_close, "quick_pending", lambda environ: None)
    monkeypatch.delenv(handoff_close.CLOSE_ENV, raising=False)
    seen: list[tuple[int, str]] = []

    def fake_claimed(pid: int, session_id: str) -> bool:
        seen.append((pid, session_id))
        return False

    monkeypatch.setattr(handoff_close, "_claimed", fake_claimed)
    assert handoff_close.on_stop({"session_id": "sid-1"}) is False
    assert seen == [(4242, "sid-1")]


def test_relaunch_command_asks_which_for_the_real_binary_and_falls_back_to_the_bare_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seat = registry.Seat(1, "eng-1", "w1:p2", "claude", "quick-abc-eng-1")
    found = member.Member(tmp_path, registry.Swarm("quick-abc", "/work", (seat,)), seat)
    seen: list[str] = []
    monkeypatch.setattr(handoff_close.shutil, "which", lambda name: seen.append(name) or "/bin/agentihooks")
    assert handoff_close.relaunch_command(found)[0] == "/bin/agentihooks"
    assert seen == ["agentihooks"]
    monkeypatch.setattr(handoff_close.shutil, "which", lambda name: None)
    assert handoff_close.relaunch_command(found)[0] == "agentihooks"


def test_a_quick_pending_stop_schedules_the_close_then_relaunch_with_its_pane_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(handoff_close, "_home", lambda: tmp_path)
    monkeypatch.setattr("hooks.context.account_sessions.agent_pid", lambda: 4242)
    monkeypatch.setattr("hooks.context.broadcast._load_sessions", lambda: {})
    found = SimpleNamespace(folder=tmp_path, seat=SimpleNamespace(seat="eng-1"))
    monkeypatch.setattr(handoff_close, "quick_pending", lambda environ: found)
    monkeypatch.setattr(handoff_close.shutil, "which", lambda name: "/bin/agentihooks")
    scheduled: list[tuple[tuple, dict]] = []
    monkeypatch.setattr("hooks._async.fork_and_call", lambda *args, **kwargs: scheduled.append((args, kwargs)))
    assert handoff_close.on_stop({"session_id": "sid-1"}) is True
    args, kwargs = scheduled[0]
    assert args == (
        handoff_close.close_and_relaunch,
        ["/bin/agentihooks", "terminate-agent", "4242", "--type", "any", "--keep-pane"],
        ["/bin/agentihooks", "quick", "relaunch", "eng-1", "--folder", str(tmp_path)],
    )
    assert kwargs == {"timeout_sec": 720, "task_name": "quick-handoff-close"}
