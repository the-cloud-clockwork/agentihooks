from nats.js.errors import NotFoundError

from .resource_names import consumer_name


async def existing_stream(js, desired: str, legacy: str) -> str:
    for name in (desired, legacy):
        try:
            await js.stream_info(name)
        except NotFoundError:
            continue
        return name
    return desired


async def existing_consumer(js, stream: str, durable: str) -> str:
    desired = consumer_name(durable)
    if desired == durable:
        return desired
    for name in (desired, durable):
        try:
            await js.consumer_info(stream, name)
        except NotFoundError:
            continue
        return name
    return desired
