from typing import Protocol

from scripts.ci_lab import digests
from scripts.ci_lab.units import Unit


def digest(value: object) -> str:
    return str(value)


class Cache(Protocol):
    def identity(self) -> str: ...


def bare_stats(cache: Cache) -> Unit:
    key = cache.identity()
    # ruleid: lab-mutation-unit-key-prefix
    return Unit("stats", key, ("test.py",))


def bare_mutant(members: tuple[str, ...]) -> Unit:
    key = digest(members)
    # ruleid: lab-mutation-unit-key-prefix
    return Unit("mutant", key, members)


def bare_split_mutant(key: str, whole: bool, part: tuple[str, ...]) -> Unit:
    # ruleid: lab-mutation-unit-key-prefix
    return Unit("mutant", key if whole else digest([key, list(part)]), part)


def inline_stats(cache: Cache) -> Unit:
    # ruleid: lab-mutation-unit-key-prefix
    return Unit("stats", cache.identity(), ("test.py",))


def inline_mutant(members: tuple[str, ...]) -> Unit:
    # ruleid: lab-mutation-unit-key-prefix
    return Unit("mutant", digest(members), members)


def prefixed_stats(cache: Cache) -> Unit:
    key = cache.identity()
    # ok: lab-mutation-unit-key-prefix
    return Unit("stats", f"stats-v{digests.SCHEMAS['stats']}-{key}", ("test.py",))


def prefixed_mutant(members: tuple[str, ...]) -> Unit:
    key = digest(members)
    # ok: lab-mutation-unit-key-prefix
    return Unit("mutant", f"mutant-v{digests.SCHEMAS['mutant']}-{key}", members)


def prefixed_split_mutant(key: str, whole: bool, part: tuple[str, ...]) -> Unit:
    # ok: lab-mutation-unit-key-prefix
    return Unit("mutant", f"mutant-v{digests.SCHEMAS['mutant']}-{key if whole else digest([key, list(part)])}", part)
