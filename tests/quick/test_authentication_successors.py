from pathlib import Path
from typing import Any

import pytest

from hooks.context import quick_swarm
from scripts.quick import handoff, launch, member, paths, registry
from tests.quick.test_handoff import VALID

pytestmark = pytest.mark.unit


def _saved(folder: Path, harness: str, auth: str, account: str) -> registry.Swarm:
    swarm = registry.Swarm(
        "quick-proof",
        str(folder),
        (
            registry.Seat(1, "master", "w1:p1", "claude", "quick-proof-master", session_id="master-session"),
            registry.Seat(2, "eng-1", "w1:p2", harness, "quick-proof-eng-1", auth=auth, account=account),
        ),
    )
    paths.create(folder, [seat.seat for seat in swarm.seats])
    registry.save(folder, swarm)
    return swarm


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize(("auth", "account"), [("default", ""), ("named", "alpha")])
def test_initial_native_launch_binds_the_selected_engineer_before_ready(
    harness: str, auth: str, account: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    swarm = _saved(tmp_path, harness, auth, account)
    observed: list[bool] = []

    class Process:
        def __init__(self, command: list[str], env: dict[str, str]) -> None:
            assert command[-2:] == ["--auth", "default" if auth == "default" else "named:alpha"]
            self.env = {**env, "HERDR_PANE_ID": "w1:p2", "AGENTIHOOKS_TARGET": harness}

        def wait(self) -> int:
            quick_swarm.refocus("initial-session", "start", self.env)
            observed.append(member.explicit_authentication("initial-session", self.env))
            return 0

    monkeypatch.setattr(launch.subprocess, "Popen", Process)
    assert launch.start_all(tmp_path, swarm, {}) == {"eng-1": 0}
    saved = registry.load(tmp_path)
    assert saved is not None
    assert saved.seats[0] == swarm.seats[0]
    assert saved.seats[1].session_id == "initial-session"
    assert saved.seats[1].state == registry.READY
    assert observed == [True]


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize(("auth", "account"), [("routed", ""), ("default", ""), ("named", "alpha")])
@pytest.mark.parametrize("transition", ["handoff", "force", "retry"])
def test_native_successors_retain_saved_authentication_when_caller_routing_changes(
    harness: str, auth: str, account: str, transition: str, tmp_path: Path
) -> None:
    swarm = _saved(tmp_path, harness, auth, account)
    if transition == "handoff":
        document = tmp_path / "handoff.md"
        document.write_text(VALID)
        assert handoff.accept(member.Member(tmp_path, swarm, swarm.seats[1]), document) == []
    elif transition == "retry":
        registry.set_state(tmp_path, "eng-1", registry.PENDING)
    commands: list[list[str]] = []
    ended: list[str] = []
    changed = {"AGENTIHOOKS_ROUTE_ACCOUNT": "other", "AH_ROUTE_INTERACTIVE_other": "1"}

    def start(arguments: list[str], env: dict[str, str]) -> int:
        commands.append(arguments)
        assert env["AGENTIHOOKS_ROUTE_ACCOUNT"] == "other"
        assert registry.bind(tmp_path, "eng-1", "w1:p2", "next-session") is not None
        return 0

    options: dict[str, Any] = {"bare": lambda pane: not commands, "start": start, "sleep": lambda seconds: None}
    if transition == "force":
        assert launch.force(tmp_path, "eng-1", changed, end=ended.append, **options) == "started"
        assert ended == [swarm.seats[1].name]
    else:
        assert launch.relaunch(tmp_path, "eng-1", changed, **options) == "started"
    expected = ["--routed-only"] if auth == "routed" else ["--auth", "default" if auth == "default" else "named:alpha"]
    assert len(commands) == 1
    assert commands[0][-len(expected) :] == expected
    saved = registry.load(tmp_path)
    assert saved is not None
    assert saved.seats[0] == swarm.seats[0]
    assert (saved.seats[1].auth, saved.seats[1].account, saved.seats[1].session_id, saved.seats[1].state) == (
        auth,
        account,
        "next-session",
        registry.READY,
    )
