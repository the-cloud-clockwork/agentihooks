import json

import pytest

from scripts.codex_session import protocol

pytestmark = pytest.mark.unit


class Socket:
    def __init__(self, replies=(), recv_error=None):
        self.replies = list(replies)
        self.sent = []
        self.closed = False
        self.recv_error = recv_error

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout):
        if self.recv_error:
            raise self.recv_error
        if not self.replies:
            raise TimeoutError("nothing to read")
        return json.dumps(self.replies.pop(0))

    def close(self):
        self.closed = True


def test_a_request_skips_notifications_and_server_requests_until_its_reply():
    ws = Socket(
        [
            {"method": "thread/status/changed", "params": {}},
            {"id": 1, "method": "item/commandExecution/requestApproval", "params": {}},
            {"id": 2, "result": {"other": True}},
            {"id": 1, "result": {"thread": {"id": "t"}}},
        ]
    )
    assert protocol.Connection(ws, 5).request("thread/start", {"cwd": "/repo"}) == {"thread": {"id": "t"}}
    assert ws.sent == [{"jsonrpc": "2.0", "id": 1, "method": "thread/start", "params": {"cwd": "/repo"}}]


def test_an_error_reply_raises_with_its_message():
    ws = Socket([{"id": 1, "error": {"code": -32600, "message": "no rollout found"}}])
    with pytest.raises(protocol.ProtocolError, match="thread/resume refused: no rollout found"):
        protocol.Connection(ws, 5).request("thread/resume", {"threadId": "x"})


def test_a_request_with_no_reply_times_out():
    now = iter([0.0, 0.0, 10.0])
    ws = Socket([{"method": "noise"}])
    with pytest.raises(TimeoutError, match="no reply to account/read"):
        protocol.Connection(ws, 5, clock=lambda: next(now)).request("account/read")


def test_a_lost_connection_is_a_protocol_error():
    from websockets.exceptions import ConnectionClosedError

    ws = Socket(recv_error=ConnectionClosedError(None, None))
    with pytest.raises(protocol.ProtocolError, match="^hooks/list lost the connection: ConnectionClosedError$"):
        protocol.Connection(ws, 5).request("hooks/list")


def test_a_frame_that_is_not_json_is_a_protocol_error():
    class Garbled(Socket):
        def recv(self, timeout):
            return "not json"

    with pytest.raises(protocol.ProtocolError, match="thread/start got a frame that is not JSON"):
        protocol.Connection(Garbled(), 5).request("thread/start")


def test_a_request_hands_notifications_and_server_requests_to_the_listener():
    events = [{"method": "turn/started", "params": {"turn": {"id": "t1"}}}, {"id": 1, "method": "x/requestApproval"}]
    ws = Socket([*events, {"id": 1, "result": {}}])
    connection = protocol.Connection(ws, 5)
    seen = []
    connection.listener = seen.append
    connection.request("thread/read")
    assert seen == events


def test_an_error_reply_is_a_definitive_refusal():
    ws = Socket([{"id": 1, "error": {"message": "expected turn mismatch"}}])
    with pytest.raises(protocol.Refused) as refused:
        protocol.Connection(ws, 5).request("turn/steer")
    assert str(refused.value) == "turn/steer refused: expected turn mismatch"


def test_a_lost_connection_is_not_a_refusal():
    from websockets.exceptions import ConnectionClosedError

    with pytest.raises(protocol.ProtocolError) as lost:
        protocol.Connection(Socket(recv_error=ConnectionClosedError(None, None)), 5).request("turn/start")
    assert not isinstance(lost.value, protocol.Refused)


def test_the_default_listener_ignores_events():
    assert protocol.ignore({"method": "turn/started"}) is None
    assert protocol.Connection(Socket(), 5).listener is protocol.ignore


def test_poll_hands_one_event_to_the_listener():
    event = {"method": "turn/completed", "params": {"turn": {"id": "t1"}}}
    connection = protocol.Connection(Socket([event, {"method": "later"}]), 5)
    seen = []
    connection.listener = seen.append
    assert connection.poll(0.1) is True
    assert seen == [event]


def test_poll_skips_a_stray_response_and_reports_a_frame():
    connection = protocol.Connection(Socket([{"id": 9, "result": {}}]), 5)
    seen = []
    connection.listener = seen.append
    assert connection.poll(0.1) is True
    assert seen == []


def test_poll_with_nothing_to_read_returns_false():
    timeouts = []

    class Quiet(Socket):
        def recv(self, timeout):
            timeouts.append(timeout)
            raise TimeoutError("quiet")

    assert protocol.Connection(Quiet(), 5).poll(0.25) is False
    assert timeouts == [0.25]


def test_poll_on_a_lost_connection_is_a_protocol_error():
    from websockets.exceptions import ConnectionClosedError

    connection = protocol.Connection(Socket(recv_error=ConnectionClosedError(None, None)), 5)
    with pytest.raises(protocol.ProtocolError) as lost:
        connection.poll(0.1)
    assert str(lost.value) == "the event stream lost the connection: ConnectionClosedError"


def test_poll_refuses_a_frame_that_is_not_json():
    class Garbled(Socket):
        def recv(self, timeout):
            return "stderr text"

    with pytest.raises(protocol.ProtocolError) as garbled:
        protocol.Connection(Garbled(), 5).poll(0.1)
    assert str(garbled.value) == "the event stream got a frame that is not JSON"


def test_poll_refuses_json_that_is_not_an_object():
    class Scalar(Socket):
        def recv(self, timeout):
            return "null"

    with pytest.raises(protocol.ProtocolError) as scalar:
        protocol.Connection(Scalar(), 5).poll(0.1)
    assert str(scalar.value) == "the event stream got a frame that is not a JSON object"


def test_a_request_refuses_json_that_is_not_an_object():
    class Listed(Socket):
        def recv(self, timeout):
            return "[1]"

    with pytest.raises(protocol.ProtocolError) as listed:
        protocol.Connection(Listed(), 5).request("thread/read")
    assert str(listed.value) == "thread/read got a frame that is not a JSON object"


def test_steer_turn_sends_the_expected_turn_and_returns_the_steered_one():
    ws = Socket([{"id": 1, "result": {"turnId": "t2"}}])
    assert protocol.steer_turn(protocol.Connection(ws, 5), "th", "t1", "hi") == "t2"
    assert ws.sent == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn/steer",
            "params": {"threadId": "th", "expectedTurnId": "t1", "input": [{"type": "text", "text": "hi"}]},
        }
    ]


def test_steer_turn_keeps_the_expected_turn_when_the_reply_names_none():
    assert protocol.steer_turn(protocol.Connection(Socket([{"id": 1, "result": {}}]), 5), "th", "t1", "hi") == "t1"


def test_read_thread_asks_for_turns_and_returns_the_thread():
    ws = Socket([{"id": 1, "result": {"thread": {"id": "th", "turns": []}}}])
    assert protocol.read_thread(protocol.Connection(ws, 5), "th") == {"id": "th", "turns": []}
    assert ws.sent[0]["method"] == "thread/read"
    assert ws.sent[0]["params"] == {"threadId": "th", "includeTurns": True}


def test_read_thread_without_a_thread_is_empty():
    assert protocol.read_thread(protocol.Connection(Socket([{"id": 1, "result": {}}]), 5), "th") == {}


def test_connect_initializes_then_announces_initialized():
    ws = Socket([{"id": 1, "result": {"codexHome": "/home/codex"}}])
    opened = []

    def opener(path, open_timeout, max_size):
        opened.append((path, open_timeout, max_size))
        return ws

    connection, info = protocol.connect("/run/cx/s", 3.0, opener)
    assert info == {"codexHome": "/home/codex"}
    assert opened == [("/run/cx/s", 3.0, None)]
    assert [message["method"] for message in ws.sent] == ["initialize", "initialized"]
    assert ws.sent[0]["params"] == {"clientInfo": {"name": protocol.CLIENT_NAME, "version": "1"}}
    assert ws.sent[1] == {"jsonrpc": "2.0", "method": "initialized"}
    assert connection.ws is ws


def test_connect_wraps_a_refused_socket():
    def refused(path, open_timeout, max_size):
        raise ConnectionRefusedError("no listener")

    with pytest.raises(protocol.ProtocolError, match="cannot open the app-server socket: ConnectionRefusedError"):
        protocol.connect("/run/cx/s", 1.0, refused)


def test_connect_closes_the_socket_when_initialize_fails():
    ws = Socket()
    with pytest.raises(TimeoutError):
        protocol.connect("/run/cx/s", 1.0, lambda path, open_timeout, max_size: ws)
    assert ws.closed


def test_the_method_helpers_send_the_qualified_shapes():
    ws = Socket(
        [
            {
                "id": 1,
                "result": {"data": [{"hooks": [{"eventName": "stop"}]}, {"hooks": [{"eventName": "sessionStart"}]}]},
            },
            {"id": 2, "result": {}},
            {"id": 3, "result": {"account": {"type": "chatgpt"}}},
            {"id": 4, "result": {"thread": {"id": "t1"}}},
            {"id": 5, "result": {"thread": {"id": "t2"}}},
            {"id": 6, "result": {"turn": {"id": "u1"}}},
            {"id": 7, "result": {}},
        ]
    )
    connection = protocol.Connection(ws, 5)
    assert [hook["eventName"] for hook in protocol.hooks(connection, "/repo")] == ["stop", "sessionStart"]
    protocol.trust(connection, [{"key": "/h/hooks.json:stop:0", "currentHash": "abc"}])
    assert protocol.account_type(connection) == "chatgpt"
    assert protocol.start_thread(connection, "/repo") == {"id": "t1"}
    assert protocol.resume_thread(connection, "t2") == {"id": "t2"}
    assert protocol.start_turn(connection, "t2", "hello") == "u1"
    assert protocol.account_type(connection) == ""
    assert [message["method"] for message in ws.sent] == [
        "hooks/list",
        "config/batchWrite",
        "account/read",
        "thread/start",
        "thread/resume",
        "turn/start",
        "account/read",
    ]
    params = [message["params"] for message in ws.sent]
    assert params[0] == {"cwds": ["/repo"]}
    assert params[2] == {}
    assert params[1] == {
        "edits": [
            {"keyPath": 'hooks.state."/h/hooks.json:stop:0".trusted_hash', "value": "abc", "mergeStrategy": "replace"}
        ],
        "reloadUserConfig": True,
    }
    assert params[3] == {"cwd": "/repo"}
    assert params[4] == {"threadId": "t2"}
    assert params[5] == {"threadId": "t2", "input": [{"type": "text", "text": "hello"}]}


class Timed(Socket):
    def __init__(self, replies=()):
        super().__init__(replies)
        self.timeouts = []

    def recv(self, timeout):
        self.timeouts.append(timeout)
        return super().recv(timeout)


def test_a_reply_inside_the_last_second_is_read_with_the_time_left():
    now = iter([0.0, 4.5])
    ws = Timed([{"id": 1, "result": {"ok": True}}])
    assert protocol.Connection(ws, 5, clock=lambda: next(now)).request("account/read") == {"ok": True}
    assert ws.timeouts == [0.5]


def test_no_time_left_times_out_without_reading():
    now = iter([0.0, 5.0])
    ws = Timed([{"id": 1, "result": {}}])
    with pytest.raises(TimeoutError, match="^no reply to account/read$"):
        protocol.Connection(ws, 5, clock=lambda: next(now)).request("account/read")
    assert ws.timeouts == []


def test_an_error_reply_without_a_message_says_so():
    ws = Socket([{"id": 1, "error": {"code": -32600}}])
    with pytest.raises(protocol.ProtocolError) as refused:
        protocol.Connection(ws, 5).request("thread/resume", {"threadId": "x"})
    assert str(refused.value) == "thread/resume refused: no message"


def test_set_thread_name_names_the_thread_through_the_server():
    ws = Socket([{"id": 1, "result": {}}])
    protocol.set_thread_name(protocol.Connection(ws, 5), "t2", "engineer@abc-0001")
    assert [(message["method"], message["params"]) for message in ws.sent] == [
        ("thread/name/set", {"threadId": "t2", "name": "engineer@abc-0001"})
    ]


def test_a_turn_reply_without_an_id_has_an_empty_turn_id():
    ws = Socket([{"id": 1, "result": {"turn": {}}}])
    assert protocol.start_turn(protocol.Connection(ws, 5), "t", "hello") == ""
