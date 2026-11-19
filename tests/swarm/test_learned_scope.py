from typing import Any

import pytest

from scripts.swarm import prompt
from scripts.tick.steps.place import primed
from tests.helpers.seat import seat_agent
from tests.swarm.test_cli import env, run  # noqa: F401


@pytest.mark.feature("swarm-learned-scope")
@pytest.mark.parametrize("entrypoint", ["command", "start", "all"])
def test_returned_notes_stay_with_the_caller_and_shared_guidance(
    env: tuple[Any, Any, Any],  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
) -> None:
    store, ledger, _ = env
    assert run("sw", "create", "--repo", "/repo") == 0
    agent = seat_agent(store, ledger, "sw", "t1")
    own = [
        ("own note", "note"),
        ("own canon", "canon"),
        ("own insight", "insight"),
        ("own data", "data"),
    ]
    foreign = [
        ("unrelated note " + "x" * 16384, "note"),
        ("unrelated data " + "y" * 16384, "data"),
        ("shared canon", "canon"),
        ("shared insight", "insight"),
    ]
    for seat, notes in ((agent.seat, own), ("ci-1@sw", foreign)):
        for text, maturity in notes:
            store.memory.learn(seat, "previous occupant", text, 1, maturity)
    store.memory.learn("eng-1@another", "other swarm", "other swarm canon", 1, "canon")
    store.memory.add_recap("ci-1@sw", "previous occupant", "old task", "unrelated recap", at=1)
    selected = own + (foreign if entrypoint == "all" else foreign[2:])
    if entrypoint == "start":
        task = primed(store, "sw", agent.seat, {"id": "t1"})
        assert {note["text"] for note in task["learned"]} == {text for text, _ in selected}
        returned = "\n".join(prompt.learned_lines(task["learned"]))
        assert "shared canon" in returned and "shared insight" in returned
        assert "own data" not in returned
    else:
        capsys.readouterr()
        arguments = ("--all",) if entrypoint == "all" else ()
        assert run("sw", "--as", agent.name, "learned", *arguments) == 0
        returned = capsys.readouterr().out
        expected = {f"{agent.seat}\t{number}\t{maturity}\t{text}" for number, (text, maturity) in enumerate(own, 1)}
        expected.update(
            f"ci-1@sw\t{number}\t{maturity}\t{text}"
            for number, (text, maturity) in enumerate(foreign, 1)
            if entrypoint == "all" or maturity in ("canon", "insight")
        )
        assert set(returned.splitlines()) == expected
    if entrypoint == "all":
        assert len(returned.encode()) > 32768
    else:
        assert len(returned.encode()) < 1000
