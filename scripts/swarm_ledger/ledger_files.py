import os
import tempfile

LOG_MAX_BYTES = 5 << 20


def rotate_if_full(path, limit=LOG_MAX_BYTES):
    try:
        if path.stat().st_size >= limit:
            path.replace(path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


def append_capped(path, text, limit=LOG_MAX_BYTES):
    rotate_if_full(path, limit)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    written = os.stat(tmp)
    os.replace(tmp, path)
    return written
