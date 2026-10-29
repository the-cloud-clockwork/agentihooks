import http.client
import os
import signal
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from scripts.swarm_ledger import ledger_link

WRITE_TIMEOUT_S = 5.0
FAILS_MAX = 3
TALLY = ".health-failures"
PROC = Path("/proc")
REFUSED = "not listening yet"


def probe(base: str, urlopen: Callable = urllib.request.urlopen) -> str:
    request = urllib.request.Request(f"{base}/healthz", data=b"")
    try:
        with urlopen(request, timeout=WRITE_TIMEOUT_S, **ledger_link.transport()) as reply:
            reply.read()
    except urllib.error.URLError as exc:
        return REFUSED if isinstance(exc.reason, ConnectionRefusedError) else str(exc)
    except (OSError, http.client.HTTPException) as exc:
        return str(exc) or type(exc).__name__
    return ""


def tally(folder: Path, failed: bool) -> int:
    path = folder / TALLY
    if not failed:
        path.unlink(missing_ok=True)
        return 0
    try:
        count = int(path.read_text()) + 1
    except (OSError, ValueError):
        count = 1
    path.write_text(str(count % FAILS_MAX))
    return count


def end_server(folder: Path, kill: Callable[[int, int], None] = os.kill, proc: Path = PROC) -> None:
    try:
        pid = int((folder / ".server.pid").read_text())
        argv = (proc / str(pid) / "cmdline").read_bytes()
    except (OSError, ValueError):
        return
    if b"ledger_server.py" in argv:
        try:
            kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main(
    environ: dict = os.environ,
    check: Callable[[str], str] = probe,
    end: Callable[[Path], None] = end_server,
) -> int:
    folder = ledger_link.folder(environ)
    error = check(ledger_link.base(environ))
    if error == REFUSED:
        print(f"probe write failed: {error}", file=sys.stderr)
        return 1
    count = tally(folder, bool(error))
    if not error:
        return 0
    print(f"probe write failed, {count} of {FAILS_MAX}: {error}", file=sys.stderr)
    if count >= FAILS_MAX:
        end(folder)
    return 1


if __name__ == "__main__":
    sys.exit(main())
