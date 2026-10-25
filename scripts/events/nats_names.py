import re

from nats.js.errors import NotFoundError

from . import nkey
from .memory import subject_matches

LEGACY_CONSUMER = re.compile(r"[A-Za-z0-9_-]+")


async def existing_stream(js, desired: str, legacy: str) -> str:
    for name in (legacy, desired):
        try:
            await js.stream_info(name)
        except NotFoundError:
            continue
        return name
    return desired


async def root_stream(js, desired: str, legacy: str, credentials: str | None) -> str:
    if credentials is not None:
        permissions = nkey.claims(nkey.parse_creds(credentials)[0])["nats"].get("pub", {})
        request = f"$JS.API.STREAM.INFO.{legacy}"
        allow, deny = permissions.get("allow", []), permissions.get("deny", [])
        allowed = not allow or any(subject_matches(pattern, request) for pattern in allow)
        if not allowed or any(subject_matches(pattern, request) for pattern in deny):
            return legacy
    return await existing_stream(js, desired, legacy)


async def existing_consumer(js, stream: str, desired: str, legacy: str) -> str:
    if desired == legacy or not LEGACY_CONSUMER.fullmatch(legacy):
        return desired
    for name in (legacy, desired):
        try:
            await js.consumer_info(stream, name)
        except NotFoundError:
            continue
        return name
    return desired
