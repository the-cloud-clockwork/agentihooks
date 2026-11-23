"""The live check of the expert launch pin over a stand-in store."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.feature_checks import expert_launch_pin as check
from scripts.swarm.store import AgentRecord

pytestmark = pytest.mark.unit


def _agent(name: str, lane: str = "expert", **fields: Any) -> AgentRecord:
    record: dict[str, Any] = {"state": "working", "harness": "claude", "model": "fable", "effort": "xhigh", **fields}
    return AgentRecord(name, lane, "expert-1", **record)


class Store:
    def __init__(self, swarms: dict[str, list[AgentRecord]], stopped: tuple[str, ...] = ()) -> None:
        self.swarms, self.stopped = swarms, stopped

    def slugs(self) -> list[str]:
        return sorted(self.swarms)

    def config(self, slug: str) -> SimpleNamespace:
        return SimpleNamespace(state="stopped" if slug in self.stopped else "running")

    def agents(self, slug: str) -> list[AgentRecord]:
        return self.swarms[slug]


def _run(store: Store) -> int:
    return check.main(store)


def test_a_fleet_with_no_expert_passes_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store({"rig": [_agent("engineer@a1b2c3-0001", lane="eng", model="opus", effort="high")]})
    assert _run(store) == 0
    assert capsys.readouterr().out == "no working expert\n"


@pytest.mark.feature("expert-kind-and-launch-pin")
def test_working_experts_on_the_pin_pass_and_are_listed(capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(
        {
            "rig": [
                _agent("expert@a1b2c3-0002", model="claude-fable-5-2[1m]", effort="max"),
                _agent("expert@a1b2c3-0001"),
            ],
            "doctor": [_agent("expert@d0c7e5-0001")],
        }
    )
    assert _run(store) == 0
    assert capsys.readouterr().out.splitlines() == [
        "doctor expert expert@d0c7e5-0001 runs claude fable at xhigh",
        "rig expert expert@a1b2c3-0001 runs claude fable at xhigh",
        "rig expert expert@a1b2c3-0002 runs claude claude-fable-5-2[1m] at max",
    ]


@pytest.mark.feature("expert-kind-and-launch-pin")
@pytest.mark.parametrize(
    "fields,ran",
    [
        ({"harness": "codex"}, "codex fable at xhigh"),
        ({"model": "opus"}, "claude opus at xhigh"),
        ({"model": "fable-old"}, "claude fable-old at xhigh"),
        ({"model": "claude-fable-lite"}, "claude claude-fable-lite at xhigh"),
        ({"model": ""}, "claude  at xhigh"),
        ({"effort": "high"}, "claude fable at high"),
        ({"effort": ""}, "claude fable at "),
    ],
)
def test_a_working_expert_off_the_pin_fails_by_name(
    capsys: pytest.CaptureFixture[str], fields: dict[str, str], ran: str
) -> None:
    store = Store({"rig": [_agent("expert@a1b2c3-0001"), _agent("expert@a1b2c3-0002", **fields)]})
    assert _run(store) == 1
    assert capsys.readouterr().out.splitlines() == [
        "rig expert expert@a1b2c3-0001 runs claude fable at xhigh",
        f"FAIL: rig expert expert@a1b2c3-0002 runs {ran}, off the pin claude fable at xhigh or max",
    ]


def test_an_expert_that_is_not_working_or_whose_swarm_stopped_is_not_judged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = Store(
        {
            "rig": [_agent("expert@a1b2c3-0001", state="starting", harness="", model="", effort="")],
            "old": [_agent("expert@0ld0ld-0001", model="opus")],
            "done": [_agent("expert@d0d0d0-0001", state="finished", model="opus")],
        },
        stopped=("old",),
    )
    assert _run(store) == 0
    assert capsys.readouterr().out == "no working expert\n"


def test_with_no_store_given_the_check_reads_the_swarm_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import store as swarm_store

    client = object()
    found = Store({"rig": [_agent("expert@a1b2c3-0001", effort="max")]})
    monkeypatch.setattr(swarm_store, "redis_client", lambda: client)
    monkeypatch.setattr(swarm_store, "RedisStore", lambda redis: found if redis is client else None)
    assert check.main() == 0
    assert capsys.readouterr().out == "rig expert expert@a1b2c3-0001 runs claude fable at max\n"
