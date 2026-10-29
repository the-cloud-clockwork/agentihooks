import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

import jsonschema

from scripts import codex_router
from scripts.headless import claude, codex, tiers
from scripts.headless.request import READ_ONLY, SEALED, HeadlessError, Request

ADAPTERS = {"claude": claude, "codex": codex}
CLAMPS = (SEALED, READ_ONLY)
ATTEMPTS = 2
CLAUDE = "claude"
T = TypeVar("T")


class RouteRefused(HeadlessError):
    pass


def on_claude_if_refused(harness: str, ask: Callable[[str], T]) -> tuple[str, T]:
    try:
        return harness, ask(harness)
    except RouteRefused:
        return CLAUDE, ask(CLAUDE)


def harness_for(harness: str, environ: Mapping[str, str]) -> str:
    chosen = (environ.get("AGENTIHOOKS_TARGET") or "claude") if harness == "auto" else harness
    if chosen not in ADAPTERS:
        raise ValueError(f"no headless adapter for {chosen}")
    return chosen


def validated(reply: object, schema: dict) -> dict:
    value = json.loads(reply) if isinstance(reply, str) else reply
    jsonschema.validate(value, schema)
    return value


def run(
    profile_prompt: str, user_input: str, schema: dict, clamp: str, tier: str, effort: str, harness: str = "auto"
) -> dict:
    if clamp not in CLAMPS:
        raise ValueError(f"clamp must be one of {', '.join(CLAMPS)}")
    name = harness_for(harness, os.environ)
    request = Request(clamp, profile_prompt, user_input, schema, tiers.model(name, tier, os.environ), effort)
    adapter = ADAPTERS[name]
    try:
        routed = adapter.route(os.environ)
    except codex_router.RoutingError as exc:
        raise RouteRefused(f"{name} refused to route: {exc}") from exc
    for _ in range(ATTEMPTS):
        with tempfile.TemporaryDirectory() as work:
            reply = adapter.invoke(request, routed, Path(work))
        try:
            return validated(reply, schema)
        except (ValueError, jsonschema.ValidationError) as exc:
            error = exc
    raise HeadlessError(f"{name} returned no reply matching the schema after {ATTEMPTS} attempts: {error}")
