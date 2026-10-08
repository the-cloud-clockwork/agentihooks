import pytest

from scripts.inbox.seats import Occupancy, SeatRegistry, is_seat, seat_address

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def seats():
    import fakeredis

    return SeatRegistry(fakeredis.FakeRedis(decode_responses=True))


def test_a_seat_address_is_the_seat_at_the_swarm():
    assert seat_address("rig", "eng-1") == "eng-1@rig"
    assert is_seat("eng-1@rig")
    assert not is_seat("rig-eng-13")


def test_an_empty_seat_has_no_occupant_and_generation_zero(seats):
    assert seats.occupant("eng-1@rig") == Occupancy("", 0)


def test_each_new_occupant_bumps_the_generation(seats):
    assert seats.occupy("eng-1@rig", "rig-eng-1", at=10) == 1
    assert seats.occupy("eng-1@rig", "rig-eng-4", at=20) == 2
    assert seats.occupant("eng-1@rig") == Occupancy("rig-eng-4", 2)


def test_the_occupancy_history_keeps_every_generation(seats):
    for at, name in enumerate(("rig-eng-1", "rig-eng-4", "rig-eng-9")):
        seats.occupy("eng-1@rig", name, at=at)
    assert seats.history("eng-1@rig") == [
        {"generation": 1, "occupant": "rig-eng-1", "at": 0},
        {"generation": 2, "occupant": "rig-eng-4", "at": 1},
        {"generation": 3, "occupant": "rig-eng-9", "at": 2},
    ]


def test_a_note_joins_the_history_and_keeps_the_occupant(seats):
    seats.occupy("eng-1@rig", "rig-eng-1", at=1)
    seats.note("eng-1@rig", "promoted", "the master launch failed", 5)
    assert seats.history("eng-1@rig") == [
        {"generation": 1, "occupant": "rig-eng-1", "at": 1},
        {"generation": 1, "occupant": "rig-eng-1", "at": 5, "event": "promoted", "detail": "the master launch failed"},
    ]
    assert seats.occupant("eng-1@rig") == Occupancy("rig-eng-1", 1)


def test_seat_of_names_only_the_seat_a_session_still_holds(seats):
    seats.occupy("eng-1@rig", "rig-eng-1", at=1)
    assert seats.seat_of("rig-eng-1") == "eng-1@rig"
    seats.occupy("eng-1@rig", "rig-eng-4", at=2)
    assert seats.seat_of("rig-eng-1") == ""
    assert seats.seat_of("rig-eng-4") == "eng-1@rig"
    assert seats.seat_of("nobody") == ""


def test_a_seat_that_keeps_changing_refuses_after_bounded_attempts(seats):
    from scripts.inbox.seats import OCCUPY_ATTEMPTS, SeatError

    watch, calls = seats.watch, []

    def contended(pipe, address):
        seen = watch(pipe, address)
        calls.append(seen)
        seats.redis.hset(seats.key(address), "generation", seen.generation + 10)
        return seen

    seats.watch = contended
    with pytest.raises(SeatError):
        seats.occupy("eng-1@rig", "rig-eng-1", at=1)
    assert len(calls) == OCCUPY_ATTEMPTS and seats.history("eng-1@rig") == []


def test_agent_seats_read_every_seat_at_once_and_page_the_legacy_scan(seats, monkeypatch):
    from scripts.inbox.seats import PREFIX
    from scripts.swarm.naming import NameRegistry

    names = NameRegistry(seats.redis)
    names.mint_code("rig", "rig", "/repo")
    minted = [names.next("rig", "eng") for _ in range(3)]
    seats.occupy("eng-1@rig", minted[0], at=1)
    seats.occupy("eng-2@rig", minted[2], at=2)
    seats.occupy("eng-3@rig", "rig-eng-9", at=3)
    scans, scan = [], seats.redis.scan_iter
    monkeypatch.setattr(seats.redis, "scan_iter", lambda **kw: scans.append(kw) or scan(**kw))
    get, mget, reads = seats.redis.get, seats.redis.mget, []
    monkeypatch.setattr(seats.redis, "get", lambda key: pytest.fail(key) if f"{PREFIX}-of:" in key else get(key))
    monkeypatch.setattr(seats.redis, "mget", lambda keys: reads.append(len(keys)) or mget(keys))
    assert seats.agent_seats("rig") == [("rig-eng-9", "eng-3@rig"), (minted[0], "eng-1@rig"), (minted[2], "eng-2@rig")]
    assert reads == [4]
    assert seats.agent_names("rig") == ["rig-eng-9", minted[0], minted[2]]
    assert scans == [{"match": f"{PREFIX}-of:rig-*", "count": 1000}] * 2


def test_exits_reads_every_recorded_exit_at_once(seats, monkeypatch):
    seats.record_exit("rig-eng-1", "eng-1@rig", "exited")
    mget = seats.redis.mget
    monkeypatch.setattr(seats.redis, "get", lambda key: pytest.fail(f"a read per exit: {key}"))
    monkeypatch.setattr(seats.redis, "mget", lambda keys: mget(keys) if keys else pytest.fail("an empty MGET"))
    assert seats.exits(["rig-eng-1", "rig-eng-2"]) == {
        "rig-eng-1": {"seat": "eng-1@rig", "reason": "exited"},
        "rig-eng-2": {},
    }
    assert seats.exits([]) == {}
    assert seats.agent_seats("nobody") == []
