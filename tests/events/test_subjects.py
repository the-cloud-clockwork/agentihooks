import pytest

from scripts.events.subjects import PREFIX, inbox, stream_name, subject

pytestmark = pytest.mark.unit


def test_subject_applies_the_per_swarm_prefix():
    assert PREFIX == "swarm"
    assert subject("crew", "task", "t1") == "swarm.crew.task.t1"


def test_subjects_of_two_swarms_never_overlap():
    assert subject("a", "task") != subject("b", "task")


def test_subject_needs_at_least_one_token():
    with pytest.raises(ValueError, match=r"^event subject needs at least one token$"):
        subject("crew")


@pytest.mark.parametrize("token", ["", "a.b", "*", ">", "a b", "a\tb"])
def test_subject_refuses_a_token_nats_cannot_carry(token):
    with pytest.raises(ValueError, match="invalid event subject token"):
        subject("crew", token)


@pytest.mark.parametrize("swarm", ["", "a.b", "*", ">"])
def test_subject_refuses_a_swarm_nats_cannot_carry(swarm):
    with pytest.raises(ValueError, match="invalid event subject token"):
        subject(swarm, "task")


def test_inbox_is_the_reply_prefix_of_one_user():
    assert inbox("UABC") == "_INBOX.UABC"


@pytest.mark.parametrize("user", ["", "a.b", ">"])
def test_inbox_refuses_a_user_nats_cannot_carry(user):
    with pytest.raises(ValueError, match="invalid event subject token"):
        inbox(user)


def test_stream_names_carry_an_unambiguous_swarm_identity():
    assert stream_name("rig", "EVENTS") == "AGENTIHOOKS_726967_EVENTS"
    assert stream_name("other", "STATE_spoke-work") == "AGENTIHOOKS_6f74686572_STATE_spoke-work"
    assert stream_name("a_b", "EVENTS") != stream_name("a", "b_EVENTS")


@pytest.mark.parametrize("swarm,resource", [("bad.name", "EVENTS"), ("rig", "*"), (None, "EVENTS"), ("rig", None)])
def test_stream_names_refuse_invalid_identity_tokens(swarm, resource):
    with pytest.raises(ValueError, match="invalid event subject token"):
        stream_name(swarm, resource)
