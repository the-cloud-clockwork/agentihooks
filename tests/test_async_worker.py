import re
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.unit

HELD_MODULE = """
import sys

gate = getattr(sys, "async_worker_gate", None)
if gate is not None:
    gate[0].set()
    gate[1].wait()
"""

JOB_MODULE = """
import time


def import_held(marker):
    import held_by_thread

    open(marker, "w").write("done")


def sleep_past(seconds):
    time.sleep(seconds)
"""


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    from hooks import _async

    (tmp_path / "held_by_thread.py").write_text(HELD_MODULE)
    (tmp_path / "async_jobs.py").write_text(JOB_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(_async, "_LOG_FILE", tmp_path / "logs" / "async-hooks.log")
    yield tmp_path
    for name in ("held_by_thread", "async_jobs"):
        sys.modules.pop(name, None)


def _wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_job_completes_while_another_thread_holds_an_import_lock(jobs, monkeypatch):
    from hooks import _async

    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(sys, "async_worker_gate", (entered, release), raising=False)
    holder = threading.Thread(target=lambda: __import__("held_by_thread"), name="holder", daemon=True)
    holder.start()
    assert entered.wait(5)
    import async_jobs

    marker = jobs / "marker"
    try:
        _async.fork_and_call(async_jobs.import_held, str(marker), timeout_sec=5, task_name="held-lock")
        assert _wait_for(marker.exists, 8), (jobs / "logs" / "async-hooks.log").read_text()
    finally:
        release.set()
        holder.join(5)


def test_timeout_is_logged_with_time_and_task(jobs):
    import async_jobs

    from hooks import _async

    log = jobs / "logs" / "async-hooks.log"
    _async.fork_and_call(async_jobs.sleep_past, 10, timeout_sec=1, task_name="slow-job")
    assert _wait_for(lambda: log.exists() and "TIMEOUT" in log.read_text(), 8)
    assert re.search(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \[async\] slow-job: TIMEOUT after 1s$", log.read_text(), re.M)
