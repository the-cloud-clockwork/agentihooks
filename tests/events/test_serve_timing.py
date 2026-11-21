import pytest

from scripts.events.serve_timing import ServeTiming


@pytest.mark.feature("hub-slot-request-queue-timing")
def test_pool_occupancy_is_independent_and_recovers_after_an_exception(capsys: pytest.CaptureFixture[str]) -> None:
    slots = ServeTiming(2)
    other = ServeTiming(4)
    with slots.request("slots.rig.one", 750):
        with slots.request("exits.rig.one", 2), other.request("slots.other.one", 1):
            pass
        with pytest.raises(ValueError, match="handler failed"), slots.request("slots.rig.one", 3):
            raise ValueError("handler failed")
    with slots.request("slots.rig.one", 0), other.request("slots.other.one", 0):
        pass
    assert capsys.readouterr().err.splitlines() == [
        "bus: serve subject=slots.rig.one waited_ms=750 running=1 workers=2",
        "bus: serve subject=exits.rig.one waited_ms=2 running=2 workers=2",
        "bus: serve subject=slots.other.one waited_ms=1 running=1 workers=4",
        "bus: serve subject=slots.rig.one waited_ms=3 running=2 workers=2",
        "bus: serve subject=slots.rig.one waited_ms=0 running=1 workers=2",
        "bus: serve subject=slots.other.one waited_ms=0 running=1 workers=4",
    ]
