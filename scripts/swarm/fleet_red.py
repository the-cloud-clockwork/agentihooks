"""Hold the claim of a task whose pull request is red only from checks failing on most other open pull requests."""

import subprocess
from collections.abc import Callable


class ReadError(Exception):
    pass


def read(repo: str, run: Callable = subprocess.run) -> list[dict]:
    return []


def shared(pulls: list[dict], url: str) -> set[str]:
    return set()


def held(pulls: list[dict], url: str) -> bool:
    return False


def checker(repo: str, reader: Callable | None = None) -> Callable[[dict], bool]:
    return lambda task: True
