"""Names of the per swarm execution stop stream and of the one stop consumer each hive reads in it."""

import hashlib
import json

from scripts.events.subjects import STOPS, consumer_name, stream_name

FAMILY = STOPS.lower()


def stream(swarm: str) -> str:
    """The JetStream stream that holds every execution stop of one swarm."""
    return stream_name(swarm, STOPS)


def durable(swarm: str, hive: str) -> str:
    """The durable consumer one hive reads its stops through in a swarm."""
    return f"execution-stop-{hashlib.sha256(json.dumps([swarm, hive]).encode()).hexdigest()}"


def consumer(swarm: str, hive: str) -> str:
    """The consumer name the server holds for one hive's stop durable in a swarm."""
    return consumer_name(durable(swarm, hive))
