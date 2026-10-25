import builtins
import signal
import threading

import pytest

_setitimer = signal.setitimer


@pytest.fixture
def mcp_sdk():
    # An alarm inside the cold SDK import drops mcp.server but keeps mcp.server.stdio for the rest of the worker.
    import anyio._backends._asyncio  # noqa: F401
    import fakeredis  # noqa: F401
    import mcp.server.lowlevel  # noqa: F401
    import mcp.server.stdio  # noqa: F401
    import mcp.shared.message  # noqa: F401

    import scripts.hive.scope  # noqa: F401
    import scripts.inbox.pool  # noqa: F401
    import tests.inbox.test_channel_server  # noqa: F401

    yield


@pytest.fixture
def recovery_deadline(mcp_sdk, monkeypatch):
    def expired(signum, frame):
        raise AssertionError("recovery loop exceeded its deadline")

    real_import = builtins.__import__
    lock = threading.Lock()
    running = [0, 0.0]

    def paused(*args, **kwargs):
        # A cold import is not recovery work, and an alarm inside one leaves a module half loaded.
        with lock:
            running[0] += 1
            if running[0] == 1:
                running[1] = _setitimer(signal.ITIMER_REAL, 0)[0]
        try:
            return real_import(*args, **kwargs)
        finally:
            with lock:
                running[0] -= 1
                if running[0] == 0 and running[1]:
                    _setitimer(signal.ITIMER_REAL, running[1])

    monkeypatch.setattr(builtins, "__import__", paused)
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 5)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture
def channel_backend():
    import fakeredis

    from scripts.inbox.store import InboxStore

    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    return server, InboxStore(client)
