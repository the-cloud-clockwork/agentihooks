import ctypes

TRIM = getattr(ctypes.CDLL(None), "malloc_trim", None)


def release() -> bool:
    if TRIM is None:
        return False
    return bool(TRIM(0))
