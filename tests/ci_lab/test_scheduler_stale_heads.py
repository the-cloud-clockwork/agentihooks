"""The lab scheduler ends the work of pull request heads nobody wants graded any more.

A pull request head travels with a lane naming its head branch, so a newer head supersedes the older one; a closed
pull request retires its lane; and the first pass of a scheduler retires the live pull request grades whose head is
neither an open pull request head nor a plant branch head, from one listing a repository.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
import requests

from scripts.ci_lab import budget, intake, scheduler, stale_heads, webhooks
from scripts.ci_lab.checks import API
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.github_app import GitAuthRefused, GitHubAppAuth
from tests.ci_lab.test_scheduler import (
    ACTOR,
    APP_SETTINGS,
    BASE,
    HEAD_A,
    HEAD_B,
    REPO,
    SLUG,
    Consumer,
    Delivery,
    Expander,
    FakeKube,
    Inbox,
    Lab,
    Run,
    fake_redis,
    plan,
    unit,
)

OTHER_REPO = f"{REPO}-bundle"
HEAD_C = "f" * 40
OTHER_BASE = "d" * 40
THIRD_BASE = "e" * 40
OWNER = "the-cloud-clockwork"
PULLS = f"{API}/repos/{SLUG}/pulls?state=open&per_page=100"
PULLS_NEXT = f"{API}/repositories/7/pulls?state=open&per_page=100&page=2"
PLANTS = f"{API}/repos/{SLUG}/git/matching-refs/heads/diffcheck/lab-plant-?per_page=100"
PLANT = {"ref": "refs/heads/diffcheck/lab-plant-size-green", "object": {"sha": HEAD_B}}
ELSEWHERE = f"{API}.example.com/repositories/7/pulls?state=open&per_page=100&page=2"
BEARER = f"Bearer token of {SLUG}"
PULL_LANE = f"{REPO}|feature|{int(Priority.PULL_REQUEST)}"
Asked = tuple[str, str]


@dataclass
class Wanted:
    heads: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    failing: dict[str, Exception] = field(default_factory=dict[str, Exception])
    asked: list[str] = field(default_factory=list[str])

    def __call__(self, repo: str) -> frozenset[str]:
        self.asked.append(repo)
        if repo in self.failing:
            raise self.failing[repo]
        return frozenset(self.heads.get(repo, set()))


@dataclass
class Listed(Lab):
    wanted: Wanted | None = None

    def build(self, quiet_ms: int, retries: Policy, spending: budget.Budget | None) -> scheduler.Scheduler:
        ports = scheduler.Ports(self.inbox, self.expander, self.checks, self.hotfixes, self.kube, self.wanted)
        settings = scheduler.Settings(frozenset({ACTOR}), quiet_ms, retries)
        return scheduler.Scheduler(self.client, self.objects, ports, settings, spending)


@dataclass
class Token:
    slug: str

    def get_token(self) -> str:
        return f"token of {self.slug}"


@pytest.fixture
def lab(tmp_path: Path) -> Callable[..., Run]:
    def make(plans: dict[str, scheduler.Plan], quiet_ms: int = 0, listed: bool = True) -> Run:
        wanted = Wanted() if listed else None
        state = Listed(fake_redis(), LocalObjects(tmp_path / "objects"), Inbox(), Expander(plans), wanted=wanted)
        return Run(state, state.build(quiet_ms, Policy(), None))

    return make


def _wanted(run: Run) -> Wanted:
    state = run.lab
    assert isinstance(state, Listed) and state.wanted is not None
    return state.wanted


def _restarted(run: Run) -> Run:
    return Run(run.lab, run.lab.build(0, Policy(), None))


def _pull(action: str, head: str, branch: str = "feature", owner: str = OWNER, base: str = BASE) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"html_url": REPO},
        "sender": {"login": ACTOR},
        "pull_request": {
            "head": {"sha": head, "ref": branch, "label": f"{owner}:{branch}"},
            "base": {"sha": base, "label": f"{OWNER}:dev"},
            "user": {"login": ACTOR},
        },
    }


def _deliver(run: Run, payload: Mapping[str, Any]) -> None:
    deliveries = [Delivery(envelope(event)) for event in webhooks.head_events("pull_request", payload)]
    run.lab.inbox.pending.extend(scheduler.BusInbox(Consumer([*deliveries])).heads())


def _opened(run: Run, head: str) -> None:
    run.push(head, lane=head, event="pull_request", action="opened")


def _retired(run: str, head: str, dropped: int, repo: str = REPO) -> str:
    return (
        f"retired {run}: {head} is no open pull request head or plant head of {repo}, dropped {dropped} pending units"
    )


def _listed(heads: int, live: int, retired: int, dropped: int, repo: str = REPO) -> str:
    return (
        f"listed {heads} wanted heads of {repo}: {live} live pull request grades, {retired} retired, "
        f"{dropped} pending units dropped"
    )


def _answer(url: str, rows: list[Any], following: str = "", status: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response._content = json.dumps(rows).encode()  # pyright: ignore[reportPrivateUsage]
    if following:
        response.headers["Link"] = f'<{following}>; rel="next"'
    return response


def _github(answers: Callable[[str], requests.Response], asked: list[Asked]) -> Callable[..., requests.Response]:
    def get(url: str, *, headers: Mapping[str, str], timeout: float) -> requests.Response:
        asked.append((url, headers["Authorization"]))
        assert timeout > 0
        return answers(url)

    return get


def _listings(pulls: list[Any], plants: list[Any]) -> Callable[[str], requests.Response]:
    pages = {
        PULLS: _answer(PULLS, pulls[:100], PULLS_NEXT if len(pulls) > 100 else ""),
        PULLS_NEXT: _answer(PULLS_NEXT, pulls[100:]),
        PLANTS: _answer(PLANTS, plants),
    }
    return pages.__getitem__


def _readings(*pulls: list[str]) -> Callable[[str], requests.Response]:
    turns = iter(pulls)

    def answer(url: str) -> requests.Response:
        return _answer(url, [PLANT] if url == PLANTS else [{"head": {"sha": head}} for head in next(turns)])

    return answer


def _twin_grades(run: Run) -> tuple[scheduler.State, scheduler.State]:
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    _deliver(run, _pull("opened", HEAD_A, base=OTHER_BASE))
    run.tick(1010)
    one, two = (next(grade for grade in run.grades() if grade.base == base) for base in (BASE, OTHER_BASE))
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) == two.run
    return one, two


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "closed"])
def test_a_pull_request_delivery_names_its_head_branch_as_its_lane(action: str) -> None:
    assert webhooks.head_events("pull_request", _pull(action, HEAD_A)) == [
        HeadEvent("pull_request", REPO, HEAD_A, BASE, ACTOR, action, "feature")
    ]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_pull_request_from_a_fork_names_its_owner_and_branch_as_its_lane() -> None:
    events = webhooks.head_events("pull_request", _pull("opened", HEAD_A, owner="stranger"))
    assert [event.lane for event in events] == ["stranger:feature"]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_envelope_of_a_pull_request_head_carries_its_lane_beside_the_six_head_fields() -> None:
    [event] = webhooks.head_events("pull_request", _pull("opened", HEAD_A))
    assert envelope(event).data == {
        "event": "pull_request",
        "repo": REPO,
        "head": HEAD_A,
        "base": BASE,
        "actor": ACTOR,
        "action": "opened",
        "lane": "feature",
    }


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_envelope_of_a_head_that_names_no_lane_holds_the_six_head_fields_only() -> None:
    event = HeadEvent("notice", REPO, HEAD_A, BASE, ACTOR, "pushed")
    six = {"event": "notice", "repo": REPO, "head": HEAD_A, "base": BASE, "actor": ACTOR, "action": "pushed"}
    sealed = envelope(event)
    assert sealed.data == six
    assert sealed.id == hashlib.sha256(json.dumps(six).encode()).hexdigest()
    assert envelope(replace(event, lane="feature")).id != sealed.id


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_bus_inbox_reads_the_lane_key_then_the_branch_then_the_head() -> None:
    laned = envelope(HeadEvent("pull_request", REPO, HEAD_A, BASE, ACTOR, "opened", "feature"))
    both = replace(laned, data={**laned.data, "branch": "pushed"})
    branched = replace(laned, data={**laned.data, "lane": "", "branch": "pushed"})
    bare = replace(laned, data={**laned.data, "lane": ""})
    older = replace(laned, data={name: value for name, value in laned.data.items() if name != "lane"})
    deliveries = [Delivery(item) for item in (laned, both, branched, bare, older)]
    heads = scheduler.BusInbox(Consumer([*deliveries])).heads()
    assert [head.lane for head in heads] == ["feature", "feature", "pushed", HEAD_A, HEAD_A]
    assert [delivery.acked for delivery in deliveries] == [True] * 5
    assert [delivery.termed for delivery in deliveries] == [False] * 5


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_newer_head_of_a_pull_request_drops_the_pending_units_of_its_older_head(lab: Callable[..., Run]) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first,)}), HEAD_B: plan({"lint": (second,)})})
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]
    _deliver(run, _pull("synchronize", HEAD_B))
    run.tick(1100)
    assert run.grade(HEAD_A).superseded
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, second)]
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) == run.grade(HEAD_B).run


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_drops_its_pending_units_and_forgets_its_run(lab: Callable[..., Run]) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first, second)})})
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    live = run.grade(HEAD_A).run
    posts = len(run.lab.poster.posts)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, item) for item in (first, second)]
    _deliver(run, _pull("closed", HEAD_A))
    assert run.tick(1100) == [f"closed feature of {REPO}: retired {live}, dropped 2 pending units"]
    assert run.engine.queue.pending() == []
    assert run.lab.client.smembers(scheduler.LIVE) == set()
    assert run.lab.client.get(scheduler.GRADES + live) is None
    assert run.lab.client.hgetall(scheduler.ORIGINS) == {}
    assert len(run.lab.poster.posts) == posts
    assert run.tick(1200) == []


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_drops_its_waiting_head_and_starts_no_grade(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=500)
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    assert list(run.lab.client.hgetall(scheduler.WAITING)) == [PULL_LANE]
    _deliver(run, _pull("closed", HEAD_A))
    assert run.tick(1100) == [f"closed feature of {REPO}: dropped the waiting head {HEAD_A}"]
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.tick(2000) == []
    assert run.grades() == []


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_ends_its_waiting_head_and_its_live_grade_together(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})}, quiet_ms=500)
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    run.tick(1600)
    live = run.grade(HEAD_A).run
    _deliver(run, _pull("synchronize", HEAD_B))
    run.tick(1700)
    _deliver(run, _pull("closed", HEAD_B))
    closed = f"closed feature of {REPO}: dropped the waiting head {HEAD_B}, retired {live}, dropped 1 pending units"
    assert run.tick(1800) == [closed]
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.engine.queue.pending() == []
    assert run.grades() == []


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_close_that_names_the_waiting_head_alone_leaves_the_live_grade_of_another_base(
    lab: Callable[..., Run],
) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)}), HEAD_B: plan({"lint": (unit(2),)})}, quiet_ms=500)
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    run.tick(1600)
    live = run.grade(HEAD_A).run
    _deliver(run, _pull("synchronize", HEAD_B, base=OTHER_BASE))
    run.tick(1700)
    _deliver(run, _pull("closed", HEAD_B, base=OTHER_BASE))
    assert run.tick(1800) == [f"closed feature of {REPO}: dropped the waiting head {HEAD_B}"]
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert [grade.run for grade in run.grades()] == [live]
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_with_nothing_held_names_that_and_the_batch_goes_on(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_B: plan({"lint": (unit(2),)})})
    _deliver(run, _pull("closed", HEAD_A))
    _deliver(run, _pull("opened", HEAD_B, branch="other"))
    actions = run.tick(1000)
    assert actions[:2] == [f"closed feature of {REPO}: nothing held", f"received {HEAD_B} on other"]
    assert run.grade(HEAD_B).lane == "other"


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_keeps_the_pending_units_another_live_grade_grades(lab: Callable[..., Run]) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)})})
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    pulled = next(grade for grade in run.grades() if grade.tier == Priority.PULL_REQUEST)
    _deliver(run, _pull("closed", HEAD_A))
    assert f"closed feature of {REPO}: retired {pulled.run}, dropped 0 pending units" in run.tick(1100)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]
    assert [grade.tier for grade in run.grades()] == [int(Priority.QUEUE)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_ends_its_own_grade_and_leaves_the_other_pull_request_of_its_branch(
    lab: Callable[..., Run],
) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first,)}), HEAD_B: plan({"lint": (second,)})})
    one, two = _twin_grades(run)
    _deliver(run, _pull("closed", HEAD_A))
    assert f"closed feature of {REPO}: retired {one.run}, {two.run} still grades {HEAD_A}" in run.tick(1100)
    assert [grade.run for grade in run.grades()] == [two.run]
    assert run.lab.client.hkeys(scheduler.ORIGINS) == [two.run]
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) == two.run
    _deliver(run, _pull("synchronize", HEAD_B, base=OTHER_BASE))
    run.tick(1200)
    assert run.grade(HEAD_A).superseded
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, second)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_whose_grade_its_lane_names_hands_the_lane_to_the_other_pull_request(
    lab: Callable[..., Run],
) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first,)}), HEAD_B: plan({"lint": (second,)})})
    one, two = _twin_grades(run)
    _deliver(run, _pull("closed", HEAD_A, base=OTHER_BASE))
    assert f"closed feature of {REPO}: retired {two.run}, {one.run} still grades {HEAD_A}" in run.tick(1100)
    assert [grade.run for grade in run.grades()] == [one.run]
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) == one.run
    _deliver(run, _pull("synchronize", HEAD_B))
    run.tick(1200)
    assert run.grade(HEAD_A).superseded
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, second)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_close_whose_base_no_grade_of_a_shared_lane_carries_ends_nothing(lab: Callable[..., Run]) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)})})
    one, two = _twin_grades(run)
    _deliver(run, _pull("closed", HEAD_A, base=THIRD_BASE))
    assert f"closed feature of {REPO}: nothing held" in run.tick(1100)
    assert sorted(grade.run for grade in run.grades()) == sorted([one.run, two.run])
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) == two.run


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_with_no_grade_of_its_own_ends_nothing_of_its_branch(lab: Callable[..., Run]) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)})}, quiet_ms=500)
    _deliver(run, _pull("opened", HEAD_A))
    _deliver(run, _pull("opened", HEAD_A, base=OTHER_BASE))
    run.tick(1000)
    run.tick(1600)
    live = run.grade(HEAD_A)
    assert [grade.base for grade in run.grades()] == [BASE]
    _deliver(run, _pull("closed", HEAD_A, base=OTHER_BASE))
    assert f"closed feature of {REPO}: nothing held" in run.tick(1700)
    assert [grade.run for grade in run.grades()] == [live.run]
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]
    _deliver(run, _pull("closed", HEAD_A))
    assert f"closed feature of {REPO}: retired {live.run}, dropped 1 pending units" in run.tick(1800)


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_close_leaves_the_waiting_head_of_another_base(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})}, quiet_ms=500)
    _deliver(run, _pull("opened", HEAD_B, base=OTHER_BASE))
    run.tick(1000)
    _deliver(run, _pull("closed", HEAD_B))
    assert run.tick(1100) == [f"closed feature of {REPO}: nothing held"]
    run.tick(1600)
    live = run.grade(HEAD_B).run
    _deliver(run, _pull("synchronize", HEAD_A))
    run.tick(1700)
    _deliver(run, _pull("closed", HEAD_A, base=OTHER_BASE))
    assert f"closed feature of {REPO}: retired {live}, dropped 1 pending units" in run.tick(1800)
    assert list(run.lab.client.hgetall(scheduler.WAITING)) == [PULL_LANE]
    assert run.lab.client.smembers(scheduler.LIVE) == set()


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_three_pull_requests_of_one_branch_end_one_grade_at_each_close(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    one, two = _twin_grades(run)
    _deliver(run, _pull("opened", HEAD_A, base=THIRD_BASE))
    run.tick(1020)
    three = next(grade for grade in run.grades() if grade.base == THIRD_BASE)
    stays, other = sorted([one, two], key=lambda grade: grade.run)
    _deliver(run, _pull("closed", HEAD_A, base=THIRD_BASE))
    assert f"closed feature of {REPO}: retired {three.run}, {stays.run} still grades {HEAD_A}" in run.tick(1100)
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) == stays.run
    _deliver(run, _pull("closed", HEAD_A, base=other.base))
    assert f"closed feature of {REPO}: retired {other.run}, {stays.run} still grades {HEAD_A}" in run.tick(1200)
    assert [grade.run for grade in run.grades()] == [stays.run]
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) == stays.run
    _deliver(run, _pull("closed", HEAD_A, base=stays.base))
    assert f"closed feature of {REPO}: retired {stays.run}, dropped 1 pending units" in run.tick(1300)
    assert run.grades() == []
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) == stays.run
    _deliver(run, _pull("closed", HEAD_A, base=stays.base))
    assert run.tick(1400) == [f"closed feature of {REPO}: nothing held"]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_keeps_the_waiting_head_of_a_branch_another_pull_request_holds(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})}, quiet_ms=500)
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    run.tick(1600)
    _deliver(run, _pull("opened", HEAD_A, base=OTHER_BASE))
    run.tick(1700)
    run.tick(2300)
    one, two = (next(grade for grade in run.grades() if grade.base == base) for base in (BASE, OTHER_BASE))
    _deliver(run, _pull("synchronize", HEAD_B, base=OTHER_BASE))
    run.tick(2400)
    _deliver(run, _pull("closed", HEAD_B))
    assert f"closed feature of {REPO}: retired {one.run}, {two.run} still grades {HEAD_A}" in run.tick(2500)
    assert list(run.lab.client.hgetall(scheduler.WAITING)) == [PULL_LANE]
    run.tick(3000)
    assert run.grade(HEAD_B).base == OTHER_BASE
    assert run.grade(HEAD_A).superseded


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_ends_every_grade_of_its_head_and_base_in_its_lane(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    earlier = run.grade(HEAD_A).run
    run.push(HEAD_A, event="pull_request", action=intake.RERUN)
    run.tick(1010)
    again = run.lab.client.get(scheduler.LANES + PULL_LANE)
    assert again != earlier and sorted(grade.run for grade in run.grades()) == sorted([earlier, again])
    _deliver(run, _pull("closed", HEAD_A))
    assert f"closed feature of {REPO}: retired {earlier}, {again}, dropped 1 pending units" in run.tick(1100)
    assert run.grades() == []
    assert run.engine.queue.pending() == []


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_grade_of_the_same_head_in_another_lane_is_no_other_pull_request_of_the_branch(
    lab: Callable[..., Run],
) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)})})
    _deliver(run, _pull("opened", HEAD_A, branch="other", base=OTHER_BASE))
    run.tick(1000)
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1010)
    other, mine = (next(grade for grade in run.grades() if grade.lane == lane) for lane in ("other", "feature"))
    _deliver(run, _pull("closed", HEAD_A))
    assert f"closed feature of {REPO}: retired {mine.run}, dropped 0 pending units" in run.tick(1100)
    assert [grade.run for grade in run.grades()] == [other.run]
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_whose_live_grade_lost_its_document_is_still_forgotten(
    lab: Callable[..., Run],
) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    live = run.grade(HEAD_A).run
    run.lab.client.delete(scheduler.GRADES + live)
    _deliver(run, _pull("closed", HEAD_A))
    assert f"closed feature of {REPO}: retired {live}, dropped 1 pending units" in run.tick(1100)
    assert run.lab.client.smembers(scheduler.LIVE) == set()


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_leaves_a_grade_pushed_to_a_grade_ref(lab: Callable[..., Run]) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)})})
    run.push(HEAD_A, lane="feature", event="notice", action="pushed", ref=f"{scheduler.GRADE_REFS}feature")
    run.tick(1000)
    live = run.grade(HEAD_A).run
    _deliver(run, _pull("closed", HEAD_A))
    assert f"closed feature of {REPO}: nothing held" in run.tick(1100)
    assert run.lab.client.smembers(scheduler.LIVE) == {live}
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_close_ends_the_live_grade_of_its_base_when_the_lane_names_a_grade_that_finished(
    lab: Callable[..., Run],
) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first,)})})
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    live = run.grade(HEAD_A).run
    run.lab.expander.plans[HEAD_A] = plan({"lint": (second,)})
    run.cache(second)
    _deliver(run, _pull("opened", HEAD_A, base=OTHER_BASE))
    run.tick(1010)
    assert run.lab.client.smembers(scheduler.LIVE) == {live}
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) != live
    _deliver(run, _pull("closed", HEAD_A))
    assert run.tick(1100) == [f"closed feature of {REPO}: retired {live}, dropped 1 pending units"]
    assert run.lab.client.smembers(scheduler.LIVE) == set()
    assert run.engine.queue.pending() == []


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_close_ends_the_live_grade_of_its_base_beside_a_grade_pushed_to_a_grade_ref_that_keeps_the_lane(
    lab: Callable[..., Run],
) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first,)})})
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    plain = run.grade(HEAD_A).run
    run.lab.expander.plans[HEAD_A] = plan({"lint": (second,)})
    notice = HeadEvent("notice", REPO, HEAD_A, OTHER_BASE, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(notice, "feature", f"{scheduler.GRADE_REFS}feature"))
    run.tick(1010)
    pushed = run.lab.client.get(scheduler.LANES + PULL_LANE)
    assert run.lab.client.smembers(scheduler.LIVE) == {plain, pushed}
    _deliver(run, _pull("closed", HEAD_A))
    assert run.tick(1100) == [f"closed feature of {REPO}: retired {plain}, dropped 0 pending units"]
    assert run.lab.client.smembers(scheduler.LIVE) == {pushed}
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) == pushed
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first), scheduler.job_id(HEAD_A, second)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_leaves_a_waiting_head_pushed_to_a_grade_ref(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})}, quiet_ms=500)
    run.push(HEAD_A, lane="feature", event="notice", action="pushed", ref=f"{scheduler.GRADE_REFS}feature")
    run.tick(1000)
    _deliver(run, _pull("closed", HEAD_A))
    assert run.tick(1100) == [f"closed feature of {REPO}: nothing held"]
    assert list(run.lab.client.hgetall(scheduler.WAITING)) == [PULL_LANE]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_closed_pull_request_leaves_the_same_lane_of_the_hotfix_class(lab: Callable[..., Run]) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)})})
    run.lab.fixes.add("feature")
    _deliver(run, _pull("opened", HEAD_A))
    run.tick(1000)
    assert run.grade(HEAD_A).tier == Priority.HOTFIX
    _deliver(run, _pull("closed", HEAD_A))
    assert f"closed feature of {REPO}: nothing held" in run.tick(1100)
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_head_already_graded_takes_its_lane_and_supersedes_the_lanes_older_head(lab: Callable[..., Run]) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first,)}), HEAD_B: plan({"lint": (second,)})})
    _deliver(run, _pull("opened", HEAD_A))
    run.push(HEAD_B, lane=HEAD_B, event="notice", action="pushed")
    run.tick(1000)
    newer = run.grade(HEAD_B).run
    _deliver(run, _pull("synchronize", HEAD_B))
    assert f"already graded {newer}" in run.tick(1100)
    assert run.grade(HEAD_A).superseded
    assert run.engine.queue.pending() == [scheduler.job_id(HEAD_B, second)]
    assert run.lab.client.get(scheduler.LANES + PULL_LANE) == newer


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_first_pass_retires_the_pull_request_grades_whose_head_nothing_wants(lab: Callable[..., Run]) -> None:
    first, second = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (first,)}), HEAD_B: plan({"lint": (second,)})})
    _opened(run, HEAD_A)
    _opened(run, HEAD_B)
    run.tick(1000)
    stale, current = run.grade(HEAD_A).run, run.grade(HEAD_B).run
    _wanted(run).heads[REPO] = {HEAD_B, BASE}
    again = _restarted(run)
    assert again.tick(1100)[:2] == [_retired(stale, HEAD_A, 1), _listed(2, 2, 1, 1)]
    assert again.engine.queue.pending() == [scheduler.job_id(HEAD_B, second)]
    assert again.lab.client.smembers(scheduler.LIVE) == {current}
    assert again.lab.client.get(scheduler.GRADES + stale) is None
    assert list(again.lab.client.hgetall(scheduler.ORIGINS)) == [current]
    assert _wanted(run).asked == [REPO]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_stale_grades_of_one_pass_are_retired_in_run_order_and_counted(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1), unit(3))}), HEAD_B: plan({"lint": (unit(2),)})})
    _opened(run, HEAD_A)
    _opened(run, HEAD_B)
    run.tick(1000)
    heads = {grade.run: grade.head for grade in run.grades()}
    units = {HEAD_A: 2, HEAD_B: 1}
    again = _restarted(run)
    retired = [_retired(name, heads[name], units[heads[name]]) for name in sorted(heads)]
    assert again.tick(1100) == [*retired, _listed(0, 2, 2, 3)]
    assert again.engine.queue.pending() == []
    assert again.grades() == []


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_stale_head_retire_runs_on_the_first_pass_of_a_scheduler_only(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)})})
    _opened(run, HEAD_A)
    run.tick(1000)
    live = run.grade(HEAD_A).run
    _wanted(run).heads[REPO] = {HEAD_A}
    again = _restarted(run)
    assert again.tick(1100)[:1] == [_listed(1, 1, 0, 0)]
    _wanted(run).heads[REPO] = set()
    assert again.tick(1200) == []
    assert again.lab.client.smembers(scheduler.LIVE) == {live}
    assert _wanted(run).asked == [REPO]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_listing_that_fails_retires_nothing_and_is_named_once(lab: Callable[..., Run]) -> None:
    first, error = unit(1), OSError("github is down")
    run = lab({HEAD_A: plan({"lint": (first,)})})
    _opened(run, HEAD_A)
    run.tick(1000)
    live = run.grade(HEAD_A).run
    _wanted(run).failing[REPO] = error
    again = _restarted(run)
    assert again.tick(1100)[:1] == [f"cannot list the wanted heads of {REPO}: {error}"]
    assert again.tick(1200) == []
    assert again.lab.client.smembers(scheduler.LIVE) == {live}
    assert again.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]
    assert _wanted(run).asked == [REPO]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_each_repository_is_listed_once_and_a_failed_one_keeps_its_grades(lab: Callable[..., Run]) -> None:
    run = lab({HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)})})
    _opened(run, HEAD_A)
    elsewhere = HeadEvent("pull_request", OTHER_REPO, HEAD_B, BASE, ACTOR, "opened")
    run.lab.inbox.pending.append(scheduler.Head(elsewhere, HEAD_B, ""))
    run.tick(1000)
    stale, kept = run.grade(HEAD_A).run, run.grade(HEAD_B).run
    _wanted(run).failing[OTHER_REPO] = OSError("github is down")
    again = _restarted(run)
    assert again.tick(1100)[:3] == [
        f"cannot list the wanted heads of {OTHER_REPO}: github is down",
        _retired(stale, HEAD_A, 1),
        _listed(0, 1, 1, 1),
    ]
    assert again.lab.client.smembers(scheduler.LIVE) == {kept}
    assert _wanted(run).asked == [REPO, OTHER_REPO]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_first_pass_asks_nothing_for_grades_of_other_classes_or_pushed_to_a_grade_ref(
    lab: Callable[..., Run],
) -> None:
    plans = {HEAD_A: plan({"lint": (unit(1),)}), HEAD_B: plan({"lint": (unit(2),)}), HEAD_C: plan({"lint": (unit(3),)})}
    run = lab(plans)
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    run.push(HEAD_B, lane="feature", event="notice", action="pushed", ref=f"{scheduler.GRADE_REFS}feature")
    run.lab.fixes.add(HEAD_C)
    _opened(run, HEAD_C)
    run.tick(1000)
    live = run.lab.client.smembers(scheduler.LIVE)
    assert len(live) == 3
    again = _restarted(run)
    again.tick(1100)
    assert again.lab.client.smembers(scheduler.LIVE) == live
    assert _wanted(run).asked == []


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_first_pass_keeps_the_pending_units_another_live_grade_grades(lab: Callable[..., Run]) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)})})
    run.push(HEAD_A, lane=HEAD_A, event="merge_group", action="checks_requested")
    _opened(run, HEAD_A)
    run.tick(1000)
    pulled = next(grade for grade in run.grades() if grade.tier == Priority.PULL_REQUEST)
    again = _restarted(run)
    assert again.tick(1100)[:2] == [_retired(pulled.run, HEAD_A, 0), _listed(0, 1, 1, 0)]
    assert again.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]
    assert [grade.tier for grade in again.grades()] == [int(Priority.QUEUE)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_scheduler_without_a_listing_retires_nothing(lab: Callable[..., Run]) -> None:
    first = unit(1)
    run = lab({HEAD_A: plan({"lint": (first,)})}, listed=False)
    _opened(run, HEAD_A)
    run.tick(1000)
    again = _restarted(run)
    assert again.tick(1100) == []
    assert again.engine.queue.pending() == [scheduler.job_id(HEAD_A, first)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_listing_reads_every_page_of_open_pull_requests_and_of_plant_branches_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulls = [{"head": {"sha": f"{number:040x}"}} for number in range(101)]
    asked: list[Asked] = []
    monkeypatch.setattr(requests, "get", _github(_listings(pulls, [PLANT]), asked))
    heads = stale_heads.OpenHeads(Token)(REPO)
    assert heads == frozenset({*(str(pull["head"]["sha"]) for pull in pulls), HEAD_B})
    assert asked == [(PULLS, BEARER), (PULLS_NEXT, BEARER), (PLANTS, BEARER)] * 2


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_listings_are_read_again_until_two_readings_in_a_row_name_the_same_heads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[Asked] = []
    monkeypatch.setattr(requests, "get", _github(_readings([HEAD_A], [HEAD_A, HEAD_C], [HEAD_A, HEAD_C]), asked))
    assert stale_heads.OpenHeads(Token)(REPO) == frozenset({HEAD_A, HEAD_B, HEAD_C})
    assert [url for url, _ in asked] == [PULLS, PLANTS] * 3


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_reading_that_repeats_an_earlier_one_but_not_the_last_settles_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[Asked] = []
    monkeypatch.setattr(requests, "get", _github(_readings([HEAD_A], [HEAD_C], [HEAD_A], [HEAD_A]), asked))
    assert stale_heads.OpenHeads(Token)(REPO) == frozenset({HEAD_A, HEAD_B})
    assert [url for url, _ in asked] == [PULLS, PLANTS] * 4


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_listings_settle_on_the_last_reading_they_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    heads = [[f"{number:040x}"] for number in range(4)]
    asked: list[Asked] = []
    monkeypatch.setattr(requests, "get", _github(_readings(*heads, heads[-1]), asked))
    assert stale_heads.OpenHeads(Token)(REPO) == frozenset({heads[-1][0], HEAD_B})
    assert [url for url, _ in asked] == [PULLS, PLANTS] * 5


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_listings_that_never_repeat_raise_instead_of_naming_heads(monkeypatch: pytest.MonkeyPatch) -> None:
    heads = [[f"{number:040x}"] for number in range(6)]
    asked: list[Asked] = []
    monkeypatch.setattr(requests, "get", _github(_readings(*heads), asked))
    with pytest.raises(OSError, match=f"the listings of {SLUG} changed through 5 readings"):
        stale_heads.OpenHeads(Token)(REPO)
    assert [url for url, _ in asked] == [PULLS, PLANTS] * 5


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_next_link_outside_the_api_raises_and_sends_the_token_nowhere_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[Asked] = []
    monkeypatch.setattr(requests, "get", _github(lambda url: _answer(url, [], ELSEWHERE), asked))
    with pytest.raises(OSError, match=f"pulls of {SLUG} continues outside {API}"):
        stale_heads.OpenHeads(Token)(REPO)
    assert asked == [(PULLS, BEARER)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_listing_of_a_repository_url_asks_its_owner_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Asked] = []
    monkeypatch.setattr(requests, "get", _github(_listings([], []), asked))
    assert stale_heads.OpenHeads(Token)(f"https://github.com/{SLUG}") == frozenset()
    assert [url for url, _ in asked] == [PULLS, PLANTS] * 2


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
@pytest.mark.parametrize("refused", [PULLS, PLANTS])
def test_a_listing_github_refuses_raises_instead_of_naming_fewer_heads(
    monkeypatch: pytest.MonkeyPatch, refused: str
) -> None:
    asked: list[Asked] = []
    monkeypatch.setattr(
        requests, "get", _github(lambda url: _answer(url, [], status=500 if url == refused else 200), asked)
    )
    with pytest.raises(requests.HTTPError):
        stale_heads.OpenHeads(Token)(REPO)


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_listing_longer_than_its_page_cap_raises_instead_of_naming_fewer_heads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[Asked] = []
    endless = _github(lambda url: _answer(url, [{"head": {"sha": HEAD_A}}], PULLS_NEXT), asked)
    monkeypatch.setattr(requests, "get", endless)
    with pytest.raises(OSError, match=f"pulls of {SLUG} holds more than {stale_heads.PAGES} pages"):
        stale_heads.OpenHeads(Token)(REPO)
    assert [url for url, _ in asked] == [PULLS, *[PULLS_NEXT] * (stale_heads.PAGES - 1)]


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_listing_with_a_row_that_names_no_head_raises_an_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Asked] = []
    monkeypatch.setattr(requests, "get", _github(_listings([{"number": 7}], []), asked))
    with pytest.raises(OSError, match=f"the listings of {SLUG} cannot be read: KeyError\\('head'\\)"):
        stale_heads.OpenHeads(Token)(REPO)


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_listing_whose_token_is_refused_raises_an_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class Refused:
        slug: str

        def get_token(self) -> str:
            raise GitAuthRefused(f"no token for {self.slug}")

    monkeypatch.setattr(requests, "get", _github(_listings([], []), []))
    with pytest.raises(OSError, match=f"cannot be read: GitAuthRefused\\('no token for {SLUG}'\\)"):
        stale_heads.OpenHeads(Refused)(REPO)


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_a_listing_without_the_lab_app_settings_raises_an_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in APP_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(OSError, match="cannot be read: KeyError\\('GITHUB_APP_ID'\\)"):
        stale_heads.OpenHeads()(REPO)


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_listing_asks_the_lab_app_for_read_access_to_pull_requests_and_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in APP_SETTINGS.items():
        monkeypatch.setenv(name, value)
    auth = stale_heads.app(SLUG)
    assert isinstance(auth, GitHubAppAuth)
    assert auth.project == GitHubAppAuth(f"github.com/{SLUG}").project
    assert auth.permissions == {"contents": "read", "pull_requests": "read"}


@pytest.mark.feature("lab-retire-stale-pull-request-heads")
def test_the_service_scheduler_lists_wanted_heads_through_the_lab_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler.MirrorExpander, "trusted", lambda self, repo: frozenset({repo}))
    monkeypatch.setattr(scheduler.MirrorExpander, "retries", lambda self, repo: Policy())
    monkeypatch.setattr(scheduler, "worker_pods", lambda environ: FakeKube())
    environ = {
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: "repository",
    }
    client = fake_redis()
    spending = budget.Budget(client, lambda: 0, budget.Policy(100.0, {}))
    built = scheduler.build(environ, client, LocalObjects(tmp_path / "objects"), Consumer([]), spending)
    wanted = built.ports.wanted
    assert isinstance(wanted, stale_heads.OpenHeads) and wanted.auth is stale_heads.app
