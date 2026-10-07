import pytest

from scripts.inbox.store import InboxError, InboxStore

pytestmark = pytest.mark.xdist_group("fakeredis")


def store():
    import fakeredis

    return InboxStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.mark.parametrize("detail", ["", "  \n\t "])
def test_work_done_requires_an_outcome_without_changing_item_or_history(detail):
    inbox = store()
    item = inbox.send("sender", "receiver", "Fix checks")
    history = inbox.history(item.id)
    with pytest.raises(InboxError, match="^done needs an outcome naming where the work went$"):
        inbox.close(item.id, "receiver", "done", detail)
    assert inbox.get(item.id) == item
    assert inbox.history(item.id) == history


def test_information_may_close_bare_and_work_may_close_with_outcome():
    inbox = store()
    fyi = inbox.send("sender", "receiver", "Thanks", fyi=True)
    assert inbox.close(fyi.id, "receiver", "done").reason == "done"
    work = inbox.send("sender", "receiver", "Fix checks")
    assert inbox.close(work.id, "receiver", "done", "Fixed checks").reason == "done: Fixed checks"
