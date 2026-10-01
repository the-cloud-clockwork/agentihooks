import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path


def lock_file(home: Path, path: str) -> Path:
    folder = home / "gc-locks"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (hashlib.sha1(path.encode()).hexdigest()[:20] + ".lock")


@contextmanager
def removing(home: Path, path: str):
    with open(lock_file(home, path), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def being_removed(home: Path, path: str) -> bool:
    file = lock_file(home, path)
    if not file.exists():
        return False
    with open(file, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False
