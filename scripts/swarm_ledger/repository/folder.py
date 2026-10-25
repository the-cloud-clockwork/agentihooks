from collections.abc import Mapping
from pathlib import Path

SHARED = "development-ledger"


def ledger_folder(env: Mapping[str, str]) -> Path:
    return Path(env.get("LEDGER_DIR") or Path.home() / SHARED).expanduser()
