import pytest

from hooks import hook_manager
from hooks.context import tool_end


class Blocked(Exception):
    pass


@pytest.fixture
def ended(monkeypatch):
    calls = []
    monkeypatch.setattr(hook_manager, "_swarm_tool_end", calls.append)
    return calls


def test_denied_call_is_ended_and_its_block_raised(ended):
    payload = {"tool_use_id": "toolu_denied"}

    def handler(seen):
        assert seen is payload
        raise Blocked("denied")

    with pytest.raises(Blocked, match="denied"):
        tool_end.closing_denied(handler, payload, Blocked)
    assert ended == [payload]


def test_allowed_call_is_left_open(ended):
    seen = []
    tool_end.closing_denied(seen.append, {"tool_use_id": "toolu_allowed"}, Blocked)
    assert seen == [{"tool_use_id": "toolu_allowed"}]
    assert ended == []


def test_other_errors_leave_the_call_open(ended):
    def handler(payload):
        raise ValueError("broken")

    with pytest.raises(ValueError):
        tool_end.closing_denied(handler, {"tool_use_id": "toolu_broken"}, Blocked)
    assert ended == []
