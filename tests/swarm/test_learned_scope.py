from typing import Any

import pytest

from scripts.swarm import priming_trace, prompt
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
    for seat in (agent.seat, "ci-1@sw"):
        store.memory.learn(seat, "previous occupant", "retired canon", 1, "canon")
        store.memory.retire(seat, 5, "master", "obsolete", 2)
    selected = own + (foreign if entrypoint == "all" else foreign[2:])
    if entrypoint == "start":
        task = primed(store, "sw", agent.seat, {"id": "t1"})
        assert {note["text"] for note in task["learned"]} == {text for text, _ in selected}
        returned = "\n".join(prompt.learned_lines(task["learned"]))
        assert "shared canon" in returned and "shared insight" in returned
        assert "own data" not in returned
        rows = {row["text"]: row for row in priming_trace.rows("sw", task)}
        assert rows["shared canon"]["source"] == "learned:ci-1@sw#3"
        assert rows["shared insight"]["locator"] == {"seat": "ci-1@sw", "note": 4}
        assert "unrelated recap" not in rows
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


@pytest.mark.feature("swarm-learned-scope")
@pytest.mark.parametrize("all_seats", [False, True])
def test_scoped_memory_preserves_live_notes_and_source_ordinals(
    env: tuple[Any, Any, Any],  # noqa: F811
    all_seats: bool,
) -> None:
    from scripts.inbox.seats import SeatMemory

    store, _, _ = env
    memory = SeatMemory(store.redis)
    own = [("own note", "note"), ("own data", "data"), ("own canon", "canon"), ("own insight", "insight")]
    foreign = [
        ("foreign note", "note"),
        ("foreign data", "data"),
        ("shared canon", "canon"),
        ("shared insight", "insight"),
    ]
    for seat, notes in (("eng-1@sw", own), ("ci-1@sw", foreign)):
        store.memory.add_recap(seat, "previous occupant", "old task", "unrelated recap", at=1)
        for text, maturity in notes:
            store.memory.learn(seat, "previous occupant", text, 1, maturity)
        store.memory.learn(seat, "previous occupant", "retired canon", 1, "canon")
        store.memory.retire(seat, 5, "master", "obsolete", 2)
    store.memory.learn("eng-1@another", "previous occupant", "other swarm canon", 1, "canon")
    rows = memory.scoped("sw", "eng-1@sw", all_seats=all_seats)
    expected = {
        ("eng-1@sw", 1, "own note", "note"),
        ("eng-1@sw", 2, "own data", "data"),
        ("eng-1@sw", 3, "own canon", "canon"),
        ("eng-1@sw", 4, "own insight", "insight"),
        ("ci-1@sw", 3, "shared canon", "canon"),
        ("ci-1@sw", 4, "shared insight", "insight"),
    }
    if all_seats:
        expected.update({("ci-1@sw", 1, "foreign note", "note"), ("ci-1@sw", 2, "foreign data", "data")})
    assert {(row["seat"], row["number"], row["text"], row["maturity"]) for row in rows} == expected
    assert len(rows) == len(expected)
    assert all(row["occupant"] == "previous occupant" and row["at"] == 1 for row in rows)


@pytest.mark.feature("swarm-learned-scope")
def test_scoped_memory_without_matching_notes_is_empty(env: tuple[Any, Any, Any]) -> None:  # noqa: F811
    from scripts.inbox.seats import SeatMemory

    store, _, _ = env
    memory = SeatMemory(store.redis)
    store.memory.learn("eng-1@another", "previous occupant", "other swarm canon", 1, "canon")
    assert memory.scoped("sw", "eng-1@sw") == []


@pytest.mark.feature("swarm-learned-scope")
def test_scoped_memory_reads_learned_lists_without_listing_agent_pointers(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.inbox.seats import SeatMemory, SeatRegistry

    store, _, _ = env
    store.memory.learn("eng-1@sw", "previous occupant", "own note", 1, "note")
    store.memory.learn("ci-1@sw", "previous occupant", "shared canon", 1, "canon")
    store.memory.learn("eng-1@sw-doctor", "previous occupant", "doctor canon", 1, "canon")
    store.memory.learn("x:eng-1@sw", "previous occupant", "colon canon", 1, "canon")

    def refused(self: SeatRegistry, slug: str) -> list[tuple[str, str]]:
        raise AssertionError(f"learned notes listed every agent seat of {slug}")

    monkeypatch.setattr(SeatRegistry, "agent_seats", refused)
    rows = SeatMemory(store.redis).scoped("sw", "eng-1@sw")
    assert [(row["seat"], row["number"], row["text"]) for row in rows] == [
        ("ci-1@sw", 1, "shared canon"),
        ("eng-1@sw", 1, "own note"),
    ]


@pytest.mark.feature("swarm-learned-scope")
def test_scoped_memory_asks_redis_only_for_the_swarm_learned_keys(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.inbox.seats import PREFIX, SCAN_PAGE, SeatMemory

    store, _, _ = env
    store.memory.learn("eng-1@sw", "previous occupant", "own note", 1, "note")
    scans: list[dict[str, object]] = []
    scan = store.redis.scan_iter

    def recorded(**options: Any) -> Any:
        scans.append(options)
        return scan(**options)

    monkeypatch.setattr(store.redis, "scan_iter", recorded)
    rows = SeatMemory(store.redis).scoped("sw", "eng-1@sw")
    assert [row["text"] for row in rows] == ["own note"]
    assert scans == [{"match": f"{PREFIX}:*@sw:learned", "count": SCAN_PAGE}]


@pytest.mark.feature("swarm-learned-scope")
def test_scoped_memory_reads_the_index_after_its_first_scan(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.inbox.seats import SeatMemory

    store, _, _ = env
    store.memory.learn("eng-1@sw", "previous occupant", "own note", 1, "note")
    memory = SeatMemory(store.redis)
    memory.scoped("sw", "eng-1@sw")
    store.memory.learn("ci-1@sw", "previous occupant", "later canon", 2, "canon")
    store.memory.learn("x@eng-1@sw", "previous occupant", "two at canon", 3, "canon")
    scans: list[dict[str, object]] = []
    scan = store.redis.scan_iter

    def recorded(**options: Any) -> Any:
        scans.append(options)
        return scan(**options)

    monkeypatch.setattr(store.redis, "scan_iter", recorded)
    rows = memory.scoped("sw", "eng-1@sw")
    assert [(row["seat"], row["text"]) for row in rows] == [("ci-1@sw", "later canon"), ("eng-1@sw", "own note")]
    assert scans == []
    assert store.redis.smembers(memory.learned_index("sw")) == {"eng-1@sw", "ci-1@sw", "x@eng-1@sw"}


@pytest.mark.feature("swarm-learned-scope")
def test_notes_written_before_the_index_load_through_one_scan(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from scripts.inbox.seats import LEARNED_RESCAN_S, PREFIX, SCAN_PAGE, SeatMemory

    store, _, _ = env
    memory = SeatMemory(store.redis)
    entry = {"occupant": "previous occupant", "text": "older canon", "at": 1, "maturity": "canon"}
    store.redis.rpush(memory.key("ci-1@sw", "learned"), json.dumps(entry))
    store.redis.rpush(memory.key("ci-1@sw-doctor", "learned"), json.dumps(entry))
    scans: list[dict[str, object]] = []
    scan = store.redis.scan_iter

    def recorded(**options: Any) -> Any:
        scans.append(options)
        return scan(**options)

    monkeypatch.setattr(store.redis, "scan_iter", recorded)
    first = memory.scoped("sw", "eng-1@sw")
    again = memory.scoped("sw", "eng-1@sw")
    marker = f"{memory.learned_index('sw')}:listed"
    assert [(row["seat"], row["text"]) for row in first] == [("ci-1@sw", "older canon")]
    assert again == first
    assert scans == [{"match": f"{PREFIX}:*@sw:learned", "count": SCAN_PAGE}]
    assert store.redis.get(marker) == "1"
    assert 0 < store.redis.ttl(marker) <= LEARNED_RESCAN_S
    assert not store.redis.exists(f"{memory.learned_index('sw')}:claim")


@pytest.mark.feature("swarm-learned-scope")
def test_an_expired_scan_marker_loads_notes_written_without_the_index(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from scripts.inbox.seats import SeatMemory

    store, _, _ = env
    memory = SeatMemory(store.redis)
    store.memory.learn("eng-1@sw", "previous occupant", "own note", 1, "note")
    memory.scoped("sw", "eng-1@sw")
    entry = {"occupant": "older release", "text": "unindexed canon", "at": 2, "maturity": "canon"}
    store.redis.rpush(memory.key("ci-1@sw", "learned"), json.dumps(entry))
    scans: list[dict[str, object]] = []
    scan = store.redis.scan_iter

    def recorded(**options: Any) -> Any:
        scans.append(options)
        return scan(**options)

    monkeypatch.setattr(store.redis, "scan_iter", recorded)
    held = memory.scoped("sw", "eng-1@sw")
    store.redis.delete(f"{memory.learned_index('sw')}:listed", f"{memory.learned_index('sw')}:claim")
    expired = memory.scoped("sw", "eng-1@sw")
    assert [row["text"] for row in held] == ["own note"]
    assert [row["text"] for row in expired] == ["unindexed canon", "own note"]
    assert len(scans) == 1


@pytest.mark.feature("swarm-learned-scope")
def test_a_failed_scan_leaves_the_index_unlisted_and_a_held_claim_reads_the_index(
    env: tuple[Any, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from scripts.inbox.seats import SeatMemory

    store, _, _ = env
    memory = SeatMemory(store.redis)
    index = memory.learned_index("sw")
    store.memory.learn("eng-1@sw", "previous occupant", "own note", 1, "note")
    entry = {"occupant": "older release", "text": "unindexed canon", "at": 2, "maturity": "canon"}
    store.redis.rpush(memory.key("ci-1@sw", "learned"), json.dumps(entry))
    scan = store.redis.scan_iter

    def lost(**options: Any) -> Any:
        raise TimeoutError("scan timed out")

    monkeypatch.setattr(store.redis, "scan_iter", lost)
    with pytest.raises(TimeoutError):
        memory.scoped("sw", "eng-1@sw")
    held = memory.scoped("sw", "eng-1@sw")
    assert not store.redis.exists(f"{index}:listed")
    assert store.redis.get(f"{index}:claim") == "1"
    assert [row["text"] for row in held] == ["own note"]
    store.redis.delete(f"{index}:claim")
    monkeypatch.setattr(store.redis, "scan_iter", scan)
    retried = memory.scoped("sw", "eng-1@sw")
    assert [row["text"] for row in retried] == ["unindexed canon", "own note"]
    assert store.redis.exists(f"{index}:listed")


@pytest.mark.feature("swarm-learned-scope")
def test_learned_help_explains_the_explicit_full_listing(capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.swarm.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as result:
        parser.parse_args(["sw", "learned", "--help"])
    assert result.value.code == 0
    option_help = capsys.readouterr().out.split("\n  --all", 1)[1].split("\n  --", 1)[0]
    assert " ".join(option_help.split()) == "list every seat instead of the caller and shared guidance"
