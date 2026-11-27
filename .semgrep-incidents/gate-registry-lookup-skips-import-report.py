from collections import ChainMap
from collections.abc import Mapping

from scripts.gates.base import Gate
from scripts.gates.entry import GATES, HOST_GATES


def resolved_before_the_report(argv: list[str]) -> list[Gate | None]:
    named = ChainMap(HOST_GATES, GATES)
    # ruleid: gate-registry-lookup-skips-import-report
    return [named.get(name) for name in argv]


def native_before_the_report(name: str) -> Gate:
    # ruleid: gate-registry-lookup-skips-import-report
    return HOST_GATES[name]


def first_gate_before_the_report(name: str) -> Gate | None:
    # ruleid: gate-registry-lookup-skips-import-report
    return GATES.get(name)


def _resolve(named: Mapping[str, Gate], name: str) -> Gate | None:
    try:
        # ok: gate-registry-lookup-skips-import-report
        return named[name]
    except ImportError:
        return None


def resolved_through_the_report(argv: list[str]) -> list[Gate | None]:
    named = ChainMap(HOST_GATES, GATES)
    # ok: gate-registry-lookup-skips-import-report
    known = all(name in named for name in argv)
    # ok: gate-registry-lookup-skips-import-report
    return [_resolve(named, name) for name in argv] if known else []
