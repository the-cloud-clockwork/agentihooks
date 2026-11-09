import json
from contextlib import closing
from typing import Any, cast

import pytest

from scripts.events.bus import Bus


@pytest.mark.feature("hive-master-account-seat")
@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("master_first", [False, True])
def test_one_master_seat_is_independent_of_lane_capacity(remote: bool, master_first: bool) -> None:
    from scripts.events.config import BusConfig
    from scripts.events.memory import open_bus
    from scripts.hive import account_slots
    from scripts.hub import slots

    report = {"slots": [{"harness": "claude", "account": "a", "cap": 1}], "sessions": {}}
    with closing(cast(Bus, open_bus(BusConfig()))) as bus:
        slots.serve(bus)
        local = account_slots.Slots(bus, "one", lambda: report)
        member = account_slots.Remote(account_slots.asker(bus, "rig", "two"), "two", lambda: report)
        master = member if remote else local
        if master_first:
            assert master.take("master", "claude", "a", master=True) == "a"
        assert local.take("lane", "claude", "a") == "a"
        if not master_first:
            assert master.take("master", "claude", "a", master=True) == "a"
        assert master.take("master", "claude", "a", master=True) == "a"
        assert local.take("extra-lane", "claude", "a") is None
        assert member.take("extra-master", "claude", "a", master=True) is None
        entry = bus.state("account-slots").get(account_slots.key("claude", "a"))
        assert entry is not None
        holders = json.loads(entry.value)["holders"]
        assert holders["master"]["master"] is True
        assert "master" not in holders["lane"]
        master.release("master", "claude", "a")
        assert local.take("extra-lane", "claude", "a") is None
        assert member.take("extra-master", "claude", "a", master=True) == "a"


@pytest.mark.feature("hive-master-account-seat")
@pytest.mark.parametrize("cap", [0, 1])
def test_master_admission_respects_quota_and_failed_launch_returns_its_seat(cap: int) -> None:
    from scripts.events.config import BusConfig
    from scripts.events.memory import open_bus
    from scripts.hive import account_slots

    report = {"slots": [{"harness": "claude", "account": "a", "cap": cap}], "sessions": {}}
    with closing(cast(Bus, open_bus(BusConfig()))) as bus:
        slots = account_slots.Slots(bus, "one", lambda: report)
        if cap == 0:
            assert slots.take("master", "claude", "a", master=True) is None
        else:

            def refused(account: str) -> Any:
                assert account == "a"
                raise RuntimeError("launch refused")

            with pytest.raises(RuntimeError, match="^launch refused$"):
                slots.launch("master", "claude", "a", refused, master=True)
            assert slots.take("replacement", "claude", "a", master=True) == "a"


@pytest.mark.feature("hive-master-account-seat")
def test_master_seat_renews_and_expires_with_execution_ownership() -> None:
    from scripts.events.config import BusConfig
    from scripts.events.memory import open_bus
    from scripts.hive import account_slots

    report = {"slots": [{"harness": "claude", "account": "a", "cap": 1}], "sessions": {}}
    now = [1000]
    with closing(cast(Bus, open_bus(BusConfig(), clock=lambda: now[0]))) as bus:
        slots = account_slots.Slots(bus, "one", lambda: report, lambda: now[0])
        assert slots.take("master", "claude", "a", master=True) == "a"
        now[0] += account_slots.TTL_MS - 1
        slots.heartbeat(report, {"master": {"pane": {"state": "running"}}})
        now[0] += 1
        assert slots.take("other", "claude", "a", master=True) is None
        now[0] += account_slots.TTL_MS - 1
        assert slots.take("other", "claude", "a", master=True) == "a"
