import json
import socket
import threading

import pytest

from scripts.hive import auth, server

PUBLIC = "redis://hive.example:6380/2"


@pytest.fixture
def admin():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def port(admin):
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _post(port, path, body):
    payload = json.dumps(body).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(b"POST %s HTTP/1.0\r\nContent-Length: %d\r\n\r\n%s" % (path.encode(), len(payload), payload))
        reply = b""
        while chunk := conn.recv(65536):
            reply += chunk
    head, _, rest = reply.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(rest)


def _raises(error):
    def operation(*_, **__):
        raise error

    return operation


def _wrapped(cause):
    try:
        raise auth.HiveError("Redis refused the new member") from cause
    except auth.HiveError as exc:
        return exc


@pytest.mark.parametrize("kind", ["TimeoutError", "ConnectionError"])
@pytest.mark.parametrize("wrap", [False, True])
def test_a_redis_that_does_not_answer_gets_a_fixed_503(port, kind, wrap, monkeypatch):
    from redis import exceptions

    error = getattr(exceptions, kind)("Timeout reading from socket")
    monkeypatch.setattr(auth, "refresh", _raises(_wrapped(error) if wrap else error))

    assert _post(port, server.REFRESH_PATH, {"ledger_credential": "any"}) == (503, {"error": server.REDIS_STALLED})


def test_a_refusal_with_another_cause_stays_a_403(port, monkeypatch):
    monkeypatch.setattr(auth, "refresh", _raises(_wrapped(ValueError())))

    assert _post(port, server.REFRESH_PATH, {"ledger_credential": "any"}) == (
        403,
        {"error": "Redis refused the new member"},
    )


def test_any_redis_error_on_the_credential_lookup_is_a_refusal(admin, port, monkeypatch):
    from redis.exceptions import RedisError

    monkeypatch.setattr(admin, "transaction", _raises(RedisError("unknown reply from the server")))

    assert _post(port, server.REFRESH_PATH, {"ledger_credential": "any"}) == (
        403,
        {"error": "Redis refused the refreshed member (unknown reply from the server)"},
    )


@pytest.mark.parametrize("kind", ["TimeoutError", "ConnectionError"])
def test_a_stalled_credential_lookup_stays_a_503(admin, port, kind, monkeypatch):
    from redis import exceptions

    monkeypatch.setattr(admin, "transaction", _raises(getattr(exceptions, kind)("Timeout reading from socket")))

    assert _post(port, server.REFRESH_PATH, {"ledger_credential": "any"}) == (503, {"error": server.REDIS_STALLED})


def test_the_hive_endpoint_redis_client_drops_connections_made_before_its_bound(admin):
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    admin.set_retry(Retry(NoBackoff(), 3))
    early = admin.connection_pool.get_connection()
    admin.connection_pool.release(early)

    with server.make_server(admin, PUBLIC, "127.0.0.1", 0):
        late = admin.connection_pool.get_connection()

    assert late is not early
    assert (late.socket_timeout, late.socket_connect_timeout) == (server.REDIS_TIMEOUT_S, server.REDIS_TIMEOUT_S)
    assert late.retry.get_retries() == 0
    assert admin.get_retry().get_retries() == 0
