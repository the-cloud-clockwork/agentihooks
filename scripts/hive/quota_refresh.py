MARGIN_SECONDS = 5 * 60


def due(environ, now: float) -> bool:
    return False


def refresh(environ, now: float | None = None) -> bool:
    return False


def start(environ, worker, now: float | None = None):
    return worker
