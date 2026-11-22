from types import SimpleNamespace

import fakeredis
import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hub import registry, wait_ends
from scripts.inbox.store import InboxStore
from scripts.swarm import idle, waits
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord, RedisStore

ME = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/7"
OTHER = "https://github.com/o/r/pull/8"
GREEN = PullRequest("OPEN", None, 1, False, True, head="first")
CHECKS = {"kind": "checks", "target": URL, "head": "first"}
MERGE = {"kind": "merge", "target": URL}


def _ended(outcome: str) -> str:
    return (
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    )


def _event(kind: str = "checks.completed", url: str = URL) -> Envelope:
    return Envelope("e1", kind, "sw", "pull", url, 1, 5_000, "github", {"url": url})


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(waits, "required_green", lambda target, head: True)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
    pulls = {URL: GREEN}
    reads: list[str] = []

    def github(url: str) -> PullRequest | None:
        reads.append(url)
        return pulls.get(url)

    ledger = SimpleNamespace(task=lambda slug, task_id: {"id": task_id, "pr_url": URL})
    react = wait_ends.WaitEnds(store, ledger, github, clock=lambda: 5_000)

    def hold(held: dict) -> None:
        idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on=held)

    def told() -> list[str]:
        return [item.text for item in InboxStore(store.redis).inbox("eng-1@sw")]

    return SimpleNamespace(store=store, pulls=pulls, reads=reads, react=react, hold=hold, told=told)


@pytest.mark.parametrize(
    "held, kind, pull, outcome",
    [
        (CHECKS, "checks.completed", GREEN, f"checks on {URL}, now green"),
        (MERGE, "pull.merged", PullRequest("MERGED", 2, 1, False, True), f"pull request {URL}, now merged"),
    ],
    ids=["checks-completed", "pull-merged"],
)
@pytest.mark.feature("wait-ends-on-pull-event")
def test_a_pull_event_ends_a_matching_wait_with_one_item_and_the_polled_records(
    rig: SimpleNamespace, held: dict, kind: str, pull: PullRequest, outcome: str
) -> None:
    rig.hold(held)
    rig.pulls[URL] = pull
    rig.react.handle(_event(kind))
    assert rig.told() == [_ended(outcome)]
    assert idle.wait(rig.store.redis, "sw", ME) is None
    assert idle.woke(rig.store.redis, "sw", ME) == idle.waited(rig.store.redis, "sw", ME) == 5_000
    assert idle.wait_history(rig.store.redis, "sw", ME) == [{"until": 5_000, "reason": "", "at": 1, "on": held}]


@pytest.mark.parametrize(
    "held, url, state",
    [
        (CHECKS, OTHER, "working"),
        ({"kind": "intent", "target": URL, "task": "t1"}, URL, "working"),
        (CHECKS, URL, "finished"),
    ],
    ids=["another-pull-request", "another-wait-kind", "finished-agent"],
)
@pytest.mark.feature("wait-ends-on-pull-event")
def test_an_event_leaves_a_wait_it_does_not_name_open_and_reads_nothing(
    rig: SimpleNamespace, held: dict, url: str, state: str
) -> None:
    rig.store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw", state=state))
    rig.hold(held)
    rig.react.handle(_event(url=url))
    assert idle.wait(rig.store.redis, "sw", ME)["on"] == held
    assert rig.told() == []
    assert rig.reads == []


@pytest.mark.feature("wait-ends-on-pull-event")
def test_an_event_whose_fresh_resolution_is_pending_leaves_the_wait_open(rig: SimpleNamespace) -> None:
    rig.hold(CHECKS)
    rig.pulls[URL] = PullRequest("OPEN", None, 1, False, False, head="first")
    rig.react.handle(_event())
    assert idle.wait(rig.store.redis, "sw", ME)["on"] == CHECKS
    assert rig.told() == []


@pytest.mark.feature("wait-ends-on-pull-event")
def test_a_duplicate_event_sends_no_second_item(rig: SimpleNamespace) -> None:
    rig.hold(CHECKS)
    rig.react.handle(_event())
    rig.react.handle(_event())
    assert rig.told() == [_ended(f"checks on {URL}, now green")]


@pytest.mark.feature("wait-ends-on-pull-event")
def test_the_hub_registry_wires_one_singleton_wait_end_reactor_per_pull_subject() -> None:
    bus = memory.open_bus(BusConfig())
    found = {reactor.subject: reactor for reactor in registry.configured(bus) if reactor.name.startswith("wait-ends-")}
    assert sorted(found) == [
        "swarm.*.checks.completed",
        "swarm.*.merge.dequeued",
        "swarm.*.pull.closed",
        "swarm.*.pull.merged",
    ]
    assert all(reactor.singleton for reactor in found.values())
    bus.close()
