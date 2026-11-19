from collections.abc import Mapping
from typing import Any

FIELDS = {"claude": "claude_model", "codex": "codex_model"}
DEFAULTS = {"claude": "opus", "codex": "gpt-6.1-sol"}
KEYS = {"claude-model": "claude_model", "codex-model": "codex_model"}
UNFIXED = ("", "auto")


def model(config: Any, harness: str) -> str:
    return getattr(config, FIELDS[harness], DEFAULTS[harness])


def refusal(changes: dict, catalog: dict) -> str:
    for harness, field in FIELDS.items():
        if field in changes and changes[field] not in [entry["model"] for entry in catalog.get(harness, [])]:
            return f"{field} must be a model listed in the {harness} catalog"
    return ""


def lane_models(lanes: Mapping[str, Mapping[str, str]], previous: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    """The lanes whose model the change sets to a new fixed value, each mapped to that model."""
    models = {lane: fields.get("model", "") for lane, fields in lanes.items()}
    return {
        lane: model
        for lane, model in models.items()
        if model not in UNFIXED and model != previous.get(lane, {}).get("model")
    }


def lane_refusal(models: Mapping[str, str], catalog: Mapping[str, list[dict[str, Any]]]) -> str:
    """Why the first lane model that no harness catalog lists is refused, or empty when the catalogs list them all."""
    listed = {entry["model"] for entries in catalog.values() for entry in entries}
    for lane, model in models.items():
        if model not in listed:
            return f"{lane}-model must be auto or a model listed in a harness catalog, not {model}"
    return ""
