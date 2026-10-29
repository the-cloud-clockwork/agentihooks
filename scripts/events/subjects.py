import re

from .resource_names import consumer_name

PREFIX = "swarm"
SLOTS = "slots"

_TOKEN = re.compile(r"[^.*>\s]+")


def _token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"invalid event subject token {value!r}")
    return value


def subject(swarm: str, *tokens: str) -> str:
    if not tokens:
        raise ValueError("event subject needs at least one token")
    return ".".join([PREFIX, _token(swarm), *map(_token, tokens)])


def slot_request(swarm: str, hive: str) -> str:
    return ".".join([SLOTS, _token(swarm), _token(hive)])


def in_swarm(swarm: str, subject: str) -> bool:
    return subject.startswith((f"{PREFIX}.{swarm}.", f"{SLOTS}.{swarm}."))


def inbox(user: str) -> str:
    return f"_INBOX.{_token(user)}"


def stream_name(swarm: str, resource: str) -> str:
    return f"AGENTIHOOKS-{_token(swarm).encode().hex()}-{consumer_name(_token(resource))}"
