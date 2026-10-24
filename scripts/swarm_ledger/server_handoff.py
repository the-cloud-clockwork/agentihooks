"""Hand the ledger server's listening socket to a successor process that reports ready before the old one stops."""

import os
import select
import socket
import subprocess

LISTEN_FD = "LEDGER_LISTEN_FD"
READY_FD = "LEDGER_READY_FD"
READY = b"1"
READY_WAIT_S = 60.0


def inherited(environ=os.environ) -> socket.socket | None:
    fd = environ.pop(LISTEN_FD, None)
    return None if fd is None else socket.socket(fileno=int(fd))


def announce(payload: bytes = b"", environ=os.environ) -> None:
    fd = environ.pop(READY_FD, None)
    if fd is not None:
        with os.fdopen(int(fd), "wb") as pipe:
            pipe.write(READY + payload)


def spawn(
    listener: socket.socket, argv: list[str], popen=subprocess.Popen, extra: dict | None = None
) -> tuple[subprocess.Popen, int]:
    read, write = os.pipe()
    fd = listener.fileno()
    env = {**os.environ, **(extra or {}), LISTEN_FD: str(fd), READY_FD: str(write)}
    try:
        child = popen(argv, pass_fds=(fd, write), env=env, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        os.close(read)
        raise
    finally:
        os.close(write)
    return child, read


def ready(child: subprocess.Popen, read: int, timeout: float) -> bytes | None:
    """The payload the successor sent with its ready mark, or None once it died or the wait ran out."""
    with os.fdopen(read, "rb") as pipe:
        if select.select([pipe], [], [], timeout)[0]:
            heard = pipe.read()
            if heard.startswith(READY):
                return heard[len(READY) :]
    child.kill()
    child.wait()
    return None


def hand_off(
    listener: socket.socket,
    argv: list[str],
    popen=subprocess.Popen,
    timeout: float = READY_WAIT_S,
    extra: dict | None = None,
) -> bytes | None:
    child, read = spawn(listener, argv, popen, extra)
    return ready(child, read, timeout)
