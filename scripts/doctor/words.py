"""Wording shared by the doctor detectors' findings."""


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def gist(text, limit=200):
    first = text.splitlines()[0] if text else ""
    return first[:limit]
