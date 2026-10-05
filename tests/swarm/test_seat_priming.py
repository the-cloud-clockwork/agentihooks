from dataclasses import replace

import pytest

from scripts.swarm import prompt
from scripts.swarm.store import MASTER
from scripts.swarm.tick import tick
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks, workers  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

RECAPS = [
    {"occupant": "sw-eng-4", "task": "t1", "text": "latest recap text", "at": 20},
    {"occupant": "sw-eng-1", "task": "t1", "text": "older recap text", "at": 10},
]
LEARNED = [{"occupant": "sw-eng-1", "text": "learned note text", "at": 5}]


def _eng(**task):
    return prompt.build("sw", "/repo", "eng", "sw-eng-5", {"id": "t1", "title": "x", "phase": "p1", **task})


def test_the_successor_prompt_carries_the_chain_in_order():
    text = _eng(seat="eng-1@sw", handoff="handoff doc text", recaps=RECAPS, learned=LEARNED)
    order = [text.index(part) for part in ("handoff doc text", "latest recap text", "learned note text")]
    assert order == sorted(order) and order[-1] < text.index("older recap text")
    assert "eng-1@sw" in text
    assert text.index("older recap text") < text.index("~/development-ledger/sw.json")


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
    text = prompt.build("sw", "/repo", MASTER, "sw-master-2", task)
    assert text.index("caps go to four") < text.index("latest recap text") < text.index("learned note text")


def test_a_handoff_writes_a_recap_under_the_seat(env, tmp_path):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc, recap = tmp_path / "handoff.md", tmp_path / "recap.md"
    doc.write_text("next step: seam 2")
    recap.write_text("did seam 1, stopped at seam 2, promised the master a pr")
    assert run("sw", "--as", "sw-eng-1", "handoff", str(doc), "--recap", str(recap)) == 0
    (entry,) = swarm.memory.recaps("eng-1@sw")
    assert (entry["occupant"], entry["task"]) == ("sw-eng-1", "t1")
    assert entry["text"] == "did seam 1, stopped at seam 2, promised the master a pr"


def test_a_handoff_refuses_a_missing_recap_file(env, tmp_path, capsys):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    doc = tmp_path / "handoff.md"
    doc.write_text("doc")
    assert run("sw", "--as", "sw-eng-1", "handoff", str(doc), "--recap", "/no/such/recap.md") == 1
    assert "recap" in capsys.readouterr().err
    assert swarm.handoff("sw", "t1") == ""


def test_learned_appends_a_note_to_the_callers_seat(env):  # noqa: F811
    swarm, _, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    assert run("sw", "--as", "sw-eng-1", "learned", "the ledger refuses dashes in chat") == 0
    assert [n["text"] for n in swarm.memory.learned("eng-1@sw")] == ["the ledger refuses dashes in chat"]


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
