import pytest

from scripts.events.subjects import PREFIX, subject

pytestmark = pytest.mark.unit


def test_subject_applies_the_per_swarm_prefix():
    assert PREFIX == "swarm"
    assert subject("crew", "task", "t1") == "swarm.crew.task.t1"


def test_subjects_of_two_swarms_never_overlap():
    assert subject("a", "task") != subject("b", "task")


def test_subject_needs_at_least_one_token():
    with pytest.raises(ValueError, match="event subject needs at least one token"):
        subject("crew")


@pytest.mark.parametrize("token", ["", "a.b", "*", ">", "a b", "a\tb"])
def test_subject_refuses_a_token_nats_cannot_carry(token):
    with pytest.raises(ValueError, match="invalid event subject token"):
        subject("crew", token)


@pytest.mark.parametrize("swarm", ["", "a.b", "*", ">"])
def test_subject_refuses_a_swarm_nats_cannot_carry(swarm):
    with pytest.raises(ValueError, match="invalid event subject token"):
        subject(swarm, "task")
