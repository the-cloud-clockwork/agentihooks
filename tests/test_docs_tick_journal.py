"""Documented tick journal reads select the user unit and pass UTC bounds."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OLD_TAG = re.compile(r"-t\s+agentihooks-swarm\b|SYSLOG_IDENTIFIER=agentihooks-swarm\b")
UNIT = "-u agentihooks-swarm.service"


def documents() -> list[Path]:
    skills = [*ROOT.glob("profiles/**/skills/**/*.md"), *ROOT.glob("profiles/**/.claude/skills/**/*.md")]
    return sorted({*ROOT.glob("docs/**/*.md"), *skills})


def commands(path: Path) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", path.read_text(encoding="utf-8"))
    return [line for line in joined.splitlines() if "journalctl" in line]


def tick_reads() -> list[tuple[Path, str]]:
    return [(path, line) for path in documents() for line in commands(path) if UNIT in line or OLD_TAG.search(line)]


def test_tick_journal_reads_exist_in_the_docs() -> None:
    assert len(tick_reads()) > 10


def test_no_tick_journal_read_selects_the_old_tag() -> None:
    offenders = [f"{path.relative_to(ROOT)}: {line.strip()}" for path, line in tick_reads() if OLD_TAG.search(line)]
    assert offenders == []


def test_every_tick_journal_read_passes_utc_bounds() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}: {line.strip()}"
        for path, line in tick_reads()
        if not ("TZ=UTC journalctl" in line and "--since" in line and "--until" in line)
    ]
    assert offenders == []
