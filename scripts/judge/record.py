import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path


def digest(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build(case: dict, evidence: dict, verdict: dict, harness: str, model: str, now: datetime | None = None) -> dict:
    return {
        "case_sha256": digest(case),
        "evidence_sha256": digest(evidence),
        "verdict": verdict["verdict"],
        "notes": verdict["notes"],
        "harness": harness,
        "model": model,
        "at": (now or datetime.now(UTC)).isoformat(),
    }


def write(path: Path, entry: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, indent=2) + "\n")
    return path
