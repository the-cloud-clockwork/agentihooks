import pytest

from hooks.context import tool_end


class Blocked(Exception):
    pass


def test_denied_call_is_ended_and_its_block_raised():
    payload = {"tool_use_id": "toolu_denied"}
    ended = []

    def handler(seen):
        assert seen is payload
        raise Blocked("denied")

    with pytest.raises(Blocked, match="denied"):
        tool_end.closing_denied(handler, payload, Blocked, ended.append)
    assert ended == [payload]


def test_allowed_call_is_left_open():
    seen, ended = [], []
    tool_end.closing_denied(seen.append, {"tool_use_id": "toolu_allowed"}, Blocked, ended.append)
    assert seen == [{"tool_use_id": "toolu_allowed"}]
    assert ended == []


def test_other_errors_leave_the_call_open():
    ended = []

    def handler(payload):
        raise ValueError("broken")

    with pytest.raises(ValueError):
        tool_end.closing_denied(handler, {"tool_use_id": "toolu_broken"}, Blocked, ended.append)
    assert ended == []
