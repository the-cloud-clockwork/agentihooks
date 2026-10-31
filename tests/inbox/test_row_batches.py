import socket
import threading

import pytest

from scripts.inbox.store import InboxStore


@pytest.fixture
def store():
    import fakeredis

    return InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))


def closing_pipelines(store, monkeypatch, closes):
    from redis.exceptions import ConnectionError as RedisConnectionError

    real = store.redis.pipeline
    sizes = []

    def pipeline(*args, **kwargs):
        pipe = real(*args, **kwargs)
        execute = pipe.execute

        def counted():
            sizes.append(len(pipe.command_stack))
            if len(sizes) <= closes:
                raise RedisConnectionError("Connection closed by server.")
            return execute()

        pipe.execute = counted
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    return sizes


def test_rows_are_read_in_pipelines_of_fifty_in_order(
    store: InboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = store.send("alice", "bob", "the last row")
    sizes = closing_pipelines(store, monkeypatch, closes=0)
    rows = store._rows([f"missing-{n}" for n in range(1000)] + [item.id])
    assert sizes == [50] * 20 + [1]
    assert len(rows) == 1001
    assert rows[:1000] == [{}] * 1000
    assert rows[1000]["text"] == "the last row"


def test_a_listing_reads_again_after_the_server_closes_the_connection(store, monkeypatch):
    item = store.send("alice", "bob", "still here")
    sizes = closing_pipelines(store, monkeypatch, closes=1)
    assert [(listed.id, listed.text) for listed in store.inbox("bob")] == [(item.id, "still here")]
    assert sizes == [1, 1]


def test_a_second_close_on_the_same_batch_is_raised(store, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    store.send("alice", "bob", "hi")
    sizes = closing_pipelines(store, monkeypatch, closes=2)
    with pytest.raises(RedisConnectionError, match="Connection closed by server"):
        store.inbox("bob")
    assert sizes == [1, 1]


def test_an_empty_read_opens_no_pipeline(store, monkeypatch):
    sizes = closing_pipelines(store, monkeypatch, closes=0)
    assert store._rows([]) == []
    assert sizes == []


class ClosingServer:
    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(5)
        self.port = self.listener.getsockname()[1]
        self.connections = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            conn.settimeout(5)
            self.connections.append(conn)
            try:
                self.answer(conn, closing=len(self.connections) == 1)
            except OSError:
                pass
            finally:
                conn.close()

    def answer(self, conn, closing):
        reader = conn.makefile("rb")
        while True:
            header = reader.readline()
            if not header:
                return
            words = []
            for _ in range(int(header[1:])):
                size = int(reader.readline()[1:])
                words.append(reader.read(size + 2)[:size])
            if words[0].upper() == b"HGETALL":
                if closing:
                    return
                conn.sendall(b"*2\r\n$4\r\ntext\r\n$4\r\nback\r\n")
            else:
                conn.sendall(b"+OK\r\n")

    def close(self):
        self.listener.close()
        self.thread.join(5)


def test_a_batch_is_read_again_on_a_new_connection_after_a_real_server_close():
    import redis

    server = ClosingServer()
    try:
        store = InboxStore(
            redis.Redis(host="127.0.0.1", port=server.port, protocol=2, decode_responses=True, socket_timeout=5)
        )
        assert store._rows(["one", "two"]) == [{"text": "back"}, {"text": "back"}]
        assert len(server.connections) == 2
    finally:
        server.close()
