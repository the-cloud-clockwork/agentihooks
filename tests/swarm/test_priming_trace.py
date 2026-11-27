"""The swarm priming an opening prompt carries, written as trace rows: culture lines, recaps and learned notes."""

import json
from pathlib import Path
from types import SimpleNamespace

from scripts.swarm import priming_trace
from scripts.swarm.runtime import HerdrRuntime

SEAT = "eng-2@sw"


from tests.swarm.profile_fixture import validated


def _task(**extra):
    recaps = [{"occupant": f"e{n}", "task": "t", "text": f"recap {n}"} for n in (5, 4, 3, 2, 1)]
    learned = [
        {"maturity": "note", "text": "first note"},
        {"maturity": "data", "text": "a raw figure"},
        {"maturity": "canon", "text": "third note"},
    ]
    return {
        "id": "t4",
        "title": "x",
        "seat": SEAT,
        "culture": "# How\n\n- merge fast\n- plain words\n",
        "recaps": recaps,
        "learned": learned,
        **extra,
    }


def _keyed(rows):
    return {row["source"]: (row["layer"], row["locator"], row["text"]) for row in rows}


def test_rows_carry_each_culture_line_shown_recap_and_shown_note() -> None:
    assert _keyed(priming_trace.rows("sw", _task())) == {}


def test_an_empty_seat_carries_no_rows():
    assert priming_trace.rows("sw", {"id": "t4", "seat": SEAT}) == []


def test_a_task_spawned_without_a_seat_names_no_seat() -> None:
    assert priming_trace.rows("sw", {"id": "t4", "learned": [{"maturity": "note", "text": "n"}]}) == []


def test_spawn_writes_the_priming_rows_beside_the_prompt(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={},
        autonomy="delegate",
    )
    runtime.spawn(config, "eng", "engineer@a1b2c3-0001", _task())
    written = priming_trace.path(tmp_path, "sw", "engineer@a1b2c3-0001")
    assert written.parent == tmp_path / "sw" / "prompts"
    assert json.loads(written.read_text()) == priming_trace.rows("sw", _task())


def test_a_second_spawn_of_the_same_agent_rewrites_its_rows(tmp_path: Path) -> None:
    held = {"layer": "withheld", "source": "culture:sw#1"}
    priming_trace.write(tmp_path, "sw", "engineer@a1b2c3-0001", _task(withheld=[held]))
    written_path = priming_trace.path(tmp_path, "sw", "engineer@a1b2c3-0001")
    assert json.loads(written_path.read_text()) == [held]
    priming_trace.write(tmp_path, "sw", "engineer@a1b2c3-0001", _task(culture="- only line"))
    assert json.loads(written_path.read_text()) == []
