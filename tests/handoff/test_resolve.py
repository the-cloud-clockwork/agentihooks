import subprocess

import pytest

from scripts.handoff.resolve import Resolver
from scripts.inbox.seats import SeatMemory
from scripts.inbox.store import InboxStore
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_workspace

pytestmark = pytest.mark.xdist_group("fakeredis")

LEDGER = {"tasks": [{"id": "t1"}], "phases": [{"id": "p7"}], "questions": [], "followups": [{"id": "f1"}]}


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def _resolver(redis, gh_code=0, seen=None):
    def run(argv, **_):
        if seen is not None:
            seen.append(argv)
        return subprocess.CompletedProcess(argv, gh_code, stdout="", stderr="")

    return Resolver("sw", redis, ledger_state=lambda slug: LEDGER if slug == "sw" else {}, run=run)


@pytest.mark.parametrize("address", ["ledger:sw/tasks/t1", "ledger:sw/phases/p7", "ledger:sw/followups/f1"])
def test_a_ledger_item_that_exists_resolves(redis, address):
    assert _resolver(redis)(address)


@pytest.mark.parametrize("address", ["ledger:sw/tasks/t9", "ledger:sw/questions/q1", "ledger:other/tasks/t1"])
def test_a_ledger_item_that_does_not_exist_does_not_resolve(redis, address):
    assert not _resolver(redis)(address)


def test_a_github_link_resolves_through_gh(redis):
    seen = []
    assert _resolver(redis, seen=seen)("https://github.com/o/r/pull/12")
    assert seen == [["gh", "api", "repos/o/r/issues/12", "--silent"]]
    assert not _resolver(redis, gh_code=1)("https://github.com/o/r/issues/404")


def test_a_work_folder_note_resolves_when_the_file_is_there(redis):
    folder = ledger_workspace.folder("sw", "t1")
    folder.mkdir(parents=True)
    (folder / "progress.md").write_text("step one landed")
    assert _resolver(redis)("workspace:t1/progress")
    assert not _resolver(redis)("workspace:t1/proof")
    assert not _resolver(redis)("workspace:t1/secrets")


def test_a_seat_recap_resolves_once_the_seat_has_one(redis):
    assert not _resolver(redis)("recap:eng-1@sw")
    SeatMemory(redis).add_recap("eng-1@sw", "engineer@a1b2c3-0001", "t1", "did seam one", 1)
    assert _resolver(redis)("recap:eng-1@sw")


def test_an_inbox_item_resolves_when_it_exists(redis):
    item = InboxStore(redis).send("ci@a1b2c3-0001", "engineer@a1b2c3-0001", "contract confirmed")
    assert _resolver(redis)(f"inbox:{item.id}")
    assert not _resolver(redis)("inbox:deadbeef0000")


def test_without_redis_seat_and_inbox_addresses_do_not_resolve():
    resolver = Resolver("sw", None, ledger_state=lambda slug: LEDGER)
    assert not resolver("recap:eng-1@sw") and not resolver("inbox:deadbeef0000")
    assert resolver("ledger:sw/tasks/t1")


def test_an_unreadable_ledger_resolves_nothing(redis):
    def broken(slug):
        raise SwarmError("ledger server down")

    assert not Resolver("sw", redis, ledger_state=broken)("ledger:sw/tasks/t1")
