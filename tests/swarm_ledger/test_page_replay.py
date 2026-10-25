import pytest

from tests.swarm_ledger import page_replay


class LateOutline:
    def __init__(self):
        self.delivered = False

    def evaluate(self, script):
        if script == page_replay.FLUSH:
            self.delivered = True
            return None
        if script == page_replay.FLUSHED:
            return self.delivered
        return [0, "outline marked" if self.delivered else "outline pending"]


class StalledOutline:
    def __init__(self):
        self.captures = 0

    def evaluate(self, script):
        if script == page_replay.FLUSHED:
            return False
        if script != page_replay.FLUSH:
            self.captures += 1
        return None


def test_quiet_waits_for_pending_outline_deliveries_before_capture():
    assert page_replay.quiet(LateOutline()) == [0, "outline marked"]


def test_quiet_times_out_instead_of_capturing_while_a_delivery_stays_pending(monkeypatch):
    monkeypatch.setattr(page_replay.time, "sleep", lambda seconds: None)
    page = StalledOutline()
    with pytest.raises(TimeoutError):
        page_replay.quiet(page)
    assert page.captures == 0
