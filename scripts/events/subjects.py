import re

PREFIX = "swarm"

_TOKEN = re.compile(r"[^.*>\s]+")


def _token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"invalid event subject token {value!r}")
    return value


def subject(swarm: str, *tokens: str) -> str:
    if not tokens:
        raise ValueError("event subject needs at least one token")
    return ".".join([PREFIX, _token(swarm), *map(_token, tokens)])
