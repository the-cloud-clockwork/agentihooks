import json
from dataclasses import dataclass
from pathlib import Path

from hooks.classifier import ClassifierError, YesNo, decide
from scripts.headless import runner
from scripts.headless.request import READ_ONLY

SCOUTER = Path(__file__).resolve().parents[2] / "profiles" / "package" / "agents" / "scouter.md"
DEFAULT_ROUNDS = 3
EFFORT = "low"
PURPOSE = "scout-until"
MET = 0.5
FIELDS = ("claim", "source", "excerpt", "retrieved_at")
REPLY_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": list(FIELDS),
                "additionalProperties": False,
                "properties": {field: {"type": "string"} for field in FIELDS},
            },
        }
    },
}


@dataclass(frozen=True)
class Scout:
    question: str
    until: str | None
    rounds: int
    tier: str
    harness: str


def scouter_prompt() -> str:
    text = SCOUTER.read_text()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def round_input(scout: Scout, items: list[dict]) -> str:
    known = [{"source": item["source"], "excerpt": item["excerpt"]} for item in items]
    return json.dumps({"question": scout.question, "until": scout.until, "rounds": 1, "known": known})


def merged(items: list[dict], found: list[dict]) -> list[dict]:
    seen = {(item["source"], item["excerpt"]) for item in items}
    out = list(items)
    for item in found:
        key = (item["source"], item["excerpt"])
        if key not in seen:
            seen.add(key)
            out.append({"id": f"e{len(out) + 1}", **{field: item[field] for field in FIELDS}})
    return out


def until_met(scout: Scout, items: list[dict]) -> bool:
    question = YesNo(
        f"Does this evidence set meet: {scout.until}?",
        true="The evidence set meets the condition",
        false="The evidence set does not meet the condition yet",
    )
    state = {"question": scout.question, "until": scout.until, "evidence": {"items": items}}
    try:
        result = decide(state, {"met": question}, purpose=PURPOSE, harness=scout.harness)
    except ClassifierError:
        return False
    return (result.answers["met"].noul or 0.0) >= MET


def run(scout: Scout) -> dict:
    prompt = scouter_prompt()
    items: list[dict] = []
    for _ in range(scout.rounds):
        reply = runner.run(
            prompt, round_input(scout, items), REPLY_SCHEMA, READ_ONLY, scout.tier, EFFORT, scout.harness
        )
        items = merged(items, reply["items"])
        if scout.until and until_met(scout, items):
            break
    return {"items": items}
