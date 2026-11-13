from collections.abc import Mapping
from typing import Any


def read(environ: Mapping[str, str], now_s: float) -> list[Any] | None:
    from scripts.hive import otel_presence

    return otel_presence.read(environ, now_s)
