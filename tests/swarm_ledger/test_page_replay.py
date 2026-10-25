import unittest

from tests.swarm_ledger import page_replay


class LateOutline:
    def __init__(self):
        self.delivered = False

    def evaluate(self, script):
        if script == getattr(page_replay, "FLUSH", None):
            self.delivered = True
            return None
        return [0, "outline marked" if self.delivered else "outline pending"]


class QuietTest(unittest.TestCase):
    def test_quiet_waits_for_pending_outline_deliveries_before_capture(self):
        self.assertEqual(page_replay.quiet(LateOutline()), [0, "outline marked"])


if __name__ == "__main__":
    unittest.main()
