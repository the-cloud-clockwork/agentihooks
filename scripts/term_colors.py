import os
import sys
from collections.abc import Mapping
from typing import TextIO

BLUE = "34"
YELLOW = "33"
RED = "31"


def enabled(stream: TextIO, environ: Mapping[str, str]) -> bool:
    return stream.isatty() and "NO_COLOR" not in environ


def paint(text: str, color: str, stream: TextIO | None = None, environ: Mapping[str, str] | None = None) -> str:
    stream = sys.stdout if stream is None else stream
    environ = os.environ if environ is None else environ
    return f"\x1b[{color}m{text}\x1b[0m" if enabled(stream, environ) else text


def blue(text: str, stream: TextIO | None = None, environ: Mapping[str, str] | None = None) -> str:
    return paint(text, BLUE, stream, environ)


def yellow(text: str, stream: TextIO | None = None, environ: Mapping[str, str] | None = None) -> str:
    return paint(text, YELLOW, stream, environ)


def red(text: str, stream: TextIO | None = None, environ: Mapping[str, str] | None = None) -> str:
    return paint(text, RED, stream, environ)
