import dataclasses
from pathlib import Path

import pytest

from hooks.context import quota_policy
from scripts.quick import member, paths, registry

pytestmark = pytest.mark.unit


@pytest.fixture
def quick_auth_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name in ("AH_ROUTE_API", "AGENTIHOOKS_SWARM"):
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "quick-proof"
    environ = {
        "HOME": str(tmp_path),
        "AGENTIHOOKS_HOME": str(tmp_path / "state"),
        "AGENTIHOOKS_QUICK_DIR": str(folder),
        "AGENTIHOOKS_QUICK_SEAT": "eng-1",
        "AGENTIHOOKS_QUICK_SWARM": "quick-proof",
        "HERDR_PANE_ID": "w1:p2",
        "AGENTIHOOKS_TARGET": "codex",
    }
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    paths.create(folder, ["master", "eng-1"])
    registry.save(
        folder,
        registry.Swarm(
            "quick-proof",
            str(tmp_path),
            (
                registry.Seat(1, "master", "w1:p1", "codex", "quick-proof-master"),
                registry.Seat(
                    2, "eng-1", "w1:p2", "codex", "quick-proof-eng-1", session_id="proof-session", auth="default"
                ),
            ),
        ),
    )
    paths.register(environ, "quick-proof", folder)
    return environ


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize(("mode", "account"), [("default", ""), ("named", "alpha")])
@pytest.mark.parametrize("state", [registry.READY, registry.LAUNCHING, registry.PENDING])
def test_bound_explicit_quick_engineers_bypass_automatic_quota_switching(
    harness: str,
    mode: str,
    account: str,
    state: str,
    quick_auth_member: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = Path(quick_auth_member["AGENTIHOOKS_QUICK_DIR"])
    swarm = registry.load(folder)
    assert swarm is not None
    seat = dataclasses.replace(swarm.seats[1], harness=harness, auth=mode, account=account, state=state)
    registry.save(folder, dataclasses.replace(swarm, seats=(swarm.seats[0], seat)))
    monkeypatch.setenv("AGENTIHOOKS_TARGET", harness)
    assert member.find({**quick_auth_member, "AGENTIHOOKS_TARGET": harness}) is not None

    def refuse_windows(session_id: str) -> None:
        pytest.fail("explicit bound quick authentication read automatic quota windows")

    monkeypatch.setattr(quota_policy, "_session_windows", refuse_windows)
    assert quota_policy.evaluate("proof-session") is None
    assert quota_policy.early_warning("proof-session") is None


@pytest.mark.feature("quick-native-authentication")
@pytest.mark.parametrize(
    "mismatch",
    ["routed", "session", "pane", "harness", "unsupported-mode", "unbound", "empty-name", "master", "missing"],
)
def test_routed_or_unverified_quick_members_retain_quota_policy(
    mismatch: str, quick_auth_member: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = Path(quick_auth_member["AGENTIHOOKS_QUICK_DIR"])
    swarm = registry.load(folder)
    assert swarm is not None
    seat = swarm.seats[1]
    session = "proof-session"
    if mismatch == "routed":
        seat = dataclasses.replace(seat, auth="routed")
    elif mismatch == "session":
        session = "another-session"
    elif mismatch == "pane":
        monkeypatch.setenv("HERDR_PANE_ID", "w1:p9")
    elif mismatch == "harness":
        seat = dataclasses.replace(seat, harness="claude")
    elif mismatch == "unsupported-mode":
        seat = dataclasses.replace(seat, auth="unsupported")
    elif mismatch == "unbound":
        seat = dataclasses.replace(seat, session_id="")
    elif mismatch == "empty-name":
        seat = dataclasses.replace(seat, auth="named", account="")
    elif mismatch == "master":
        seat = dataclasses.replace(swarm.seats[0], session_id=session, auth="default")
        monkeypatch.setenv("AGENTIHOOKS_QUICK_SEAT", "master")
        monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    else:
        monkeypatch.setenv("AGENTIHOOKS_QUICK_DIR", str(folder / "missing"))
        monkeypatch.setenv("HERDR_PANE_ID", "w1:p9")
    seats = (seat, swarm.seats[1]) if mismatch == "master" else (swarm.seats[0], seat)
    registry.save(folder, dataclasses.replace(swarm, seats=seats))
    reads: list[str] = []
    monkeypatch.setattr(quota_policy, "_session_windows", lambda found: reads.append(found))
    assert quota_policy.evaluate(session) is None
    assert quota_policy.early_warning(session) is None
    assert reads == [session, session]
