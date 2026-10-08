import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "caps-2026-01-01"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "p", "description": "d"}],
        "questions": [],
        "followups": [],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)


class LogCap(unittest.TestCase):
    def setUp(self):
        core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)

    def test_rotate_if_full_moves_the_old_file_and_starts_fresh(self):
        path = core.LEDGER_DIR / "r.log"
        path.write_text("x" * 100)
        core.rotate_if_full(path, limit=50)
        self.assertFalse(path.exists())
        self.assertEqual(path.with_name(path.name + ".1").read_text(), "x" * 100)

    def test_rotate_if_full_is_a_noop_under_the_limit(self):
        path = core.LEDGER_DIR / "r2.log"
        path.write_text("x" * 10)
        core.rotate_if_full(path, limit=50)
        self.assertEqual(path.read_text(), "x" * 10)

    def test_append_capped_stays_bounded_under_sustained_writes(self):
        path = core.LEDGER_DIR / "a.log"
        for _ in range(50):
            core.append_capped(path, "x" * 10, limit=100)
        self.assertLessEqual(path.stat().st_size, 110)
        backup = path.with_name(path.name + ".1")
        self.assertTrue(backup.exists())
        self.assertLessEqual(backup.stat().st_size, 110)
        self.assertFalse(path.with_name(path.name + ".2").exists())


class EventsCap(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_events_are_capped_and_the_start_time_does_not_move(self):
        from scripts.swarm_ledger.repository import repository

        state = repository.get_document(SLUG)
        state["_meta"]["events"] = [
            {"rev": i, "at": 1000 + i, "by": "operator", "kind": "checked", "target": "x"} for i in range(2500)
        ]
        repository.import_document(SLUG, state, replace=True)

        state, _ = core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m-cap", "text": "hi"}])
        self.assertEqual(len(state["_meta"]["events"]), 2000)
        self.assertEqual(state["_meta"]["created_at"], 1000)

        state, _ = core.sync(SLUG)
        self.assertEqual(state["_meta"]["created_at"], 1000)


class Watch(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_watcher_removes_its_beat_file_on_sigterm(self):
        beat = core.watch_path(SLUG, "watcher")
        env = {**os.environ, "LEDGER_DIR": str(core.LEDGER_DIR), "LEDGER_PORT": "9"}
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "watch_ledger.py"), SLUG, "--as", "watcher", "--interval", "0.1"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(50):
                if beat.exists():
                    break
                time.sleep(0.1)
            self.assertTrue(beat.exists())
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertFalse(beat.exists())


if __name__ == "__main__":
    unittest.main()
