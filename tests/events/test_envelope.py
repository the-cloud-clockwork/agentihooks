import json

import pytest

from scripts.events import envelope
from scripts.events.envelope import SCHEMA_VERSION, Envelope

pytestmark = pytest.mark.unit


def _fields(**changes):
    fields = {
        "id": "e1",
        "type": "task.claimed",
        "swarm": "crew",
        "entity": "task",
        "key": "t1",
        "version": 3,
        "time": 1700000000000,
        "author": "engineer@1-1",
        "cause": "e0",
        "data": {"lane": "eng"},
        "schema_version": SCHEMA_VERSION,
    }
    fields.update(changes)
    return fields


def test_envelope_carries_every_field_as_a_plain_dict():
    event = Envelope(**_fields())
    assert event.to_dict() == _fields()


def test_envelope_round_trips_through_json_bytes():
    event = Envelope(**_fields())
    raw = event.encode()
    assert json.loads(raw) == _fields()
    assert Envelope.decode(raw) == event


def test_envelope_without_a_cause_round_trips():
    event = Envelope(**_fields(cause=None))
    assert Envelope.decode(event.encode()).cause is None


def test_from_dict_refuses_a_missing_field():
    fields = _fields()
    del fields["author"]
    with pytest.raises(ValueError, match="event envelope fields differ: missing author"):
        Envelope.from_dict(fields)


def test_from_dict_refuses_an_unknown_field():
    with pytest.raises(ValueError, match="event envelope fields differ: unknown extra"):
        Envelope.from_dict(_fields(extra=1))


def test_envelope_refuses_another_schema_version():
    with pytest.raises(ValueError, match="unsupported event schema version 2"):
        Envelope(**_fields(schema_version=SCHEMA_VERSION + 1))


@pytest.mark.parametrize("name", ["id", "type", "swarm", "entity", "key", "author"])
def test_envelope_refuses_an_empty_text_field(name):
    with pytest.raises(ValueError, match=f"event {name} must be non empty text"):
        Envelope(**_fields(**{name: ""}))


@pytest.mark.parametrize("value", [-1, True, "3"])
def test_envelope_refuses_a_bad_version(value):
    with pytest.raises(ValueError, match="event version must be a non negative integer"):
        Envelope(**_fields(version=value))


@pytest.mark.parametrize("value", [-1, False, 1.5])
def test_envelope_refuses_a_bad_time(value):
    with pytest.raises(ValueError, match="event time must be a non negative integer"):
        Envelope(**_fields(time=value))


def test_envelope_refuses_an_empty_cause():
    with pytest.raises(ValueError, match="event cause must be non empty text or None"):
        Envelope(**_fields(cause=""))


def test_envelope_refuses_data_that_is_not_a_dict():
    with pytest.raises(ValueError, match="event data must be a dict"):
        Envelope(**_fields(data=[1]))


def _created(**changes):
    fields = _fields(**changes)
    del fields["id"], fields["time"], fields["schema_version"]
    if "cause" not in changes:
        del fields["cause"]
    return fields


def test_create_refuses_a_caller_id():
    with pytest.raises(TypeError):
        envelope.create(**_created(), id="mine")


def test_create_stamps_a_fresh_id_the_clock_and_the_schema_version(monkeypatch):
    monkeypatch.setattr(envelope.time, "time_ns", lambda: 1_700_000_000_123_456_789)
    first = envelope.create(**_created())
    second = envelope.create(**_created())
    assert first.time == 1_700_000_000_123
    assert first.schema_version == SCHEMA_VERSION
    assert first.cause is None
    assert len(first.id) == 32
    assert first.id != second.id
    assert first.to_dict() | {"id": "x"} == second.to_dict() | {"id": "x"}


def test_create_passes_a_cause_through():
    event = envelope.create(**_created(cause="e1"))
    assert event.cause == "e1"
