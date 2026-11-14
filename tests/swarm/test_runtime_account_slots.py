from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm.runtime import HerdrRuntime
from tests.swarm.profile_fixture import validated


def test_hive_runtime_reserves_and_pins_account_before_start(tmp_path):
    calls = []

    def run(argv, **kwargs):
        assert argv[:3] == ["env", "AGENTIHOOKS_SLOT_HIVE=hive-a", "AGENTIHOOKS_SLOT_EXECUTION=execution-a"]
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="rig", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    runtime.spawn(
        config,
        "eng",
        "agent-a",
        {"id": "one", "title": "work", "account_slot_hive": "hive-a", "execution_id": "execution-a"},
    )
    assert len(calls) == 1


def test_hive_runtime_account_refusal_does_not_launch(tmp_path, monkeypatch):
    from scripts.swarm.tick import SpawnError

    def refused(argv, **kwargs):
        assert argv[:3] == ["env", "AGENTIHOOKS_SLOT_HIVE=hive-a", "AGENTIHOOKS_SLOT_EXECUTION=execution-a"]
        return SimpleNamespace(returncode=3, stdout="", stderr="No account slot available")

    runtime = HerdrRuntime(home=tmp_path, run=refused, choose=lambda *_: ("claude", "open"))
    terminated = []
    monkeypatch.setattr(runtime, "_terminate", terminated.append)
    config = SimpleNamespace(
        slug="rig", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    with pytest.raises(SpawnError, match="^No account slot available$"):
        runtime.spawn(
            config,
            "eng",
            "agent-a",
            {"id": "one", "title": "work", "account_slot_hive": "hive-a", "execution_id": "execution-a"},
        )
    assert terminated == ["agent-a"]


@pytest.mark.feature("hive-saved-account-slot-preference")
@pytest.mark.parametrize("room,routed", [(False, []), (True, ["full-account"])])
def test_a_saved_handoff_account_routes_a_hive_launch_only_while_its_account_slot_has_a_worker_seat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, room: bool, routed: list[str]
) -> None:
    from scripts.hive import account_slots

    asked: list[tuple[str, str, str, str]] = []
    calls: list[list[str]] = []

    def answer(hive: str, execution: str, harness: str, account: str) -> bool:
        asked.append((hive, execution, harness, account))
        return room

    monkeypatch.setattr(account_slots, "room", answer)

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="rig", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    runtime.spawn(
        config,
        "eng",
        "agent-a",
        {
            "id": "one",
            "title": "work",
            "account_slot_hive": "hive-a",
            "execution_id": "execution-a",
            "handoff_envelope": {"launch": {"profile": "engineer", "harness": "claude", "account": "full-account"}},
        },
    )
    passed = calls[0][calls[0].index("--") + 1 :]
    assert [passed[index + 1] for index, arg in enumerate(passed) if arg == "--route"] == routed
    assert asked == [("hive-a", "execution-a", "claude", "full-account")]


@pytest.mark.feature("hive-saved-account-slot-preference")
def test_a_quota_repick_routes_its_own_account_without_taking_a_slot_for_the_saved_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import account_slots

    asked: list[tuple[str, str, str, str]] = []
    calls: list[list[str]] = []

    def answer(hive: str, execution: str, harness: str, account: str) -> bool:
        asked.append((hive, execution, harness, account))
        return True

    monkeypatch.setattr(account_slots, "room", answer)

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="rig", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    launch = {"profile": "engineer", "harness": "claude", "model": "m", "effort": "high", "account": "kept"}
    runtime.spawn(
        config,
        "eng",
        "agent-a",
        {
            "id": "one",
            "title": "work",
            "account_slot_hive": "hive-a",
            "execution_id": "execution-a",
            "launch_assignment": launch,
            "admitted_execution_id": "execution-a",
            "handoff_envelope": {"reason": "quota", "launch": launch},
        },
    )
    passed = calls[0][calls[0].index("--") + 1 :]
    assert [passed[index + 1] for index, arg in enumerate(passed) if arg == "--route"] == ["kept"]
    assert asked == []


@pytest.mark.feature("hive-saved-account-slot-preference")
def test_a_slot_request_the_hub_cannot_answer_refuses_the_launch_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import account_slots
    from scripts.swarm.tick import SpawnError

    def unanswered(hive: str, execution: str, harness: str, account: str) -> bool:
        raise account_slots.Unavailable("Account slot admission failed")

    monkeypatch.setattr(account_slots, "room", unanswered)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="rig", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    with pytest.raises(SpawnError) as refused:
        runtime.spawn(
            config,
            "eng",
            "agent-a",
            {
                "id": "one",
                "title": "work",
                "account_slot_hive": "hive-a",
                "execution_id": "execution-a",
                "handoff_envelope": {"launch": {"profile": "engineer", "harness": "claude", "account": "saved"}},
            },
        )
    assert (str(refused.value), refused.value.status) == ("Account slot admission failed", "unavailable")
    assert calls == []
