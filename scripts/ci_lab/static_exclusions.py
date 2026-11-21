import json

PATH = "tests/ci_lab/static_gate_exclusions.json"
CATALOG_ERROR = "static exclusion catalog must be an object"
RECORD_ERROR = "static exclusion record must be an object"
REASON_ERROR = "static exclusion reason must be nonempty text"
GATE_ERROR = "static exclusion gate must be nonempty text"


def _reason(value: object) -> str:
    if not isinstance(value, dict):
        raise ValueError(RECORD_ERROR)
    record: dict[str, object] = value
    reason = record.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(REASON_ERROR)
    return reason


def read(raw: str) -> dict[str, str]:
    value: object = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(CATALOG_ERROR)
    records: dict[str, object] = value
    found: dict[str, str] = {}
    for name, record in records.items():
        if not name.strip():
            raise ValueError(GATE_ERROR)
        found[name] = _reason(record)
    return found
