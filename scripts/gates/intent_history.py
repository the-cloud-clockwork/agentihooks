import json
import re

from hooks.classifier.result import DecisionRequest
from hooks.secrets import redact
from scripts.gates.log import gates_dir


def append(slug: str, record: dict, home=None) -> None:
    path = gates_dir(slug, home) / "intent" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(record) + "\n")


def request(state: dict, questions: dict) -> str:
    return json.dumps(DecisionRequest(state, questions).wire())


def masked(value: object) -> object:
    if isinstance(value, dict):
        return {
            redact(key, mode="strict"): (
                "[REDACTED]"
                if re.search(r"password|secret|api.?key|private.?key|token|authorization|credential", key, re.I)
                else masked(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [masked(item) for item in value]
    if isinstance(value, str):
        safe = redact(re.sub(r"\bBearer\s+\S+", "Bearer [REDACTED]", value, flags=re.I), mode="strict")
        return "[REDACTED:private_key]" if "[REDACTED:private_key]" in safe else safe
    return value


def bounded(value: object, limit: int) -> object:
    text = json.dumps(value)
    if len(text) <= limit:
        return value
    return {"truncated": True, "json_prefix": text[: (limit - 64) // 2]}


def prepare(state: dict) -> dict:
    safe = masked(state)
    return {key: _bound(key, value) for key, value in safe.items()}


def _bound(key: str, value: object) -> object:
    if key == "pull_requests" and isinstance(value, list):
        return [{**item, **{part: bounded(item.get(part), 8192) for part in ("diff", "files")}} for item in value]
    return bounded(value, 32768 if key in ("reviewer_findings", "plan_chunk") else 8192)
