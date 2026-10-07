import copy
import hashlib
import json
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
NEWER_FIELD = "handoff_hint"


def _previous_minor(doc: dict) -> dict:
    doc["schema_version"] = "2.0"
    doc.pop("trace_id", None)
    return doc


def _future_major(doc: dict) -> dict:
    doc["schema_version"] = "3.0"
    return doc


def _missing_authority(doc: dict) -> dict:
    del doc["authority"]["task_generation"]
    return doc


def _previous_minor_missing_authority(doc: dict) -> dict:
    return _missing_authority(_previous_minor(doc))


def _newer_minor(doc: dict) -> dict:
    doc["schema_version"] = "2.2"
    doc[NEWER_FIELD] = "synthetic optional field from a newer producer"
    return doc


FAMILIES = {
    "previous-minor": (_previous_minor, "valid", False),
    "future-major": (_future_major, "invalid", False),
    "missing-authority": (_missing_authority, "invalid", True),
    "previous-minor-missing-authority": (_previous_minor_missing_authority, "invalid", True),
    "newer-minor": (_newer_minor, "valid", False),
}


def _text(doc: dict) -> str:
    return json.dumps(doc, indent=2) + "\n"


def build(root: Path = FIXTURES) -> dict[str, str]:
    files = {}
    index = []
    for current in sorted(root.glob("*/current.json")):
        contract = current.parent.name
        base = json.loads(current.read_text())
        entries = [("current", "valid", current.read_text())]
        for family, (derive, expect, needs_authority) in FAMILIES.items():
            if needs_authority and "authority" not in base:
                continue
            entries.append((family, expect, _text(derive(copy.deepcopy(base)))))
        for family, expect, text in entries:
            rel = f"{contract}/{family}.json"
            files[rel] = text
            digest = hashlib.sha256(text.encode()).hexdigest()
            index.append({"contract": contract, "family": family, "file": rel, "expect": expect, "sha256": digest})
    files["index.json"] = _text({"fixtures": index})
    return files


def main(argv: list[str]) -> int:
    root = Path(argv[0]) if argv else FIXTURES
    for rel, text in build(root).items():
        (root / rel).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
