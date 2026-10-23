import hashlib
import json
import subprocess

import pytest

from scripts.events.bus import StateConflict
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hub import pulls
from scripts.hub.pulls import Observation, Pulls
from scripts.swarm.store import SwarmError

pytestmark = pytest.mark.unit

URL = "https://github.com/acme/widgets/pull/7"
OTHER = "https://github.com/acme/widgets/pull/8"
SUITE = {"app": "github-actions", "conclusion": "success"}


class Clock:
    now = 1000

    def __call__(self):
        return self.now


def task_changed(swarm="crew", url=URL, version=1):
    return Envelope(
        id=f"task-{swarm}-{version}",
        type="task.changed",
        swarm=swarm,
        entity="task",
        key="t1",
        version=version,
        time=900,
        author="ledger",
        data={"id": "t1", "pr_url": url},
    )


def setup(fetch=None, clock=None):
    bus = open_bus(BusConfig())
    hub = Pulls(bus, fetch=fetch or (lambda url: pytest.fail(f"unexpected fetch of {url}")), clock=clock or Clock())
    return bus, hub, bus.events.consumer("watch", "swarm.>")


def drain(consumer):
    deliveries = consumer.fetch(100, 0)
    for delivery in deliveries:
        delivery.ack()
    return [delivery.event for delivery in deliveries]


def kinds(events):
    return [(event.swarm, event.type, event.version) for event in events]


def snapshot(bus, url=URL):
    entry = bus.state(pulls.BUCKET).get(pulls.key(url))
    return None if entry is None else json.loads(entry.value)


def open_urls(bus):
    entry = bus.state(pulls.BUCKET).get(pulls.OPEN)
    return None if entry is None else json.loads(entry.value)


def test_reactor_follows_task_changes_on_one_hub_replica():
    _, hub, _ = setup()
    reactor = hub.reactor()
    assert (reactor.name, reactor.subject, reactor.singleton) == ("pulls", "swarm.*.task.*", True)
    assert reactor.handler == hub.handle
    assert reactor.poll == hub.poll


def test_hub_runner_tracks_pull_requests_from_ledger_outbox_subjects():
    from scripts.events import subjects
    from scripts.hub import registry, runner
    from scripts.swarm_ledger.repository import outbox

    bus, hub, _ = setup(lambda url: Observation(url))
    hubs = runner.HubRunner(bus, registry.Registry([hub.reactor()]), "holder")
    removed = Envelope(**{**task_changed(url=OTHER).to_dict(), "id": "gone", "type": "task.removed", "key": "t.2"})
    bus.events.publish(subjects.subject("crew", "task", outbox.token("t.2")), removed)
    bus.events.publish(subjects.subject("crew", "task", outbox.token("t1")), task_changed())
    assert hubs.run_once(timeout_ms=0)["pulls"] == ["handled", "handled"]
    assert snapshot(bus)["swarms"] == ["crew"]
    assert snapshot(bus, OTHER) is None
    assert open_urls(bus) == [URL]


def test_key_is_a_stable_digest_of_the_lowercased_url():
    assert pulls.key(URL) == "pull." + hashlib.sha256(URL.encode()).hexdigest()
    assert pulls.key(URL.replace("acme", "Acme")) == pulls.key(URL)
    assert pulls.key(URL) != pulls.key(OTHER)


def test_names_interval_and_query():
    assert (pulls.BUCKET, pulls.OPEN, pulls.POLLED, pulls.ENTITY, pulls.AUTHOR) == (
        "hub-github",
        "open",
        "polled",
        "pull",
        "github",
    )
    assert (pulls.INTERVAL_MS, pulls.FLUSH_AFTER_MS, pulls.ATTEMPTS) == (300_000, 60_000, 8)
    assert pulls.QUERY == (
        "query($url: URI!) { resource(url: $url) { ... on PullRequest {\n"
        "    state headRefOid mergeQueueEntry { id }\n"
        "    commits(last: 1) { nodes { commit { checkSuites(first: 100) {\n"
        "        nodes { databaseId status conclusion app { slug } }\n"
        "    } } } }\n"
        "} } }"
    )


def test_swarms_are_kept_sorted():
    bus, hub, _ = setup()
    hub.track("zeta", URL)
    hub.track("alpha", URL)
    assert snapshot(bus)["swarms"] == ["alpha", "zeta"]


def test_a_reopened_pull_request_returns_to_the_poll():
    bus, hub, _ = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL, "closed", "a1"))
    assert open_urls(bus) == []
    hub.observe(Observation(URL, "open", "a1"))
    assert open_urls(bus) == [URL]


def test_a_webhook_url_in_other_casing_reaches_the_tracked_pull_request():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL.replace("acme/widgets", "Acme/Widgets"), "merged", "a1"))
    events = drain(consumer)
    assert [(e.type, e.key, e.data["url"]) for e in events] == [("pull.merged", URL, URL)]
    assert open_urls(bus) == []


def test_a_task_carrying_a_pull_request_url_tracks_it_for_its_swarm():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.handle(task_changed(version=2))
    hub.handle(task_changed("other"))
    assert snapshot(bus) == {
        "url": URL,
        "swarms": ["crew", "other"],
        "state": None,
        "head": None,
        "queued": False,
        "suites": {},
        "early": {},
        "version": 0,
        "pending": [],
    }
    assert open_urls(bus) == [URL]
    hub.handle(task_changed(url=OTHER))
    assert open_urls(bus) == [URL, OTHER]
    assert drain(consumer) == []


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        7,
        "https://github.com/acme/widgets/issues/7",
        URL + "/files",
        "x" + URL,
        "https://githubxcom/acme/widgets/pull/7",
        "https://github.com/acme/widgets/pull/",
        "http://github.com/acme/widgets/pull/7",
        "https://github.com/acme/widgets/pull/7a",
    ],
)
def test_tasks_without_a_pull_request_url_track_nothing(url):
    bus, hub, _ = setup()
    event = task_changed(url=url)
    hub.handle(event)
    hub.handle(Envelope(**{**event.to_dict(), "data": {"id": "t1"}}))
    assert snapshot(bus, url if isinstance(url, str) else URL) is None
    assert open_urls(bus) is None


def test_observing_an_untracked_pull_request_publishes_nothing():
    bus, hub, consumer = setup()
    hub.observe(Observation(URL, "merged", "a1"))
    assert drain(consumer) == []
    assert snapshot(bus) is None


def test_each_change_publishes_one_event_per_swarm_with_rising_versions():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.handle(task_changed("other"))
    hub.observe(Observation(URL, "open", "a1", False))
    events = drain(consumer)
    assert kinds(events) == [("crew", "pull.opened", 1), ("other", "pull.opened", 1)]
    first = events[0]
    assert (first.entity, first.key, first.author, first.time, first.cause) == ("pull", URL, "github", 1000, None)
    assert first.data == {"url": URL, "state": "open", "head": "a1", "queued": False}
    assert first.id != events[1].id
    hub.observe(Observation(URL, "open", "b2", True))
    events = drain(consumer)
    assert kinds(events) == [
        ("crew", "pull.pushed", 2),
        ("other", "pull.pushed", 2),
        ("crew", "merge.queued", 3),
        ("other", "merge.queued", 3),
    ]
    assert events[0].data == {"url": URL, "state": "open", "head": "b2", "queued": True, "previous": "a1"}
    assert events[2].data == {"url": URL, "state": "open", "head": "b2", "queued": True}
    assert snapshot(bus)["version"] == 3
    assert snapshot(bus)["pending"] == []


def test_events_go_to_subjects_named_by_their_type():
    bus, hub, _ = setup()
    hub.handle(task_changed())
    merged = bus.events.consumer("merged", "swarm.crew.pull.merged")
    queued = bus.events.consumer("queued", "swarm.crew.merge.*")
    hub.observe(Observation(URL, "open", "a1", True))
    hub.observe(Observation(URL, "merged", "a1"))
    assert [e.type for e in drain(merged)] == ["pull.merged"]
    assert [e.type for e in drain(queued)] == ["merge.queued"]


def test_an_unchanged_observation_publishes_nothing_and_writes_nothing():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL, "open", "a1", False, "a1", {"9": SUITE}))
    drain(consumer)
    revision = bus.state(pulls.BUCKET).get(pulls.key(URL)).revision
    hub.observe(Observation(URL, "open", "a1", False, "a1", {"9": SUITE}))
    hub.observe(Observation(URL))
    assert drain(consumer) == []
    assert bus.state(pulls.BUCKET).get(pulls.key(URL)).revision == revision


def test_state_transitions_name_what_happened():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    for state in ("open", "closed", "open", "merged"):
        hub.observe(Observation(URL, state, "a1"))
    assert [e.type for e in drain(consumer)] == ["pull.opened", "pull.closed", "pull.reopened", "pull.merged"]
    assert snapshot(bus)["state"] == "merged"


def test_first_observation_of_a_merged_pull_request_reports_the_merge():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL, "merged", "a1", True))
    assert [e.type for e in drain(consumer)] == ["pull.merged"]
    assert snapshot(bus)["queued"] is False


def test_leaving_the_queue_without_merging_is_a_dequeue():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL, "open", "a1", True))
    hub.observe(Observation(URL, None, None, False))
    hub.observe(Observation(URL, None, None, None))
    assert [e.type for e in drain(consumer)] == ["pull.opened", "merge.queued", "merge.dequeued"]
    assert snapshot(bus)["queued"] is False


def test_a_merge_out_of_the_queue_reports_only_the_merge():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL, "open", "a1", True))
    drain(consumer)
    hub.observe(Observation(URL, "merged", "a1", False))
    events = drain(consumer)
    assert [e.type for e in events] == ["pull.merged"]
    assert events[0].data == {"url": URL, "state": "merged", "head": "a1", "queued": False}


def test_completed_check_suites_report_once_per_head():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL, "open", "a1"))
    drain(consumer)
    red = {"app": "sonar", "conclusion": "failure"}
    hub.observe(Observation(URL, checks_head="a1", suites={"9": SUITE, "4": red}))
    events = drain(consumer)
    assert [(e.type, e.data["suite"], e.data["conclusion"], e.data["app"]) for e in events] == [
        ("checks.completed", "4", "failure", "sonar"),
        ("checks.completed", "9", "success", "github-actions"),
    ]
    assert events[0].data == {
        "url": URL,
        "state": "open",
        "head": "a1",
        "queued": False,
        "suite": "4",
        "app": "sonar",
        "conclusion": "failure",
    }
    hub.observe(Observation(URL, checks_head="a1", suites={"9": SUITE}))
    assert drain(consumer) == []
    hub.observe(Observation(URL, checks_head="a1", suites={"4": SUITE}))
    assert [(e.type, e.data["suite"], e.data["conclusion"]) for e in drain(consumer)] == [
        ("checks.completed", "4", "success")
    ]
    assert snapshot(bus)["suites"] == {"9": SUITE, "4": SUITE}


def test_check_suites_of_another_head_are_ignored_and_a_push_clears_old_suites():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL, checks_head="a1", suites={"9": SUITE}))
    assert drain(consumer) == []
    hub.observe(Observation(URL, "open", "a1", checks_head="a1", suites={"9": SUITE}))
    drain(consumer)
    hub.observe(Observation(URL, "open", "b2"))
    assert snapshot(bus)["suites"] == {}
    hub.observe(Observation(URL, checks_head="a1", suites={"5": SUITE}))
    assert [e.type for e in drain(consumer)] == ["pull.pushed"]
    assert snapshot(bus)["head"] == "b2"
    assert snapshot(bus)["suites"] == {}


def test_a_failed_publish_stays_pending_until_the_poll_flushes_it():
    clock = Clock()
    bus, hub, consumer = setup(lambda url: Observation(url), clock)
    hub.handle(task_changed())
    publish = bus.events.publish
    calls = []

    def broken(subject, event):
        calls.append(subject)
        raise ConnectionError("bus down")

    bus.events.publish = broken
    with pytest.raises(ConnectionError):
        hub.observe(Observation(URL, "open", "a1"))
    assert calls == ["swarm.crew.pull.opened"]
    assert [row["subject"] for row in snapshot(bus)["pending"]] == ["swarm.crew.pull.opened"]
    bus.events.publish = publish
    hub.observe(Observation(URL, "open", "a1"))
    assert drain(consumer) == []
    clock.now = 1000 + pulls.FLUSH_AFTER_MS - 1
    hub.poll()
    assert drain(consumer) == []
    assert len(snapshot(bus)["pending"]) == 1
    clock.now += pulls.INTERVAL_MS
    hub.poll()
    assert kinds(drain(consumer)) == [("crew", "pull.opened", 1)]
    assert snapshot(bus)["pending"] == []


def test_an_observation_publishes_only_the_events_it_created():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL, "open", "a1"))
    first = drain(consumer)
    state = bus.state(pulls.BUCKET)
    entry = state.get(pulls.key(URL))
    leftover = {"subject": "swarm.crew.pull.opened", "event": first[0].to_dict()}
    state.update(
        pulls.key(URL), json.dumps({**json.loads(entry.value), "pending": [leftover]}).encode(), entry.revision
    )
    hub.observe(Observation(URL, "closed", "a1"))
    assert [e.type for e in drain(consumer)] == ["pull.closed"]
    assert snapshot(bus)["pending"] == [leftover]


def test_a_terminal_pull_request_left_in_the_index_is_removed_on_its_next_observation():
    bus, hub, _ = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL, "merged", "a1"))
    state = bus.state(pulls.BUCKET)
    entry = state.get(pulls.OPEN)
    state.update(pulls.OPEN, json.dumps([URL]).encode(), entry.revision)
    hub.observe(Observation(URL, "merged", "a1"))
    assert open_urls(bus) == []


def test_check_suites_that_arrive_before_their_head_report_when_the_head_does():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.observe(Observation(URL, "open", "a1"))
    drain(consumer)
    red = {"app": "sonar", "conclusion": "failure"}
    hub.observe(Observation(URL, checks_head="b2", suites={"9": SUITE}))
    hub.observe(Observation(URL, checks_head="b2", suites={"4": red}))
    hub.observe(Observation(URL, checks_head="c3", suites={"5": SUITE}))
    assert drain(consumer) == []
    assert snapshot(bus)["early"] == {"b2": {"9": SUITE, "4": red}, "c3": {"5": SUITE}}
    hub.observe(Observation(URL, "open", "b2"))
    events = drain(consumer)
    assert [(e.type, e.data.get("suite"), e.version) for e in events] == [
        ("pull.pushed", None, 2),
        ("checks.completed", "4", 3),
        ("checks.completed", "9", 4),
    ]
    assert events[1].data["head"] == "b2"
    assert snapshot(bus)["suites"] == {"9": SUITE, "4": red}
    assert snapshot(bus)["early"] == {}


def test_a_contended_snapshot_write_retries_on_the_newer_revision():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    state = bus.state(pulls.BUCKET)
    update = state.update
    raced = []

    def racing(key, value, revision):
        if key == pulls.key(URL) and not raced:
            raced.append(revision)
            entry = state.get(key)
            update(key, json.dumps({**json.loads(entry.value), "swarms": ["crew", "late"]}).encode(), revision)
        return update(key, value, revision)

    state.update = racing
    hub.observe(Observation(URL, "open", "a1"))
    assert kinds(drain(consumer)) == [("crew", "pull.opened", 1), ("late", "pull.opened", 1)]


def test_a_snapshot_that_stays_contended_raises():
    bus, hub, _ = setup()
    hub.handle(task_changed())
    state = bus.state(pulls.BUCKET)
    attempts = []

    def always(key, value, revision):
        attempts.append(key)
        raise StateConflict("contended")

    state.update = always
    with pytest.raises(StateConflict, match=r"^hub github state remained contended$"):
        hub.observe(Observation(URL, "open", "a1"))
    assert len(attempts) == 8


def test_terminal_pull_requests_leave_the_poll_and_are_not_tracked_again():
    bus, hub, consumer = setup()
    hub.handle(task_changed())
    hub.handle(task_changed(url=OTHER))
    hub.observe(Observation(URL, "closed", "a1"))
    assert open_urls(bus) == [OTHER]
    hub.handle(task_changed(version=3))
    hub.handle(task_changed("late"))
    assert open_urls(bus) == [OTHER]
    assert snapshot(bus)["swarms"] == ["crew", "late"]
    hub.observe(Observation(URL, "merged", "a1"))
    assert [(e.swarm, e.type) for e in drain(consumer)] == [
        ("crew", "pull.closed"),
        ("crew", "pull.merged"),
        ("late", "pull.merged"),
    ]
    assert open_urls(bus) == [OTHER]


def test_poll_publishes_what_missed_deliveries_would_have_and_nothing_a_webhook_already_did():
    clock = Clock()
    seen = {URL: Observation(URL, "open", "a1", False, "a1", {"9": SUITE}), OTHER: Observation(OTHER, "open", "c3")}
    fetched = []

    def fetch(url):
        fetched.append(url)
        return seen[url]

    bus, hub, consumer = setup(fetch, clock)
    hub.handle(task_changed())
    hub.handle(task_changed(url=OTHER))
    hub.observe(Observation(URL, "open", "a1", False))
    drain(consumer)
    hub.poll()
    assert fetched == [URL, OTHER]
    assert [(e.key, e.type) for e in drain(consumer)] == [(URL, "checks.completed"), (OTHER, "pull.opened")]
    seen[URL] = Observation(URL, "merged", "a1", False, "a1", {"9": SUITE})
    clock.now += pulls.INTERVAL_MS - 1
    hub.poll()
    assert fetched == [URL, OTHER]
    clock.now += 1
    hub.poll()
    assert fetched == [URL, OTHER, URL, OTHER]
    assert [(e.key, e.type) for e in drain(consumer)] == [(URL, "pull.merged")]
    clock.now += pulls.INTERVAL_MS
    hub.poll()
    assert fetched == [URL, OTHER, URL, OTHER, OTHER]


def test_poll_records_its_pass_time():
    clock = Clock()
    bus, hub, _ = setup(lambda url: Observation(url), clock)
    hub.poll()
    assert json.loads(bus.state(pulls.BUCKET).get(pulls.POLLED).value) == 1000
    clock.now = 1000 + pulls.INTERVAL_MS
    hub.poll()
    assert json.loads(bus.state(pulls.BUCKET).get(pulls.POLLED).value) == 1000 + pulls.INTERVAL_MS


@pytest.mark.parametrize("error", [SwarmError("gone"), KeyError("state"), TypeError("none")])
def test_poll_skips_a_pull_request_it_cannot_read_and_polls_the_rest(capsys, error):
    fetched = []

    def fetch(url):
        fetched.append(url)
        if url == URL:
            raise error
        return Observation(url, "open", "c3")

    bus, hub, consumer = setup(fetch)
    hub.handle(task_changed())
    hub.handle(task_changed(url=OTHER))
    hub.poll()
    assert fetched == [URL, OTHER]
    assert capsys.readouterr().err == f"hub: GitHub poll skipped {URL}: {error}\n"
    assert [(e.key, e.type) for e in drain(consumer)] == [(OTHER, "pull.opened")]
    hub.poll()
    assert fetched == [URL, OTHER]


def test_poll_stops_at_a_rate_limit_and_waits_for_the_next_interval(capsys):
    fetched = []

    def fetch(url):
        fetched.append(url)
        raise SwarmError("API rate limit exceeded")

    bus, hub, consumer = setup(fetch)
    hub.handle(task_changed())
    hub.handle(task_changed(url=OTHER))
    hub.poll()
    assert fetched == [URL]
    assert capsys.readouterr().err == f"hub: GitHub poll skipped {URL}: API rate limit exceeded\n"
    assert json.loads(bus.state(pulls.BUCKET).get(pulls.POLLED).value) == 1000
    assert drain(consumer) == []


def test_poll_yields_when_the_replica_loses_its_lease():
    fetched = []
    bus, hub, _ = setup(lambda url: fetched.append(url) or Observation(url))
    hub.handle(task_changed())
    hub.handle(task_changed(url=OTHER))
    hub.poll(lambda: False)
    assert fetched == []
    assert bus.state(pulls.BUCKET).get(pulls.POLLED) is None
    answers = iter([True, True, False])
    hub.poll(lambda: next(answers))
    assert fetched == [URL]
    assert bus.state(pulls.BUCKET).get(pulls.POLLED) is None


def completed(conclusion="SUCCESS", status="COMPLETED", suite=9, app="github-actions"):
    return {"databaseId": suite, "status": status, "conclusion": conclusion, "app": {"slug": app}}


def graphql_reply(state="OPEN", queued=None, suites=(), commits=True):
    nodes = [{"commit": {"checkSuites": {"nodes": list(suites)}}}] if commits else []
    pull = {"state": state, "headRefOid": "a1", "mergeQueueEntry": queued, "commits": {"nodes": nodes}}
    return json.dumps({"data": {"resource": pull}})


def runner(stdout, calls):
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    return run


def test_fetch_reads_the_pull_request_snapshot_from_github():
    calls = []
    suites = [completed(), completed("FAILURE", suite=4, app="sonar"), completed(None, "IN_PROGRESS", 5)]
    reply = graphql_reply("MERGED", {"id": "q"}, suites)
    assert pulls.fetch(URL, runner(reply, calls)) == Observation(
        URL,
        "merged",
        "a1",
        True,
        "a1",
        {"9": {"app": "github-actions", "conclusion": "success"}, "4": {"app": "sonar", "conclusion": "failure"}},
    )
    assert calls == [["gh", "api", "graphql", "-f", f"query={pulls.QUERY}", "-f", f"url={URL}"]]


@pytest.mark.parametrize(("state", "expected"), [("OPEN", "open"), ("CLOSED", "closed"), ("MERGED", "merged")])
def test_fetch_maps_github_states(state, expected):
    observed = pulls.fetch(URL, runner(graphql_reply(state, commits=False), []))
    assert observed == Observation(URL, expected, "a1", False, "a1", {})


def test_fetch_reads_a_suite_without_an_app():
    suite = {**completed(), "app": None}
    observed = pulls.fetch(URL, runner(graphql_reply(suites=[suite]), []))
    assert observed.suites == {"9": {"app": "", "conclusion": "success"}}


def test_fetch_refuses_a_url_that_is_not_a_pull_request():
    reply = json.dumps({"data": {"resource": None}})
    with pytest.raises(SwarmError, match=r"^GitHub has no pull request at https://github.com/acme/widgets/pull/7$"):
        pulls.fetch(URL, runner(reply, []))


def test_fetch_defaults_to_the_gh_command(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", runner(graphql_reply(), calls))
    assert pulls.fetch(URL).state == "open"
    assert calls[0][:3] == ["gh", "api", "graphql"]


def test_pulls_default_to_github_fetch_and_wall_clock(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", runner(graphql_reply(), calls))
    bus = open_bus(BusConfig())
    hub = Pulls(bus)
    hub.handle(task_changed())
    hub.poll()
    assert len(calls) == 1
    assert json.loads(bus.state(pulls.BUCKET).get(pulls.POLLED).value) > 0
