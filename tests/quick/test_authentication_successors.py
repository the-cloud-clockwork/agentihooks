from pathlib import Path

import pytest

from hooks.context import quick_swarm
from scripts.quick import launch, paths, registry

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
