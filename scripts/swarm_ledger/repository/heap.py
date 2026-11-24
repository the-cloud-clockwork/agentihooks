import ctypes
import sys

TRIM = None if sys.platform == "win32" else getattr(ctypes.CDLL(None), "malloc_trim", None)


def release() -> None:
    if TRIM is not None:
        TRIM(0)
