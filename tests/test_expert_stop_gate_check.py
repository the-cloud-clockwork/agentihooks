"""The live check of the expert Stop gate over a stand-in store."""

from types import SimpleNamespace

import pytest

from scripts.feature_checks import expert_stop_gate as check

pytestmark = pytest.mark.unit


class Store:
    def __init__(self, swarms: dict[str, dict[str, str]], stopped: tuple[str, ...] = ()) -> None:
        self.swarms, self.stopped = swarms, stopped

    def slugs(self) -> list[str]:
        return list(self.swarms)

    def config(self, slug: str) -> SimpleNamespace:
        return SimpleNamespace(state="stopped" if slug in self.stopped else "running", gates=self.swarms[slug])


def test_a_fleet_with_no_running_swarm_passes_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main(Store({"old": {"expert-stop": "off"}}, stopped=("old",))) == 0
    assert capsys.readouterr().out == "no swarm is running\n"


@pytest.mark.feature("expert-stop-gate")
def test_each_swarm_that_keeps_the_gate_at_enforce_passes_and_is_listed(capsys: pytest.CaptureFixture[str]) -> None:
    swarms = {
        "rig": {},
        "doctor": {"expert-stop": "enforce", "claim-stop": "off"},
        "demo": {"expert-stop": "no such mode"},
        "old": {"expert-stop": "off"},
    }
    assert check.main(Store(swarms, stopped=("old",))) == 0
    assert capsys.readouterr().out.splitlines() == [
        "demo keeps the expert stop gate at enforce",
        "doctor keeps the expert stop gate at enforce",
        "rig keeps the expert stop gate at enforce",
    ]


@pytest.mark.feature("expert-stop-gate")
@pytest.mark.parametrize("mode", ["off", "observe", " Observe "])
def test_a_swarm_that_loosens_the_gate_fails_by_name(capsys: pytest.CaptureFixture[str], mode: str) -> None:
    assert check.main(Store({"rig": {"expert-stop": mode}, "doctor": {}})) == 1
    assert capsys.readouterr().out.splitlines() == [
        "doctor keeps the expert stop gate at enforce",
        f"FAIL: rig sets the expert stop gate to {mode.strip().lower()}, so an expert of a running lineage may stop",
    ]


def test_with_no_store_given_the_check_reads_the_swarm_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import store as swarm_store

    client = object()
    found = Store({"rig": {"expert-stop": "off"}})
    monkeypatch.setattr(swarm_store, "redis_client", lambda: client)
    monkeypatch.setattr(swarm_store, "RedisStore", lambda redis: found if redis is client else None)
    assert check.main() == 1
    assert capsys.readouterr().out == (
        "FAIL: rig sets the expert stop gate to off, so an expert of a running lineage may stop\n"
    )
