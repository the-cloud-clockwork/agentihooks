"""The swarm priming an opening prompt carries, written as trace rows: culture lines, recaps and learned notes."""

import json
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


def test_rows_carry_each_culture_line_shown_recap_and_shown_note():
    assert _keyed(priming_trace.rows("sw", _task())) == {
        "culture:sw#1": ("culture", {"swarm": "sw", "line": 1}, "# How"),
        "culture:sw#3": ("culture", {"swarm": "sw", "line": 3}, "- merge fast"),
        "culture:sw#4": ("culture", {"swarm": "sw", "line": 4}, "- plain words"),
        f"recap:{SEAT}#5": ("recap", {"seat": SEAT, "recap": 5}, "recap 5"),
        f"recap:{SEAT}#4": ("recap", {"seat": SEAT, "recap": 4}, "recap 4"),
        f"recap:{SEAT}#3": ("recap", {"seat": SEAT, "recap": 3}, "recap 3"),
        f"recap:{SEAT}#2": ("recap", {"seat": SEAT, "recap": 2}, "recap 2"),
        f"learned:{SEAT}#1": ("learned", {"seat": SEAT, "note": 1}, "first note"),
        f"learned:{SEAT}#3": ("learned", {"seat": SEAT, "note": 3}, "third note"),
    }


def test_an_empty_seat_carries_no_rows():
    assert priming_trace.rows("sw", {"id": "t4", "seat": SEAT}) == []


def test_a_task_spawned_without_a_seat_names_no_seat():
    [row] = priming_trace.rows("sw", {"id": "t4", "learned": [{"maturity": "note", "text": "n"}]})
    assert (row["source"], row["locator"]) == ("learned:#1", {"seat": "", "note": 1})


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


def test_a_second_spawn_of_the_same_agent_rewrites_its_rows(tmp_path):
    priming_trace.write(tmp_path, "sw", "engineer@a1b2c3-0001", _task())
    priming_trace.write(tmp_path, "sw", "engineer@a1b2c3-0001", _task(culture="- only line"))
    written = json.loads(priming_trace.path(tmp_path, "sw", "engineer@a1b2c3-0001").read_text())
    assert [row["source"] for row in written if row["layer"] == "culture"] == ["culture:sw#1"]
