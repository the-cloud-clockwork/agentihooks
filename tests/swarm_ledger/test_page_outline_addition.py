"""A contract that names an outline section the head adds keeps it out of the replayed ledger page outline."""

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.swarm_ledger import page_replay

pytestmark = pytest.mark.timeout(180)

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "scripts/swarm_ledger/shell.html"
ANCHOR = '<section id="sec-followups"'
ADDED = '<section id="sec-added" data-outline=""><details id="added-box"><summary>Added</summary></details></section>'
CONTRACT = {"#sec-added": 1}
STEPS = ("load", "outline", "outline all", "jump to", "reload")
SIGNATURE = re.compile(r'<nav [^>]*?id="outline"[^>]*? data-sig="([^"]*)"')
JUMPS = re.compile(r'<select id="jump-to".*?</select>', re.DOTALL)
GROUPS = re.compile(r'id="ol-(sec-[\w-]+)"')


@pytest.fixture(scope="module")
def outline_steps() -> Iterator[None]:
    full = page_replay.steps
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(page_replay, "steps", lambda: [step for step in full() if step[0] in STEPS])
        yield


@pytest.fixture(scope="module")
def base(outline_steps: None, tmp_path_factory: pytest.TempPathFactory) -> str:
    return replay(tmp_path_factory.mktemp("base"), {}, "base")


@pytest.fixture
def planted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shell = SHELL.read_text(encoding="utf-8")
    assert shell.count(ANCHOR) == 1
    page = tmp_path / "shell.html"
    page.write_text(shell.replace(ANCHOR, ADDED + ANCHOR), encoding="utf-8")
    monkeypatch.setattr("ledger_core.SHELL", page)


def replay(folder: Path, hidden: dict[str, int], side: str) -> str:
    records = page_replay.unclocked(page_replay.record(ROOT, folder, hidden, {}, side))
    return json.dumps(records, indent=1, ensure_ascii=False)


def traces(record: dict[str, Any]) -> list[str]:
    html = record["page"]["html"]
    signature, jumps = SIGNATURE.search(html), JUMPS.search(html)
    assert signature and jumps
    return [signature.group(1), jumps.group(0), " ".join(GROUPS.findall(html))]


@pytest.mark.feature("page-equivalence-added-outline-section")
def test_a_contract_naming_an_added_outline_section_makes_head_and_base_records_identical(
    base: str, planted: None, tmp_path: Path
) -> None:
    assert replay(tmp_path / "head", CONTRACT, "head") == base


@pytest.mark.feature("page-equivalence-added-outline-section")
def test_the_head_outline_signature_and_jump_list_leave_out_the_hidden_section(
    base: str, planted: None, tmp_path: Path
) -> None:
    head = json.loads(replay(tmp_path / "head", CONTRACT, "head"))
    assert [step["step"] for step in head] == list(STEPS)
    for step in head:
        for trace in traces(step):
            assert "sec-followups" in trace
            assert "sec-added" not in trace


@pytest.mark.feature("page-equivalence-added-outline-section")
def test_the_planted_outline_section_without_the_contract_still_differs_from_the_base(
    base: str, planted: None, tmp_path: Path
) -> None:
    head = replay(tmp_path / "head", {}, "head")
    assert head != base
    assert all("sec-added" in trace for step in json.loads(head) for trace in traces(step))
