FIELDS = {"claude": "claude_model", "codex": "codex_model"}
DEFAULTS = {"claude": "opus", "codex": "gpt-6.1-sol"}
KEYS = {"claude-model": "claude_model", "codex-model": "codex_model"}


def model(config, harness: str) -> str:
    return getattr(config, FIELDS[harness], DEFAULTS[harness])


def refusal(changes: dict, catalog: dict) -> str:
    for harness, field in FIELDS.items():
        if field in changes and changes[field] not in [entry["model"] for entry in catalog.get(harness, [])]:
            return f"{field} must be a model listed in the {harness} catalog"
    return ""
