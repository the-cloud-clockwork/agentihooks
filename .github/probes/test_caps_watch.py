import json
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
WRAP = (
    "import faulthandler,signal,runpy,sys;"
    "faulthandler.register(signal.SIGUSR1, all_threads=True);"
    "sys.argv=sys.argv[1:];"
    "runpy.run_path(sys.argv[0], run_name='__main__')"
)
KEYS = ("COV", "COVERAGE", "AGENTI", "LEDGER", "REDIS", "SWARM", "PYTHON")


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


def read(path):
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        return f"unreadable: {exc}".encode()


def status_lines(pid):
    text = read(f"/proc/{pid}/status").decode(errors="replace")
    return [line for line in text.splitlines() if line.startswith(("State", "Sig", "ShdPnd", "Threads"))]


def env_keys(pid):
    raw = read(f"/proc/{pid}/environ")
    names = [item.split(b"=", 1)[0].decode(errors="replace") for item in raw.split(b"\0") if b"=" in item]
    return sorted(name for name in names if any(key in name for key in KEYS))


def diag(record):
    target = os.environ.get("PROBE_DIAG")
    if target:
        with open(target, "a") as handle:
            handle.write(json.dumps(record) + "\n")


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
        for attempt in range(int(os.environ.get("WATCH_REPEAT", "1"))):
            self.once(attempt)

    def once(self, attempt):
        beat = core.watch_path(SLUG, "watcher")
        env = {**os.environ, "LEDGER_DIR": str(core.LEDGER_DIR), "LEDGER_PORT": "9"}
        err = open(core.LEDGER_DIR / f"child-{os.getpid()}-{attempt}.err", "w+")
        started = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-c", WRAP, str(SCRIPTS / "watch_ledger.py"), SLUG, "--as", "watcher", "--interval", "0.1"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=err,
        )
        record = {"worker": os.environ.get("PYTEST_XDIST_WORKER"), "attempt": attempt}
        record["parent_mask"] = sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, []))
        record["parent_sigterm"] = str(signal.getsignal(signal.SIGTERM))
        try:
            for _ in range(50):
                if beat.exists():
                    break
                time.sleep(0.1)
            record["ready"] = time.monotonic() - started
            record["child_status_before"] = status_lines(proc.pid)
            record["child_env"] = env_keys(proc.pid)
            self.assertTrue(beat.exists())
            proc.send_signal(signal.SIGTERM)
            sent = time.monotonic()
            try:
                proc.wait(timeout=5)
                record["latency"] = time.monotonic() - sent
                record["code"] = proc.returncode
            except subprocess.TimeoutExpired:
                record["result"] = "timeout"
                record["child_status_timeout"] = status_lines(proc.pid)
                record["wchan"] = read(f"/proc/{proc.pid}/wchan").decode(errors="replace")
                record["children"] = subprocess.run(
                    ["ps", "--ppid", str(proc.pid), "-o", "pid,stat,wchan:30,cmd"], capture_output=True, text=True
                ).stdout
                proc.send_signal(signal.SIGUSR1)
                time.sleep(2)
                err.seek(0)
                record["stderr"] = err.read()[-12000:]
                raise
        finally:
            if proc.poll() is None:
                proc.kill()
            record["beat_left"] = beat.exists()
            diag(record)
        self.assertFalse(beat.exists())


if __name__ == "__main__":
    unittest.main()
