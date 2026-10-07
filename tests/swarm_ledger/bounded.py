import threading
import time


def bounded(call, *args, limit=2.0):
    """(call(*args), seconds taken) from a thread, failing the test instead of hanging when it never returns."""
    result = []
    thread = threading.Thread(target=lambda: result.append(call(*args)), daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(limit)
    assert not thread.is_alive(), f"{call.__name__} did not return within {limit} s"
    return result[0], time.monotonic() - started
