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
def test_initial_routed_launch_records_its_actual_native_session(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    swarm = _saved(tmp_path, harness, "routed", "")

    class Process:
        def __init__(self, command: list[str], env: dict[str, str]) -> None:
            assert command[-1] == "--routed-only"
            self.env = {**env, "HERDR_PANE_ID": "w1:p2", "AGENTIHOOKS_TARGET": harness}

        def wait(self) -> int:
            quick_swarm.refocus("initial-session", "start", self.env)
            return 0

    monkeypatch.setattr(launch.subprocess, "Popen", Process)
    assert launch.start_all(tmp_path, swarm, {"AGENTIHOOKS_HOME": str(tmp_path / "home")}) == {"eng-1": 0}
    saved = registry.load(tmp_path)
    assert saved is not None
    assert saved.seats[0] == swarm.seats[0]
    assert saved.seats[1].session_id == "initial-session"
    assert saved.seats[1].state == registry.READY
    assert (saved.seats[1].auth, saved.seats[1].account) == ("routed", "")


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
    assert launch.start_all(tmp_path, swarm, {"AGENTIHOOKS_HOME": str(tmp_path / "home")}) == {"eng-1": 0}
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


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("state", [registry.READY, registry.PENDING])
@pytest.mark.parametrize(
    ("code", "binds", "expected"),
    [(0, True, registry.READY), (0, False, registry.PENDING), (5, True, registry.PENDING)],
)
def test_initial_launch_keeps_failed_or_unbound_native_engineers_pending(
    state: str, code: int, binds: bool, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _saved(tmp_path, "codex", "default", "")
    registry.set_state(tmp_path, "eng-1", state)
    swarm = registry.load(tmp_path)
    assert swarm is not None

    class Process:
        def __init__(self, command: list[str], env: dict[str, str]) -> None:
            saved = registry.load(tmp_path)
            assert saved is not None and saved.seats[1].state == registry.LAUNCHING

        def wait(self) -> int:
            if binds:
                assert registry.bind(tmp_path, "eng-1", "w1:p2", "new-session") is not None
            return code

    monkeypatch.setattr(launch.subprocess, "Popen", Process)
    assert launch.start_all(tmp_path, swarm, {}) == {"eng-1": code}
    saved = registry.load(tmp_path)
    assert saved is not None
    assert saved.seats[0] == swarm.seats[0]
    assert saved.seats[1].state == expected
    assert saved.seats[1].session_id == ("new-session" if binds else "")
    assert (saved.seats[1].auth, saved.seats[1].account) == ("default", "")


@pytest.mark.feature("quick-native-authentication")
def test_competing_initial_launchers_take_the_native_engineer_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    swarm = _saved(tmp_path, "claude", "default", "")
    calls: list[list[str]] = []
    competing: list[dict[str, int]] = []

    class Process:
        def __init__(self, command: list[str], env: dict[str, str]) -> None:
            calls.append(command)
            if len(calls) == 1:
                competing.append(launch.start_all(tmp_path, swarm, {}))

        def wait(self) -> int:
            assert registry.bind(tmp_path, "eng-1", "w1:p2", "new-session") is not None
            return 0

    monkeypatch.setattr(launch.subprocess, "Popen", Process)
    assert launch.start_all(tmp_path, swarm, {}) == {"eng-1": 0}
    assert competing == [{}]
    assert len(calls) == 1
    saved = registry.load(tmp_path)
    assert saved is not None
    assert saved.seats[1].state == registry.READY
    assert saved.seats[1].session_id == "new-session"


@pytest.mark.feature("quick-native-authentication")
def test_initial_native_spawn_failure_releases_the_seat_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    swarm = _saved(tmp_path, "codex", "named", "alpha")

    def refuse(command: list[str], env: dict[str, str]) -> None:
        raise OSError("native spawn failed")

    monkeypatch.setattr(launch.subprocess, "Popen", refuse)
    with pytest.raises(OSError, match="native spawn failed"):
        launch.start_all(tmp_path, swarm, {})
    saved = registry.load(tmp_path)
    assert saved is not None
    assert saved.seats[0] == swarm.seats[0]
    assert saved.seats[1].state == registry.PENDING
    assert saved.seats[1].session_id == ""
    assert (saved.seats[1].auth, saved.seats[1].account) == ("named", "alpha")


@pytest.mark.feature("quick-native-authentication")
def test_initial_launch_preserves_an_already_launching_native_engineer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _saved(tmp_path, "claude", "default", "")
    registry.set_state(tmp_path, "eng-1", registry.LAUNCHING)
    swarm = registry.load(tmp_path)
    assert swarm is not None

    def refuse(command: list[str], env: dict[str, str]) -> None:
        pytest.fail("an already launching native engineer was spawned twice")

    monkeypatch.setattr(launch.subprocess, "Popen", refuse)
    assert launch.start_all(tmp_path, swarm, {}) == {}
    assert registry.load(tmp_path) == swarm
