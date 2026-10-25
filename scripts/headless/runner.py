import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

import jsonschema

from scripts.headless import claude, codex, tiers
from scripts.headless.request import READ_ONLY, SEALED, HeadlessError, Request

ADAPTERS = {"claude": claude, "codex": codex}
CLAMPS = (SEALED, READ_ONLY)
ATTEMPTS = 2


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
    routed = adapter.route(os.environ)
    for _ in range(ATTEMPTS):
        with tempfile.TemporaryDirectory(prefix="headless-") as work:
            reply = adapter.invoke(request, routed, Path(work))
        try:
            return validated(reply, schema)
        except (ValueError, jsonschema.ValidationError) as exc:
            error = exc
    raise HeadlessError(f"{name} returned no reply matching the schema after {ATTEMPTS} attempts: {error}")
