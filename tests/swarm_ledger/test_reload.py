import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("LEDGER_DIR", tempfile.mkdtemp(prefix="ledger-reload-test-"))
import ledger_server as server  # noqa: E402


class Reload(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ledger-code-"))
        self.page = self.dir / "template.html"
        self.page.write_text("<p>old</p>")
        os.utime(self.page, ns=(1, 1_000_000_000))
        self.calls = []

    def execv(self, *args):
        self.calls.append(args)

    def test_unchanged_code_keeps_the_server(self):
        started = server.code_stamp(self.dir)
        self.assertFalse(server.reload_if_changed(started, self.dir, self.execv))
        self.assertEqual(self.calls, [])

    def test_changed_code_restarts_the_server_in_place(self):
        started = server.code_stamp(self.dir)
        os.utime(self.page, ns=(2, 2_000_000_000))
        self.assertTrue(server.reload_if_changed(started, self.dir, self.execv))
        self.assertEqual(self.calls[0][1][-1], "--serve")

    def test_a_new_module_counts_as_changed_code(self):
        started = server.code_stamp(self.dir)
        new = self.dir / "ledger_new.py"
        new.write_text("")
        os.utime(new, ns=(3, 3_000_000_000))
        self.assertTrue(server.reload_if_changed(started, self.dir, self.execv))

    def test_the_real_package_has_a_stamp(self):
        self.assertGreater(server.code_stamp(), 0)


if __name__ == "__main__":
    unittest.main()
