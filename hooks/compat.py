"""POSIX-only calls agentihooks makes, available on native Windows too: fcntl.flock and the real home folder."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

__all__ = ["fcntl", "pwd", "real_home"]

if sys.platform == "win32":
    import msvcrt
    from typing import IO

    class _MsvcrtFcntl:
        """The slice of fcntl agentihooks uses (flock and its LOCK_* flags), on msvcrt byte-range locks."""

        LOCK_SH = 1
        LOCK_EX = 2
        LOCK_NB = 4
        LOCK_UN = 8

        @staticmethod
        def flock(fd: int | IO[str] | IO[bytes], operation: int) -> None:
            """Lock or unlock the first byte of the file; Windows has no shared lock, so LOCK_SH is exclusive."""
            number = fd if isinstance(fd, int) else fd.fileno()
            position = os.lseek(number, 0, os.SEEK_CUR)
            os.lseek(number, 0, os.SEEK_SET)
            try:
                if operation & _MsvcrtFcntl.LOCK_UN:
                    msvcrt.locking(number, msvcrt.LK_UNLCK, 1)
                    return
                while True:
                    try:
                        msvcrt.locking(number, msvcrt.LK_NBLCK, 1)
                        return
                    except OSError:
                        if operation & _MsvcrtFcntl.LOCK_NB:
                            raise BlockingIOError("file is locked by another process") from None
                        time.sleep(0.05)
            finally:
                os.lseek(number, position, os.SEEK_SET)

    fcntl = _MsvcrtFcntl()
    pwd = None
else:
    import fcntl
    import pwd


def real_home() -> Path:
    """The account's home folder, ignoring a HOME override: the password database on POSIX, USERPROFILE on Windows."""
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE") or Path.home())
    return Path(pwd.getpwuid(os.getuid()).pw_dir)
