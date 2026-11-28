"""The lab heads names leaf holds the names the events bus, the lab scheduler and the lab relay bind."""

import pytest

from scripts.ci_lab import relay, scheduler
from scripts.events import lab_names, nats

pytestmark = pytest.mark.unit


@pytest.mark.feature("lab-scheduler-nats-grants")
def test_the_leaf_holds_the_stream_durables_and_subjects_the_events_bus_binds() -> None:
    leaf = (
        lab_names.LAB_STREAM,
        lab_names.LAB_SUBJECT,
        lab_names.LAB_CONSUMER,
        lab_names.RELAY_CONSUMER,
        lab_names.RELAY_SUBJECT,
        lab_names.LAB_DURABLES,
    )
    bus = (
        nats.LAB_STREAM,
        nats.LAB_SUBJECT,
        nats.LAB_CONSUMER,
        nats.RELAY_CONSUMER,
        nats.RELAY_SUBJECT,
        nats.LAB_DURABLES,
    )
    assert leaf == bus


@pytest.mark.feature("lab-scheduler-nats-grants")
def test_the_leaf_holds_the_durable_and_subject_the_lab_scheduler_binds() -> None:
    assert (lab_names.LAB_CONSUMER, lab_names.LAB_SUBJECT) == (scheduler.DURABLE, scheduler.SUBJECT)


@pytest.mark.feature("lab-scheduler-nats-grants")
def test_the_leaf_holds_the_durable_and_subject_the_lab_relay_binds() -> None:
    assert (lab_names.RELAY_CONSUMER, lab_names.RELAY_SUBJECT) == (relay.DURABLE, relay.SUBJECT)
