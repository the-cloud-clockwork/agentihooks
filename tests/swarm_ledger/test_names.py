import os
import sys
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger  # noqa: E402


def run(argv, env):
    called = []
    with (
        unittest.mock.patch.dict(os.environ, env),
        unittest.mock.patch.object(sys, "argv", ["ledger.py", *argv]),
        unittest.mock.patch.object(ledger, "cmd_status", called.append, create=True),
    ):
        ledger.main()
    return called


class Names(unittest.TestCase):
    def test_the_flags_name_the_ledger_and_the_member(self):
        (args,) = run(["--slug", "demo", "--as", "eng-1", "status"], {})
        self.assertEqual((args.slug, args.name), ("demo", "eng-1"))

    def test_the_old_environment_names_do_not(self):
        with self.assertRaises(SystemExit) as stop:
            run(["status"], {"PLAN_LEDGER": "demo", "PLAN_LEDGER_AS": "eng-1"})
        self.assertEqual(stop.exception.code, "--slug and --as are required")
