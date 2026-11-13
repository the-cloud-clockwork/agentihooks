import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OLD_TAG = re.compile(r"-t\s+agentihooks-swarm\b|SYSLOG_IDENTIFIER=agentihooks-swarm\b")
ANY_UNIT = re.compile(r"-u\s+'?agentihooks-swarm(?![-\w])")
UNIT = "-u agentihooks-swarm.service"


def documents() -> list[Path]:
    skipped = {".git", ".venv", "node_modules"}
    return sorted(path for path in ROOT.rglob("*.md") if not skipped & set(path.relative_to(ROOT).parts))


def commands(path: Path) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", path.read_text(encoding="utf-8"))
    return [line for line in joined.splitlines() if "journalctl" in line]


def tick_reads() -> list[tuple[Path, str]]:
    return [
        (path, line) for path in documents() for line in commands(path) if ANY_UNIT.search(line) or OLD_TAG.search(line)
    ]


@pytest.mark.feature("docs-tick-journal-user-unit-utc")
def test_tick_journal_reads_exist_in_the_docs() -> None:
    reads = tick_reads()
    assert len(reads) > 10, f"{len(reads)} tick journal reads found in the repository markdown"


@pytest.mark.feature("docs-tick-journal-user-unit-utc")
def test_no_tick_journal_read_selects_the_old_tag() -> None:
    offenders = [f"{path.relative_to(ROOT)}: {line.strip()}" for path, line in tick_reads() if OLD_TAG.search(line)]
    assert offenders == []


@pytest.mark.feature("docs-tick-journal-user-unit-utc")
def test_every_tick_journal_read_selects_the_user_unit() -> None:
    offenders = [f"{path.relative_to(ROOT)}: {line.strip()}" for path, line in tick_reads() if UNIT not in line]
    assert offenders == []


@pytest.mark.feature("docs-tick-journal-user-unit-utc")
def test_every_tick_journal_read_passes_utc_bounds() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}: {line.strip()}"
        for path, line in tick_reads()
        if not ("TZ=UTC journalctl" in line and "--since" in line and "--until" in line)
    ]
    assert offenders == []
