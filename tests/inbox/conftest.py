import signal

import pytest


@pytest.fixture
def recovery_deadline():
    def expired(signum, frame):
        raise AssertionError("recovery loop exceeded its deadline")

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
