import ast
import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_server as server  # noqa: E402


class FakeServer:
    def __init__(self):
        self.socket = object()
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1


class Reload(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ledger-code-"))
        self.page = self.dir / "template.html"
        self.page.write_text("<p>old</p>")
        os.utime(self.page, ns=(1, 1_000_000_000))
        self.calls = []
        self.server = FakeServer()
        self.ready = True
        self.addCleanup(server.HANDOFF.clear)

    def hand_off(self, *args, **kwargs):
        self.calls.append((*args, kwargs))
        return b'{"demo": 6}' if self.ready else None

    def test_unchanged_code_keeps_the_server(self):
        started = server.code_stamp((self.dir,))
        self.assertEqual(server.reload_if_changed(started, self.server, (self.dir,), self.hand_off), started)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.server.shutdowns, 0)

    def test_changed_code_hands_the_socket_to_a_successor_then_stops_serving(self):
        started = server.code_stamp((self.dir,))
        os.utime(self.page, ns=(2, 2_000_000_000))
        self.assertIsNone(server.reload_if_changed(started, self.server, (self.dir,), self.hand_off))
        self.assertEqual(
            self.calls,
            [
                (
                    self.server.socket,
                    [sys.executable, str(server.CODE_DIR / "ledger_server.py"), "--serve"],
                    {"extra": {server.WATCHED: "", server.MODULES: server.loaded_modules()}},
                )
            ],
        )
        self.assertEqual(self.server.shutdowns, 1)
        self.assertEqual(server.HANDOFF, {"since": {"demo": 6}})

    def test_a_successor_that_sent_no_generations_gets_every_written_ledger(self):
        started = server.code_stamp((self.dir,))
        self.assertIsNone(
            server.reload_if_changed(started, self.server, (self.dir,), lambda *args, **kwargs: b"", force=True)
        )
        self.assertEqual(server.HANDOFF, {"since": {}})

    def test_a_successor_that_never_got_ready_leaves_the_server_serving_until_the_next_change(self):
        started = server.code_stamp((self.dir,))
        os.utime(self.page, ns=(2, 2_000_000_000))
        self.ready = False
        self.assertEqual(server.reload_if_changed(started, self.server, (self.dir,), self.hand_off), 2_000_000_000)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.server.shutdowns, 0)
        self.assertEqual(server.HANDOFF, {})

    def test_a_requested_reload_hands_off_unchanged_code(self):
        started = server.code_stamp((self.dir,))
        self.assertIsNone(server.reload_if_changed(started, self.server, (self.dir,), self.hand_off, force=True))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.server.shutdowns, 1)

    def test_a_new_module_counts_as_changed_code(self):
        started = server.code_stamp((self.dir,))
        new = self.dir / "ledger_new.py"
        new.write_text("")
        os.utime(new, ns=(3, 3_000_000_000))
        self.assertIsNone(server.reload_if_changed(started, self.server, (self.dir,), self.hand_off))

    def test_a_touched_file_with_the_loaded_content_keeps_the_server(self):
        started = server.code_stamp((self.dir,))
        loaded = server.code_digest((self.dir,))
        os.utime(self.page, ns=(2, 2_000_000_000))
        with mock.patch.object(server, "PIDFILE", self.dir / ".server.pid"):
            self.assertEqual(
                server.reload_if_changed(started, self.server, (self.dir,), self.hand_off, loaded=loaded),
                2_000_000_000,
            )
        self.assertEqual(self.calls, [])
        self.assertEqual(
            server.server_code.loaded(self.dir),
            {"pid": os.getpid(), "stamp": 2_000_000_000, "dirs": [str(self.dir)], "digest": loaded},
        )
        self.assertFalse(server.server_code.stale(self.dir, os.getpid()))

    def test_new_content_hands_off_even_with_a_loaded_digest(self):
        started = server.code_stamp((self.dir,))
        loaded = server.code_digest((self.dir,))
        self.page.write_text("<p>new</p>")
        os.utime(self.page, ns=(2, 2_000_000_000))
        self.assertIsNone(server.reload_if_changed(started, self.server, (self.dir,), self.hand_off, loaded=loaded))
        self.assertEqual(len(self.calls), 1)

    def test_a_requested_reload_ignores_the_loaded_digest(self):
        started = server.code_stamp((self.dir,))
        loaded = server.code_digest((self.dir,))
        self.assertIsNone(
            server.reload_if_changed(started, self.server, (self.dir,), self.hand_off, force=True, loaded=loaded)
        )
        self.assertEqual(len(self.calls), 1)

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

    def hand_off(self, *args, **kwargs):
        self.calls.append(args)
        return b"{}"

    def assert_reloads_after(self, changed):
        started = server.code_stamp(self.dirs)
        os.utime(changed, ns=(2, 2_000_000_000))
        self.assertIsNone(server.reload_if_changed(started, FakeServer(), self.dirs, self.hand_off))

    def test_an_inbox_change_reloads_the_server(self):
        self.assert_reloads_after(self.files[1])

    def test_a_swarm_change_reloads_the_server(self):
        self.assert_reloads_after(self.files[2])

    def test_a_nested_swarm_package_change_reloads_the_server(self):
        self.assert_reloads_after(self.files[3])

    def test_an_operation_journal_change_reloads_the_server(self):
        self.assert_reloads_after(self.files[4])

    def test_the_operation_journal_the_store_dispatches_to_is_watched(self):
        from scripts.swarm.store import RedisStore

        journal = RedisStore.operation_journal.fget(None)
        folder = Path(inspect.getfile(type(journal))).resolve().parent
        self.assertTrue(any(folder.is_relative_to(d) for d in server.CODE_DIRS), f"{folder} is not watched for reload")

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
