"""Keep the operator's real ledger folder and the shared ledger port out of every test.

Imported by the root conftest before any test module, so a module that binds LEDGER_DIR or LEDGER_PORT at import
binds this suite's own folder and a spare port. A folder conftest fixture is not enough: pytest drops it for a file
given after a file from another folder.
"""

import atexit
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

REAL = Path.home() / "development-ledger"
ROOTS = tuple({os.path.abspath(REAL), os.path.realpath(REAL)})
EVENTS = frozenset(("open", "os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.listdir", "os.scandir"))
touched: list[str] = []


class RealLedgerFolder(RuntimeError):
    pass


def reserve_port() -> socket.socket:
    # Bound but never listening: bind 0 skips the port in every process, while a SO_REUSEADDR server still binds it.
    hold = socket.socket()
    hold.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    hold.bind(("127.0.0.1", 0))
    return hold


def _under(path) -> bool:
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    absolute = os.path.abspath(os.fsdecode(path))
    return any(absolute == root or absolute.startswith(root + os.sep) for root in ROOTS)


def _audit(event, args):
    if event in EVENTS:
        hit = next((os.fsdecode(path) for path in args[:2] if _under(path)), None)
        if hit:
            touched.append(f"{event} {hit}")
            raise RealLedgerFolder(f"a test reached the real ledger folder: {event} {hit}")


SUITE = Path(tempfile.mkdtemp(prefix="agentihooks-test-ledgers-"))
os.environ["LEDGER_DIR"] = str(SUITE)
os.environ["LEDGER_RUN_PID"] = str(os.getpid())
os.environ.pop("LEDGER_RUN_START", None)
os.environ["LEDGER_AUTOSTART"] = "0"
HELD_PORT = reserve_port()
os.environ["LEDGER_PORT"] = str(HELD_PORT.getsockname()[1])
atexit.register(lambda pid=os.getpid(): os.getpid() == pid and shutil.rmtree(SUITE, ignore_errors=True))
sys.addaudithook(_audit)
