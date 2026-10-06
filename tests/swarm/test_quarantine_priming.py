"""Correction quarantine in swarm priming: corrected culture lines and learned notes drop out, local patches refused."""

import pytest

from hooks.context import injection_trace
from scripts.swarm import priming_trace, prompt
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

SID = "sess-prime-1"
BAD = "Push straight to dev because review slows the swarm down"
GOOD = "Run the full suite before a push because CI shards hide leaks"
SEAT = "eng-2@sw"


@pytest.fixture(autouse=True)
def plain_env(monkeypatch):
    for name in (
        "AGENTIHOOKS_SWARM",
        "AGENTIHOOKS_AGENT_NAME",
        "AGENTIHOOKS_SWARM_TASK",
        "AGENTIHOOKS_GATE_QUARANTINE",
    ):
        monkeypatch.delenv(name, raising=False)


def _correct(source, text, locator):
    layer = source.split(":", 1)[0]
    injection_trace.record(SID, layer, source, text, locator)
    return injection_trace.correct(SID, source, "/repos/agentihooks", "dev is protected")


def _task():
    learned = [{"maturity": "note", "text": BAD}, {"maturity": "insight", "text": GOOD}]
    return {"id": "t1", "seat": SEAT, "culture": f"# How\n- {BAD}\n- plain words\n", "learned": learned}


def test_a_corrected_learned_note_and_culture_line_drop_out_of_priming():
    _correct(f"learned:{SEAT}#1", BAD, {"seat": SEAT, "note": 1})

    task = priming_trace.withhold("sw", _task())

    lines = "\n".join(prompt.priming_lines(task))
    assert BAD not in lines
    assert f"- insight: {GOOD}" in lines
    assert "- plain words" in lines
    rows = {row["source"]: row for row in priming_trace.rows("sw", task)}
    assert f"learned:{SEAT}#1" not in {s for s, r in rows.items() if r["layer"] == "learned"}
    assert rows[f"learned:{SEAT}#2"]["layer"] == "learned"
    assert "culture:sw#2" not in {s for s, r in rows.items() if r["layer"] == "culture"}
    assert rows["culture:sw#3"]["text"] == "- plain words"
    withheld = sorted((r["source"], r["locator"]["layer"]) for r in rows.values() if r["layer"] == "withheld")
    assert withheld == [("culture:sw#2", "culture"), (f"learned:{SEAT}#1", "learned")]


def test_a_withheld_note_is_not_counted_as_data():
    task = {"learned": [{"maturity": "data", "text": "x", "withheld": True}, {"maturity": "data", "text": "y"}]}
    assert prompt.learned_lines(task["learned"]) == [
        "4. Learned notes: only data so far.",
        "1 data entries are kept on the seat and not shown.",
    ]


def test_observe_mode_keeps_the_note_and_logs_it(monkeypatch):
    _correct(f"learned:{SEAT}#1", BAD, {"seat": SEAT, "note": 1})
    monkeypatch.setenv("AGENTIHOOKS_GATE_QUARANTINE", "observe")

    task = priming_trace.withhold("sw", _task())

    assert BAD in "\n".join(prompt.priming_lines(task))
    modes = {r["locator"]["mode"] for r in priming_trace.rows("sw", task) if r["layer"] == "withheld"}
    assert modes == {"observe"}


def test_off_mode_and_no_corrections_leave_the_task_untouched(monkeypatch):
    assert priming_trace.withhold("sw", _task()) == _task()
    _correct(f"learned:{SEAT}#1", BAD, {"seat": SEAT, "note": 1})
    monkeypatch.setenv("AGENTIHOOKS_GATE_QUARANTINE", "off")
    assert priming_trace.withhold("sw", _task()) == _task()


def test_a_task_without_culture_or_notes_survives():
    _correct(f"learned:{SEAT}#1", BAD, {"seat": SEAT, "note": 1})
    task = priming_trace.withhold("sw", {"id": "t1", "seat": SEAT, "culture": None})
    assert (task["culture"], task["learned"], task["withheld"]) == ("", [], [])


def test_the_tick_primes_spawns_without_the_corrected_note(env):  # noqa: F811
    from scripts.swarm.tick import primed

    store, _, _ = env
    store.memory.learn(SEAT, "e1", BAD, 1)
    store.memory.learn(SEAT, "e1", GOOD, 2)
    _correct(f"learned:{SEAT}#1", BAD, {"seat": SEAT, "note": 1})

    task = primed(store, "sw", SEAT, {"id": "t1"})

    assert [note.get("withheld", False) for note in task["learned"]] == [True, False]


def test_learned_refuses_a_note_restating_an_open_correction(env, capsys):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    _correct(f"learned:{SEAT}#1", BAD, {"seat": SEAT, "note": 1})

    assert run("sw", "--as", "master@a1b2c3-0001", "learned", BAD) == 1

    assert "fix it at its source" in capsys.readouterr().err
    assert swarm.memory.learned("master@sw") == []
    assert run("sw", "--as", "master@a1b2c3-0001", "learned", GOOD) == 0


def test_culture_set_refuses_text_restating_an_open_correction(env, tmp_path, capsys):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    _correct("culture:sw#2", BAD, {"swarm": "sw", "line": 2})
    bad, good = tmp_path / "bad.md", tmp_path / "good.md"
    bad.write_text(f"# How\n- {BAD}\n")
    good.write_text("# How\n- plain words\n")

    assert run("sw", "culture", "set", str(bad)) == 1
    assert "fix it at its source" in capsys.readouterr().err
    assert run("sw", "culture", "set", str(good)) == 0
    assert swarm.culture.get("sw") == "# How\n- plain words\n"
