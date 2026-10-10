import os
import re
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


def signal_state(pid):
    proc_dir = Path("/proc") / str(pid)
    try:
        status = (proc_dir / "status").read_text()
        wchan = (proc_dir / "wchan").read_text()
    except OSError as exc:
        return f"unreadable: {exc}"
    fields = [
        line
        for line in status.splitlines()
        if line.startswith(("State", "SigQ", "SigPnd", "ShdPnd", "SigBlk", "SigIgn", "SigCgt"))
    ]
    return "\n".join([*fields, f"wchan: {wchan}"])


def stop_or_report_stack(proc, stderr, timeout):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        state = signal_state(proc.pid)
        proc.send_signal(signal.SIGABRT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        stderr.seek(0)
        stack = stderr.read().decode(errors="replace")
        raise AssertionError(
            f"child did not exit within {timeout}s of SIGTERM\n"
            f"signal state before abort:\n{state}\n"
            f"exit code after SIGABRT: {proc.returncode}\n"
            f"stderr with faulthandler stack:\n{stack}"
        ) from None


class StopOrReportStack(unittest.TestCase):
    def test_a_child_that_ignores_sigterm_fails_with_its_stack_and_signal_state(self):
        ready = core.LEDGER_DIR / "hang.ready"
        ready.unlink(missing_ok=True)
        code = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "def hang():\n"
            "    open(sys.argv[1], 'w').close()\n"
            "    time.sleep(60)\n"
            "hang()\n"
        )
        with open(core.LEDGER_DIR / "hang.stderr", "w+b") as stderr:
            proc = subprocess.Popen(
                [sys.executable, "-X", "faulthandler", "-c", code, str(ready)], stdout=subprocess.DEVNULL, stderr=stderr
            )
            try:
                for _ in range(50):
                    if ready.exists():
                        break
                    time.sleep(0.1)
                self.assertTrue(ready.exists())
                with self.assertRaises(AssertionError) as caught:
                    stop_or_report_stack(proc, stderr, timeout=0.5)
            finally:
                if proc.poll() is None:
                    proc.kill()
        message = str(caught.exception)
        self.assertIn("within 0.5s of SIGTERM", message)
        self.assertIn("Fatal Python error: Aborted", message)
        self.assertIn("in hang", message)
        self.assertIn(f"exit code after SIGABRT: {-signal.SIGABRT}", message)
        ignored = int(re.search(r"SigIgn:\s+([0-9a-f]+)", message).group(1), 16)
        self.assertTrue(ignored & (1 << (signal.SIGTERM - 1)))


class Watch(unittest.TestCase):
    def setUp(self):
        make_ledger()

    def test_watcher_removes_its_beat_file_on_sigterm(self):
        beat = core.watch_path(SLUG, "watcher")
        env = {**os.environ, "LEDGER_DIR": str(core.LEDGER_DIR), "LEDGER_PORT": "9"}
        plant = "import runpy, signal, sys; signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM}); sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
        command = [
            sys.executable,
            "-X",
            "faulthandler",
            "-c",
            plant,
            str(SCRIPTS / "watch_ledger.py"),
            SLUG,
            "--as",
            "watcher",
            "--interval",
            "0.1",
        ]
        with open(core.LEDGER_DIR / "watcher.stderr", "w+b") as stderr:
            proc = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=stderr)
            try:
                for _ in range(50):
                    if beat.exists():
                        break
                    time.sleep(0.1)
                self.assertTrue(beat.exists())
                stop_or_report_stack(proc, stderr, timeout=5)
            finally:
                if proc.poll() is None:
                    proc.kill()
        self.assertFalse(beat.exists())


if __name__ == "__main__":
    unittest.main()
