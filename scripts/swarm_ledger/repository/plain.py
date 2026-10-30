import os
import time
from pathlib import Path

from scripts.swarm_ledger import ledger_locks

LEDGER_DIR = Path(os.environ.get("LEDGER_DIR", Path.home() / "development-ledger")).expanduser()
LOCK = ledger_locks.LedgerLock()


def now_ms() -> int:
    return int(time.time() * 1000)
