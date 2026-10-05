import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "doctor"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())
