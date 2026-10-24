import ctypes

TRIM = getattr(ctypes.CDLL(None), "malloc_trim", None)


def release() -> None:
    if TRIM is not None:
        TRIM(0)
