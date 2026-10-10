import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
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
        started = server.code_stamp((self.dir,))
        self.assertFalse(server.reload_if_changed(started, (self.dir,), self.execv))
        self.assertEqual(self.calls, [])

    def test_changed_code_restarts_the_server_in_place(self):
        started = server.code_stamp((self.dir,))
        os.utime(self.page, ns=(2, 2_000_000_000))
        self.assertTrue(server.reload_if_changed(started, (self.dir,), self.execv))
        self.assertEqual(self.calls[0][1][-1], "--serve")

    def test_a_new_module_counts_as_changed_code(self):
        started = server.code_stamp((self.dir,))
        new = self.dir / "ledger_new.py"
        new.write_text("")
        os.utime(new, ns=(3, 3_000_000_000))
        self.assertTrue(server.reload_if_changed(started, (self.dir,), self.execv))

    def test_the_real_package_has_a_stamp(self):
        self.assertGreater(server.code_stamp(), 0)


class ImportedCode(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ledger-root-"))
        self.dirs = tuple(self.root / d.relative_to(server.ROOT) for d in server.CODE_DIRS)
        self.files = [
            self.root / "scripts" / rel
            for rel in (
                "swarm_ledger/ledger_server.py",
                "inbox/store.py",
                "swarm/store.py",
                "swarm/health/verdicts.py",
                "swarm_v2/runtime/operations.py",
            )
        ]
        for path in self.files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
            os.utime(path, ns=(1, 1_000_000_000))
        self.calls = []

    def execv(self, *args):
        self.calls.append(args)

    def assert_reloads_after(self, changed):
        started = server.code_stamp(self.dirs)
        os.utime(changed, ns=(2, 2_000_000_000))
        self.assertTrue(server.reload_if_changed(started, self.dirs, self.execv))

    def test_an_inbox_change_reloads_the_server(self):
        self.assert_reloads_after(self.files[1])

    def test_a_swarm_change_reloads_the_server(self):
        self.assert_reloads_after(self.files[2])

    def test_a_nested_swarm_package_change_reloads_the_server(self):
        self.assert_reloads_after(self.files[3])

    def test_an_operation_journal_change_reloads_the_server(self):
        self.assert_reloads_after(self.files[4])

    def test_every_agentihooks_folder_the_server_imports_is_watched(self):
        modules = sorted(
            {
                node.module
                for path in SCRIPTS.glob("*.py")
                for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("scripts.")
            }
        )
        probe = (
            "import importlib, sys\n"
            f"sys.path[:0] = [{str(SCRIPTS)!r}, {str(server.ROOT)!r}]\n"
            "import ledger_server\n"
            f"for name in {modules!r}: importlib.import_module(name)\n"
            "for m in list(sys.modules.values()): print(getattr(m, '__file__', None) or '')\n"
        )
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
        loaded = {Path(f).resolve().parent for f in out.split() if f.endswith(".py") and f.startswith(str(server.ROOT))}
        loaded.discard(server.ROOT / "scripts")
        self.assertTrue(loaded)
        for folder in loaded:
            self.assertTrue(
                any(folder.is_relative_to(d) for d in server.CODE_DIRS), f"{folder} is not watched for reload"
            )


if __name__ == "__main__":
    unittest.main()
