import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from scripts import threshold_pins, type_gate

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

RULE_KEYED = frozenset({".semgrep-guards/floors.json"})


def allowlists() -> list[str]:
    """Return every allowlist that names repository paths, sorted.

    These are the floors and allowlists the threshold pin registers and the Semgrep path allowlists. The type baseline
    has its own test through the type gate's loader, and the Semgrep floors name rules, not paths.
    """
    registered = {*threshold_pins.SHRINK_ONLY, *(path for path, _ in threshold_pins.GRADED_BY)}
    semgrep = {path.relative_to(_ROOT).as_posix() for path in (_ROOT / ".semgrep").glob("*allowlist*.json")}
    return sorted((registered | semgrep) - RULE_KEYED - {type_gate.BASELINE.as_posix()})


def missing(entries: Iterable[str]) -> list[str]:
    """Return each file the entries name, without a unit after :: or a leading slash, that is not in the tree."""
    paths = {entry.partition("::")[0].lstrip("/") for entry in entries}
    return sorted(path for path in paths if not (_ROOT / path).is_file())


def test_every_source_the_type_baseline_records_exists() -> None:
    stale = missing(type_gate.load(_ROOT) or {})
    assert not stale, f"{type_gate.BASELINE} records files that no longer exist; drop their entries: {stale}"


@pytest.mark.parametrize("allowlist", allowlists())
def test_every_path_an_allowlist_names_exists(allowlist: str) -> None:
    stale = missing(json.loads((_ROOT / allowlist).read_text()))
    assert not stale, f"{allowlist} names files that no longer exist; drop their entries: {stale}"
