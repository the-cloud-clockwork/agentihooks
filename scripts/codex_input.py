import errno
import os
import pty
import select
import signal
import sys
import termios
import time
import tty

# Remove this guard after herdr ships https://github.com/herdrdev/herdr/issues/5251.
ESCAPE_WAIT_S = 1.0
PASTE_END = b"\x1b[201~"
FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class InputFramer:
    def __init__(self) -> None:
        self.pending = bytearray()
        self.paste = False
        self.escape_at = 0.0
        self.escape_key = b"\x1b"

    def feed(self, data: bytes) -> bytes:
        return b"".join(self._consume(value) for value in data)

    def expire_escape(self) -> bytes:
        if not self.paste and self.pending == b"\x1b" and time.monotonic() - self.escape_at >= ESCAPE_WAIT_S:
            self.pending.clear()
            return self.escape_key
        return b""

    def _consume(self, value: int) -> bytes:
        if self.paste:
            return self._paste_byte(value)
        if value == 3:
            self.pending.clear()
            return b"\x03"
        if value == 27:
            previous = self.escape_key if self.pending == b"\x1b" else b""
            self.pending = bytearray(b"\x1b")
            self.escape_key = b"\x1b"
            self.escape_at = time.monotonic()
            return previous
        if not self.pending:
            return bytes([value])
        self.pending.append(value)
        if len(self.pending) == 2 and value not in (91, 79):
            return self.escape_key + self._take()[1:]
        return self._sequence()

    def _sequence(self) -> bytes:
        if self.pending.startswith(b"\x1b[M"):
            if len(self.pending) == 6:
                self.pending.clear()
            return b""
        if len(self.pending) < 3 or not 64 <= self.pending[-1] <= 126:
            return b""
        sequence = self._take()
        if sequence.startswith(b"\x1b[") and sequence[-1:] in (b"M", b"m"):
            return b""
        if sequence in (b"\x1b[27u", b"\x1b[27;1u", b"\x1b[27;1:1u"):
            self.pending = bytearray(b"\x1b")
            self.escape_key = sequence
            self.escape_at = time.monotonic()
            return b""
        self.paste = sequence == b"\x1b[200~"
        return sequence

    def _paste_byte(self, value: int) -> bytes:
        self.pending.append(value)
        output = bytearray()
        for _ in range(len(self.pending)):
            if PASTE_END.startswith(self.pending):
                break
            output.append(self.pending.pop(0))
        if self.pending == PASTE_END:
            output.extend(self._take())
            self.paste = False
        return bytes(output)

    def _take(self) -> bytes:
        data = bytes(self.pending)
        self.pending.clear()
        return data


def write_all(fd: int, data: bytes) -> None:
    offset = 0
    for _ in range(len(data)):
        offset += os.write(fd, data[offset:])
        if offset == len(data):
            break


def pump(master: int) -> None:
    framer = InputFramer()
    readers = [master, 0]
    while master in readers:
        ready, _, _ = select.select(readers, [], [], 0.05)
        write_all(master, framer.expire_escape())
        for fd in ready:
            try:
                data = os.read(fd, 65536)
            except OSError as exc:
                if fd != master or exc.errno != errno.EIO:
                    raise
                data = b""
            if not data:
                readers.remove(fd)
            elif fd == master:
                write_all(1, data)
            else:
                write_all(master, framer.feed(data))


def run(argv: list[str]) -> int:
    if not os.isatty(0):
        os.execvp(argv[0], argv)
    saved = termios.tcgetattr(0)
    size = termios.tcgetwinsize(0)
    pid, master = pty.fork()
    if pid == 0:
        termios.tcsetattr(0, termios.TCSANOW, saved)
        termios.tcsetwinsize(0, size)
        for number in FORWARDED:
            signal.signal(number, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, FORWARDED)
        os.execvp(argv[0], argv)

    def resize(number, frame):
        termios.tcsetwinsize(master, termios.tcgetwinsize(0))

    def forward(number, frame):
        os.killpg(pid, number)

    previous = {number: signal.signal(number, forward) for number in FORWARDED}
    previous[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, resize)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, FORWARDED)
    try:
        tty.setraw(0)
        pump(master)
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, saved)
        for number, handler in previous.items():
            signal.signal(number, handler)
        os.close(master)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return code if code >= 0 else 128 - code


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
