from dataclasses import replace

from nats.js import JetStreamContext
from nats.js.errors import APIError

from .subjects import PREFIX

LEGACY_EVENTS = ["swarm.*.execution.>"]


async def provision_events(js: JetStreamContext, name: str, dedupe_window: float) -> None:
    try:
        await js.add_stream(name=name, subjects=LEGACY_EVENTS, duplicate_window=dedupe_window)
    except APIError as exc:
        if exc.err_code != 10058:
            raise
        info = await js.stream_info(name)
        subjects = info.config.subjects or []
        if f"{PREFIX}.>" in subjects:
            await js.update_stream(
                config=replace(
                    info.config, subjects=[value for value in subjects if value != f"{PREFIX}.>"] + LEGACY_EVENTS
                )
            )


async def capture(js: JetStreamContext, stream: str, logical: str) -> None:
    info = await js.stream_info(stream)
    if logical not in info.config.subjects:
        await js.update_stream(config=replace(info.config, subjects=[*info.config.subjects, logical]))
