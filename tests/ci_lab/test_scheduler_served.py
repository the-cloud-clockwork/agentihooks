"""The lab scheduler grades only the repositories it serves.

A scheduler holds its served repositories by owner and name. Intake ignores a head of any other repository before
it checks the actor, and the service reads the set from the environment: the listed repositories, or else the one
repository it is set up for.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.ci_lab import budget, contract, intake, scheduler
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.webhooks import NOTICE, PLANT, HeadEvent, envelope
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    HEAD_A,
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

GITHUB = "https://github.com/the-cloud-clockwork/agentihooks"
OTHER = "https://github.com/the-cloud-clockwork/antoncore"
OTHER_SLUG = "the-cloud-clockwork/antoncore"
IGNORED = f"ignored {HEAD_A}: {OTHER} is not a served repository"
RECEIVED = f"received {HEAD_A} on dev"


@dataclass
class Based(Expander):
    based: list[str] = field(default_factory=list[str])

    def merge_base(self, repo: str, head: str) -> str:
        self.based.append(head)
        return super().merge_base(repo, head)


@pytest.fixture
def lab(tmp_path: Path) -> Callable[[frozenset[str] | None], Run]:
    def make(served: frozenset[str] | None) -> Run:
        plans = {HEAD_A: plan({"lint": (unit(1),)})}
        state = Lab(fake_redis(), LocalObjects(tmp_path / "objects"), Inbox(), Based(plans))
        ports = scheduler.Ports(state.inbox, state.expander, state.checks, state.hotfixes, state.kube)
        settings = scheduler.Settings(frozenset({ACTOR}), 0, Policy(), served)
        return Run(state, scheduler.Scheduler(state.client, state.objects, ports, settings, None))

    return make


def _head(
    run: Run, repo: str, event: str = "push", action: str = "pushed", lane: str = "dev", actor: str = ACTOR
) -> None:
    run.lab.inbox.pending.append(scheduler.Head(HeadEvent(event, repo, HEAD_A, BASE, actor, action), lane, ""))


def _trusted(self: scheduler.MirrorExpander, repo: str) -> frozenset[str]:
    return frozenset({ACTOR})


def _retries(self: scheduler.MirrorExpander, repo: str) -> Policy:
    return Policy()


def _pods(found: Mapping[str, str]) -> FakeKube:
    return FakeKube()


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., scheduler.Scheduler]:
    monkeypatch.setattr(scheduler.MirrorExpander, "trusted", _trusted)
    monkeypatch.setattr(scheduler.MirrorExpander, "retries", _retries)
    monkeypatch.setattr(scheduler, "worker_pods", _pods)
    environ = {
        contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: f"{GITHUB}.git",
    }

    def build(extra: Mapping[str, str], events: tuple[HeadEvent, ...] = ()) -> scheduler.Scheduler:
        client = fake_redis()
        spending = budget.Budget(client, lambda: 0, budget.Policy(100.0, {}))
        consumer = Consumer([Delivery(envelope(event)) for event in events])
        return scheduler.build({**environ, **extra}, client, LocalObjects(tmp_path / "objects"), consumer, spending)

    return build


@pytest.mark.feature("lab-serves-listed-repositories")
@pytest.mark.parametrize(
    "repo",
    [
        GITHUB,
        f"{GITHUB}.git",
        f"{GITHUB}/",
        f"{GITHUB}.git/",
        f"{GITHUB}.GIT",
        "https://github.com/The-Cloud-Clockwork/AgentiHooks",
        REPO,
    ],
)
def test_the_slug_of_a_repository_is_its_owner_and_name_whatever_form_names_it(repo: str) -> None:
    assert intake.slug(repo) == SLUG


@pytest.mark.feature("lab-serves-listed-repositories")
def test_the_slug_of_a_repository_loses_only_the_slashes_at_its_ends() -> None:
    assert intake.slug("https://github.com/Xenon/WidgetsX/") == "xenon/widgetsx"


@pytest.mark.feature("lab-serves-listed-repositories")
@pytest.mark.parametrize(
    ("listed", "expected"),
    [
        ("", {SLUG}),
        (" \n", {SLUG}),
        (f"{OTHER}.git", {OTHER_SLUG}),
        (f"{GITHUB}, {OTHER}", {SLUG, OTHER_SLUG}),
        (f"{GITHUB}\n{OTHER},https://github.com/acme/widgets", {SLUG, OTHER_SLUG, "acme/widgets"}),
    ],
)
def test_the_served_set_is_the_listed_repositories_or_else_the_one_the_service_is_set_up_for(
    listed: str, expected: set[str]
) -> None:
    assert intake.served(listed, f"{GITHUB}.git") == expected


@pytest.mark.feature("lab-serves-listed-repositories")
def test_a_set_serves_the_repositories_it_names_and_no_set_serves_every_repository() -> None:
    assert intake.serves(frozenset({SLUG}), f"{GITHUB}.git")
    assert not intake.serves(frozenset({SLUG}), OTHER)
    assert not intake.serves(frozenset(), GITHUB)
    assert intake.serves(None, OTHER)


@pytest.mark.feature("lab-serves-listed-repositories")
@pytest.mark.parametrize(
    ("event", "action", "lane"),
    [
        ("push", "pushed", "dev"),
        ("pull_request", "opened", "feature"),
        ("pull_request", "closed", "feature"),
        ("merge_group", "checks_requested", HEAD_A),
        ("check_run", "rerequested", HEAD_A),
        (NOTICE, "pushed", "feature"),
        (PLANT, "pushed", HEAD_A),
    ],
)
def test_a_head_of_a_repository_that_is_not_served_is_ignored_whatever_asks_for_it(
    lab: Callable[[frozenset[str] | None], Run], event: str, action: str, lane: str
) -> None:
    run = lab(frozenset({SLUG}))
    _head(run, OTHER, event, action, lane)
    assert run.tick(1000) == [IGNORED]
    assert run.lab.client.hgetall(scheduler.WAITING) == {}
    assert run.grades() == []
    expander = run.lab.expander
    assert isinstance(expander, Based) and (expander.seen, expander.based) == ([], [])
    assert (run.lab.repos, run.lab.poster.posts) == ([], [])


@pytest.mark.feature("lab-serves-listed-repositories")
def test_a_head_of_a_repository_that_is_not_served_is_ignored_before_its_actor_is_checked(
    lab: Callable[[frozenset[str] | None], Run],
) -> None:
    run = lab(frozenset({SLUG}))
    _head(run, OTHER, actor="stranger")
    assert run.tick(1000) == [IGNORED]


@pytest.mark.feature("lab-serves-listed-repositories")
def test_a_head_of_a_served_repository_is_held_in_the_pass_that_ignores_a_head_before_it(
    lab: Callable[[frozenset[str] | None], Run],
) -> None:
    run = lab(frozenset({SLUG}))
    _head(run, OTHER)
    _head(run, REPO)
    assert run.tick(1000)[:2] == [IGNORED, RECEIVED]
    assert [(grade.repo, grade.head) for grade in run.grades()] == [(REPO, HEAD_A)]


@pytest.mark.feature("lab-serves-listed-repositories")
@pytest.mark.parametrize("named", [REPO, GITHUB, f"{GITHUB}.git", "https://github.com/The-Cloud-Clockwork/agentihooks"])
def test_a_head_of_a_served_repository_is_held_whatever_form_names_it(
    lab: Callable[[frozenset[str] | None], Run], named: str
) -> None:
    run = lab(frozenset({SLUG}))
    _head(run, named)
    assert run.tick(1000)[0] == RECEIVED
    assert [(grade.repo, grade.head) for grade in run.grades()] == [(named, HEAD_A)]


@pytest.mark.feature("lab-serves-listed-repositories")
def test_a_scheduler_with_no_set_of_served_repositories_holds_a_head_of_any_repository(
    lab: Callable[[frozenset[str] | None], Run],
) -> None:
    run = lab(None)
    _head(run, OTHER)
    assert run.tick(1000)[0] == RECEIVED
    assert [(grade.repo, grade.head) for grade in run.grades()] == [(OTHER, HEAD_A)]


@pytest.mark.feature("lab-serves-listed-repositories")
def test_the_service_scheduler_serves_the_repository_it_is_set_up_for_unless_the_environment_lists_others(
    service: Callable[..., scheduler.Scheduler],
) -> None:
    assert service({}).settings.served == {SLUG}
    assert service({contract.SERVED_REPOSITORIES: ""}).settings.served == {SLUG}
    assert service({scheduler.REPOSITORY: OTHER}).settings.served == {OTHER_SLUG}
    listed = f"{OTHER}, https://github.com/acme/widgets.git"
    assert service({contract.SERVED_REPOSITORIES: listed}).settings.served == {OTHER_SLUG, "acme/widgets"}


@pytest.mark.feature("lab-serves-listed-repositories")
def test_the_service_scheduler_ignores_a_delivered_head_of_another_repository(
    service: Callable[..., scheduler.Scheduler],
) -> None:
    built = service({}, (HeadEvent("push", OTHER, HEAD_A, BASE, ACTOR, "pushed", "dev"),))
    assert built.tick(1000) == [IGNORED]
    assert built.client.hgetall(scheduler.WAITING) == {}
