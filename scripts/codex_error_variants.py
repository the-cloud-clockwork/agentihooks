"""Regenerates the snapshot of Codex app server error variants from the installed codex.

Run `python -m scripts.codex_error_variants` from the repository root after a Codex upgrade.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SNAPSHOT = Path("tests/inbox/codex_error_variants.json")
SCHEMA = "codex_app_server_protocol.v2.schemas.json"


def variants(schema: dict[str, Any]) -> list[str]:
    """The CodexErrorInfo variant names: its string enum values and the keys of its object variants."""
    names: set[str] = set()
    for option in schema["definitions"]["CodexErrorInfo"]["anyOf"]:
        names.update(option.get("enum", ()))
        names.update(option.get("properties", ()))
    return sorted(names)


def generate() -> list[str]:
    """The variant names in the app server schema the installed codex generates."""
    with tempfile.TemporaryDirectory() as out:
        subprocess.run(["codex", "app-server", "generate-json-schema", "--out", out], check=True)
        return variants(json.loads((Path(out) / SCHEMA).read_text()))


def main() -> int:
    """Write the snapshot and return the exit status."""
    SNAPSHOT.write_text(json.dumps(generate(), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
