"""Creates the bus's JetStream streams at its replica count and raises existing streams in place, never lowering one."""

from dataclasses import replace

from nats.js import JetStreamContext
from nats.js.api import StreamConfig, StreamInfo
from nats.js.errors import APIError

NAME_IN_USE = 10058


def held(info: StreamInfo) -> int:
    """Return the replica count a stream holds; a configuration without the field holds one."""
    return info.config.num_replicas or 1


async def raised(js: JetStreamContext, info: StreamInfo, replicas: int) -> StreamInfo:
    """Return the stream raised in place to the count when it holds fewer replicas, its data and settings kept."""
    if held(info) >= replicas:
        return info
    return await js.update_stream(config=replace(info.config, num_replicas=replicas))  # pyright: ignore[reportUnknownMemberType]


async def create(js: JetStreamContext, replicas: int, name: str, config: StreamConfig) -> StreamInfo:
    """Create the stream ``name`` from ``config`` at the count; an existing stream is raised to it or keeps a higher
    count it holds.

    Any other difference from the existing stream's configuration still fails with the server's name in use error.
    """
    try:
        return await js.add_stream(config, name=name, num_replicas=replicas)  # pyright: ignore[reportUnknownMemberType]
    except APIError as exc:
        if exc.err_code != NAME_IN_USE:
            raise
        info = await js.stream_info(name)
        if held(info) == replicas:
            raise
    info = await raised(js, info, replicas)
    return await js.add_stream(config, name=name, num_replicas=held(info))  # pyright: ignore[reportUnknownMemberType]


async def raise_all(js: JetStreamContext, replicas: int) -> None:
    """Raise every stream of the account holding fewer replicas than the count, in place.

    A stream electing its leader is missing from a listing, so it waits for the next provisioning bus to start.
    """
    offset = 0
    while page := await js.streams_info(offset=offset):
        for info in page:
            await raised(js, info, replicas)
        offset += len(page)
