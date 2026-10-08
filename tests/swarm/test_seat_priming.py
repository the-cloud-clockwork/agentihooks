import json
from dataclasses import replace

import pytest

from scripts.swarm import cli, prompt
from scripts.swarm.store import MASTER
from scripts.swarm.tick import tick
from tests.swarm.test_cli import _handoff_doc, env, run  # noqa: F401
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks, workers  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

RECAPS = [
    {"occupant": "engineer@a1b2c3-0004", "task": "t1", "text": "latest recap text", "at": 20},
    {"occupant": "engineer@a1b2c3-0001", "task": "t1", "text": "older recap text", "at": 10},
]
LEARNED = [{"occupant": "engineer@a1b2c3-0001", "text": "learned note text", "at": 5, "maturity": "note"}]


def _eng(**task):
    return prompt.build("sw", "/repo", "eng", "engineer@a1b2c3-0005", {"id": "t1", "title": "x", "phase": "p1", **task})


def test_the_successor_prompt_carries_the_chain_in_order(monkeypatch):
    monkeypatch.delenv("LEDGER_DIR")
    text = _eng(seat="eng-1@sw", handoff="handoff doc text", recaps=RECAPS, learned=LEARNED)
    order = [text.index(part) for part in ("handoff doc text", "latest recap text", "learned note text")]
    assert order == sorted(order) and order[-1] < text.index("older recap text")
    assert "eng-1@sw" in text
    assert text.index("older recap text") < text.index("agentihooks ledger --slug sw show")


def test_a_missing_recap_is_named_in_the_prompt():
    text = _eng(seat="eng-1@sw", handoff="handoff doc text", learned=LEARNED)
    assert "Latest recap: missing" in text
    assert "handoff doc text" in text and "learned note text" in text


def test_missing_handoff_and_learned_notes_are_named():
    text = _eng(seat="eng-1@sw", recaps=RECAPS[:1])
    assert "Handoff document: none" in text
    assert "Learned notes: none" in text
    assert "Older recaps: none" in text


def test_a_seat_with_no_history_gets_a_prompt_that_says_so():
    text = _eng(seat="eng-1@sw")
    assert "Your seat eng-1@sw has no history yet" in text
    assert "Latest recap" not in text


def test_older_recaps_beyond_the_cap_are_counted_not_dropped_silently():
    many = [{"occupant": f"sw-eng-{n}", "task": "t1", "text": f"recap {n}", "at": n} for n in range(9, 0, -1)]
    text = _eng(seat="eng-1@sw", recaps=many)
    shown = prompt.OLDER_RECAPS
    assert f"recap {9 - shown}" in text and f"recap {8 - shown}" not in text
    assert f"{len(many) - 1 - shown} older recaps are kept on the seat" in text


def test_the_master_prompt_carries_the_chain_too():
    task = {"id": MASTER, "seat": "master@sw", "handoff": "caps go to four", "recaps": RECAPS, "learned": LEARNED}
    text = prompt.build("sw", "/repo", MASTER, "master@a1b2c3-0002", task)
    assert text.index("caps go to four") < text.index("latest recap text") < text.index("learned note text")


def test_a_handoff_writes_a_recap_under_the_seat(env, tmp_path):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc, recap = _handoff_doc(tmp_path), tmp_path / "recap.md"
    recap.write_text("did seam 1, stopped at seam 2, promised the master a pr")
    assert run("sw", "--as", "engineer@a1b2c3-0001", "handoff", str(doc), "--recap", str(recap)) == 0
    (entry,) = swarm.memory.recaps("eng-1@sw")
    assert (entry["occupant"], entry["task"]) == ("engineer@a1b2c3-0001", "t1")
    assert "## Done\n- Seam one green" in entry["text"]
    assert "## Stopped at\nSeam two test red" in entry["text"]
    assert "## Next\n1. Make seam two green" in entry["text"]
    assert "promised the master a pr" not in entry["text"]


def test_a_malformed_handoff_writes_no_recap(env, tmp_path, capsys):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc = tmp_path / "handoff.md"
    doc.write_text("doc")
    assert run("sw", "--as", "engineer@a1b2c3-0001", "handoff", str(doc)) == 1
    assert "handoff refused" in capsys.readouterr().err
    assert swarm.handoff("sw", "t1") == ""
    assert swarm.memory.recaps("eng-1@sw") == []


def test_learned_appends_a_note_to_the_callers_seat(env):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert (
        run(
            "sw",
            "--as",
            "engineer@a1b2c3-0001",
            "learned",
            "the ledger refuses dashes in chat because chat must use plain words",
        )
        == 0
    )
    assert [n["text"] for n in swarm.memory.learned("eng-1@sw")] == [
        "the ledger refuses dashes in chat because chat must use plain words"
    ]
    assert [n["maturity"] for n in swarm.memory.learned("eng-1@sw")] == ["note"]


def test_the_tick_primes_a_successor_from_its_seat(store):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    (first,) = workers(store)
    store.memory.add_recap(first.seat, first.name, "t1", "stopped at seam 2", at=1_500)
    store.memory.learn(first.seat, first.name, "run the whole suite", at=1_500)
    store.put_handoff("sw", "t1", "seam 2 is red", seat=first.seat)
    store.put_agent("sw", replace(first, state="finished"))
    tick("sw", store, ledger, runtime, now_ms=2_000)
    primed = runtime.tasks[-1]
    assert primed["seat"] == first.seat and primed["handoff"] == "seam 2 is red"
    assert [r["text"] for r in primed["recaps"]] == ["stopped at seam 2"]
    assert [n["text"] for n in primed["learned"]] == ["run the whole suite"]


def test_the_tick_primes_the_next_master_from_the_master_seat(store):  # noqa: F811
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    (old,) = masters(store)
    store.memory.add_recap(old.seat, old.name, MASTER, "operator wants caps at four", at=1)
    store.put_handoff("sw", MASTER, "doc", seat=old.seat)
    store.put_agent("sw", replace(old, state="finished"))
    tick("sw", store, tasks(), runtime, 2)
    primed = runtime.masters[-1][1]
    assert primed["seat"] == "master@sw"
    assert [r["text"] for r in primed["recaps"]] == ["operator wants caps at four"]


def test_a_first_occupant_is_primed_with_an_empty_seat(store):  # noqa: F811
    runtime = FakeRuntime()
    tick("sw", store, tasks(("t1", "eng")), runtime, now_ms=1_000)
    primed = runtime.tasks[-1]
    assert (primed["seat"], primed["recaps"], primed["learned"]) == ("eng-1@sw", [], [])


def _note(text, maturity):
    return {"occupant": "engineer@a1b2c3-0001", "text": text, "at": 1, "maturity": maturity}


def test_learned_notes_list_canon_first_then_insights_and_notes_and_count_data():
    first, second = "first plain note entry", "second plain note entry"
    canon, insight = "canon note entry", "insight note entry"
    hidden = ("first hidden data entry", "second hidden data entry")
    ranked = [_note(first, "note"), _note(hidden[0], "data"), _note(canon, "canon"), _note(insight, "insight")]
    text = _eng(seat="eng-1@sw", learned=[*ranked, _note(hidden[1], "data"), _note(second, "note")])
    order = [
        text.index(f"- {m}: {t}")
        for m, t in (("canon", canon), ("insight", insight), ("note", first), ("note", second))
    ]
    assert order == sorted(order)
    assert hidden[0] not in text and hidden[1] not in text
    assert "2 data entries are kept on the seat and not shown." in text


def test_a_seat_with_only_data_says_so_and_counts_it():
    text = _eng(seat="eng-1@sw", learned=[_note("only hidden data entry", "data")])
    assert "only hidden data entry" not in text and "1 data entries are kept on the seat" in text


@pytest.mark.parametrize("lane", ["eng", "ci", MASTER])
def test_the_culture_sits_ahead_of_the_seat_recap_for_every_lane_and_the_master(lane):
    task = {"id": "t1", "title": "x", "seat": f"{lane}-1@sw", "culture": "culture text", "recaps": RECAPS}
    text = prompt.build("sw", "/repo", lane, f"sw-{lane}-1", task)
    assert text.index("culture text") < text.index("latest recap text")


def test_a_culture_alone_still_reaches_a_fresh_seat():
    text = _eng(seat="eng-1@sw", culture="culture text")
    assert "culture text" in text and "has no history yet" not in text


def test_a_swarm_without_culture_names_it_missing():
    assert "Swarm culture: none" in _eng(seat="eng-1@sw", recaps=RECAPS)


def test_the_tick_primes_every_lane_and_the_master_with_the_swarm_culture(store):  # noqa: F811
    store.culture.set("sw", "say it plainly")
    runtime = FakeRuntime()
    tick("sw", store, tasks(("t1", "eng"), ("t2", "ci")), runtime, now_ms=1_000)
    assert [t["culture"] for t in runtime.tasks] == ["say it plainly", "say it plainly"]
    assert runtime.masters[-1][1]["culture"] == "say it plainly"


def test_learned_takes_a_maturity_and_only_the_master_writes_canon(env):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert (
        run(
            "sw",
            "--as",
            "engineer@a1b2c3-0001",
            "learned",
            "a raw figure because it was measured",
            "--maturity",
            "data",
        )
        == 0
    )
    assert (
        run("sw", "--as", "engineer@a1b2c3-0001", "learned", "law because it always held", "--maturity", "canon") == 1
    )
    assert run("sw", "--as", "master@a1b2c3-0001", "learned", "law because it always held", "--maturity", "canon") == 0
    assert [n["maturity"] for n in swarm.memory.learned("eng-1@sw")] == ["data"]
    assert [n["maturity"] for n in swarm.memory.learned("master@sw")] == ["canon"]


def test_promote_rules_by_caller(env, capsys):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    run("sw", "--as", "engineer@a1b2c3-0001", "learned", "lesson one because it held")
    assert run("sw", "--as", "ci@a1b2c3-0001", "promote", "eng-1", "1", "insight", "--reason", "held twice") == 0
    assert run("sw", "--as", "engineer@a1b2c3-0001", "promote", "eng-1", "1", "canon", "--reason", "always") == 1
    assert "only the master or the operator" in capsys.readouterr().err
    assert run("sw", "--as", "engineer@a1b2c3-0001", "promote", "eng-1", "1", "note", "--reason", "back down") == 1
    assert run("sw", "--as", "engineer@a1b2c3-0001", "promote", "eng-1@other", "1", "canon", "--reason", "x") == 1
    assert run("sw", "--as", "master@a1b2c3-0001", "promote", "eng-1@sw", "1", "canon", "--reason", "always held") == 0
    (entry,) = swarm.memory.learned("eng-1@sw")
    assert entry["maturity"] == "canon"
    assert [(p["to"], p["by"]) for p in entry["promotions"]] == [
        ("insight", "ci@a1b2c3-0001"),
        ("canon", "master@a1b2c3-0001"),
    ]


def test_the_operator_may_promote_to_canon(env, monkeypatch):  # noqa: F811
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    run("sw", "--as", "engineer@a1b2c3-0001", "learned", "lesson one because it held")
    assert run("sw", "promote", "eng-1", "1", "canon", "--reason", "the operator says so") == 0
    assert swarm.memory.learned("eng-1@sw")[0]["promotions"][0]["by"] == "operator"


def test_learned_without_text_lists_entries_with_seat_and_number(env, capsys):  # noqa: F811
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    run("sw", "--as", "engineer@a1b2c3-0001", "learned", "lesson one because it held")
    run("sw", "--as", "engineer@a1b2c3-0001", "learned", "lesson two because it held twice", "--maturity", "insight")
    capsys.readouterr()
    assert run("sw", "learned") == 0
    assert capsys.readouterr().out.splitlines() == [
        "eng-1@sw\t1\tnote\tlesson one because it held",
        "eng-1@sw\t2\tinsight\tlesson two because it held twice",
    ]


def test_only_the_master_or_operator_retires_a_note_and_the_listing_drops_it(env, capsys, monkeypatch):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    run("sw", "--as", "engineer@a1b2c3-0001", "learned", "lesson one because it held")
    run("sw", "--as", "engineer@a1b2c3-0001", "learned", "lesson two because it held twice")
    assert run("sw", "--as", "engineer@a1b2c3-0001", "retire", "eng-1", "1", "--reason", "stale") == 1
    assert cli.ONLY_MASTER_RETIRE in capsys.readouterr().err
    assert run("sw", "--as", "master@a1b2c3-0001", "retire", "eng-1@other", "1", "--reason", "x") == 1
    assert run("sw", "--as", "master@a1b2c3-0001", "retire", "eng-1", "1", "--reason", "stale") == 0
    assert swarm.memory.learned("eng-1@sw")[0]["retired"]["by"] == "master@a1b2c3-0001"
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    assert run("sw", "retire", "eng-1@sw", "2", "--reason", "the operator says so") == 0
    assert swarm.memory.learned("eng-1@sw")[1]["retired"]["by"] == "operator"
    run("sw", "--as", "engineer@a1b2c3-0001", "learned", "lesson three because it held")
    capsys.readouterr()
    assert run("sw", "learned") == 0
    assert capsys.readouterr().out.splitlines() == ["eng-1@sw\t3\tnote\tlesson three because it held"]


def test_retire_reports_what_it_did_and_names_each_refusal(env, capsys, monkeypatch):  # noqa: F811
    swarm, _, _ = env
    monkeypatch.setattr(cli, "now_ms", lambda: 7_000)
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    run("sw", "--as", "engineer@a1b2c3-0001", "learned", "lesson one because it held")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    capsys.readouterr()
    assert run("sw", "retire", "eng-1", "1", "--reason", "stale") == 1
    assert cli.ONLY_MASTER_RETIRE in capsys.readouterr().err
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@a1b2c3-0001")
    assert run("sw", "retire", "eng-1@other", "1", "--reason", "x") == 1
    assert "eng-1@other is not a seat of swarm sw" in capsys.readouterr().err
    assert run("sw", "retire", "eng-1", "9", "--reason", "x") == 1
    assert "seat eng-1@sw has no learned note 9" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run("sw", "retire", "eng-1", "1")
    capsys.readouterr()
    assert run("sw", "retire", "eng-1", "1", "--reason", "stale") == 0
    assert json.loads(capsys.readouterr().out) == {"seat": "eng-1@sw", "number": 1, "retired": True}
    assert swarm.memory.learned("eng-1@sw")[0]["retired"] == {
        "by": "master@a1b2c3-0001",
        "reason": "stale",
        "at": 7_000,
    }


def test_a_retired_note_no_longer_reaches_the_next_master(store):  # noqa: F811
    runtime = FakeRuntime()
    tick("sw", store, tasks(), runtime, 1)
    (old,) = masters(store)
    store.memory.learn(old.seat, old.name, 'say "enable voice" because the operator asked once', at=1)
    store.memory.learn(old.seat, old.name, "keep caps at four because the operator said so", at=2)
    store.memory.retire(old.seat, 1, "operator", "it flips voice on every new master", at=3)
    store.put_agent("sw", replace(old, state="finished"))
    tick("sw", store, tasks(), runtime, 2)
    name, primed = runtime.masters[-1]
    text = prompt.build("sw", "/repo", MASTER, name, {**primed, "id": MASTER})
    assert "keep caps at four" in text
    assert "enable voice" not in text


def test_culture_set_and_show_survive_swarm_remove(env, tmp_path, capsys):  # noqa: F811
    run("sw", "create", "--repo", "/repo")
    culture = tmp_path / "culture.md"
    culture.write_text("say it plainly\n")
    assert run("sw", "culture", "set", str(culture)) == 0
    capsys.readouterr()
    assert run("sw", "culture", "show") == 0
    assert capsys.readouterr().out == "say it plainly\n"
    assert run("sw", "remove") == 0
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "culture", "show") == 0
    assert capsys.readouterr().out.endswith("say it plainly\n")
