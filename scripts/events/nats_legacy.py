from dataclasses import replace

from nats.js import JetStreamContext
from nats.js.errors import APIError

from .memory import subject_matches
from .subjects import PREFIX

LEGACY_EVENTS = ["swarm.*.execution.>", "legacy.swarm.>"]


async def provision_events(js: JetStreamContext, name: str, dedupe_window: float) -> None:
    try:
        await js.add_stream(name=name, subjects=LEGACY_EVENTS, duplicate_window=dedupe_window)
    except APIError as exc:
        if exc.err_code != 10058:
            raise
        info = await js.stream_info(name)
        if info.config.subjects == LEGACY_EVENTS:
            raise
        await js.update_stream(config=replace(info.config, subjects=LEGACY_EVENTS))


def legacy_target(logical: str, permissions: dict) -> str:
    if not logical.startswith(f"{PREFIX}."):
        return logical
    candidate = f"legacy.{logical}"
    allow = permissions.get("allow", [])
    deny = permissions.get("deny", [])
    allowed = not allow or any(subject_matches(pattern, candidate) for pattern in allow)
    denied = any(subject_matches(pattern, candidate) for pattern in deny)
    return candidate if allowed and not denied else logical


def legacy_filters(logical: str) -> list[str]:
    return [logical, f"legacy.{logical}"] if logical.startswith(f"{PREFIX}.") else [logical]
