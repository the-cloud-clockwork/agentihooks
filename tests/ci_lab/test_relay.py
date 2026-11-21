"""Tests for the lab relay: pending grade ref notices, a green grade's push, check runs and draft pull request, a red
grade held with its failed units, and the GitHub adapters' scoped tokens."""

import base64
import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from functools import partial
from io import StringIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
import requests

from scripts import github_app
from scripts.ci_lab import relay
from scripts.ci_lab import scheduler as scheduler_module
from scripts.ci_lab.checks import API, Checks, Output, name
from scripts.ci_lab.evidence import head_prefix
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.outcomes import FAILED, PASSED
from scripts.ci_lab.remote import post_receive
from scripts.ci_lab.scheduler import Flags, Scheduler, Settings, Slot, State
from scripts.ci_lab.units import Unit
from scripts.ci_lab.worktrees import Lab, Mirror
from scripts.events.bus import Consumer
from scripts.events.config import BusConfig
from scripts.events.memory import open_bus

REPO = "https://github.com/acme/widgets"
SLUG = "acme/widgets"
HEAD = "a" * 40
OTHER = "d" * 40
BASE = "b" * 40
GRADER = "c" * 40
LAB = "http://lab.example:8800"
BRANCH = "engineer-1"
REF = "refs/grade/t1"
PULL = "https://github.com/acme/widgets/pull/9"
HEADERS = {
    "Authorization": "Bearer fixture",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def fake_redis() -> Any:
    """Return a fresh fake Redis client."""
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def grade(conclusion: str, outcome: str = PASSED, superseded: bool = False, run: str = "run-7") -> State:
    """Return a closed grade of HEAD with a lint gate answered by the store and a unit gate run as a job."""
    units = {
        "static:ruff-1": Slot("static", "ruff-1", [], outcome=PASSED),
        "tests:batch-2": Slot("tests", "batch-2", ["tests/test_a.py"], job="job-2", outcome=outcome),
    }
    unit = "success" if outcome == PASSED else "failure"
    return State(
        run=run,
        repo=REPO,
        head=HEAD,
        base=BASE,
        tier=1,
        lane=BRANCH,
        tree="e" * 40,
        grader=GRADER,
        flags=Flags(),
        outputs={},
        skipped=[],
        gates={"lint": ["static:ruff-1"], "unit": ["tests:batch-2"]},
        units=units,
        superseded=superseded,
        conclusion=conclusion,
        posted={"lint": ["completed", "success"], "unit": ["completed", unit], "required": ["completed", conclusion]},
    )


class Calls:
    """Fake relay ports that keep every call in the order it came."""

    def __init__(self) -> None:
        self.log: list[tuple[Any, ...]] = []
        self.outputs: list[Output] = []
        self.posted: dict[tuple[str, str, str], str] = {}
        self.saved: list[dict[str, int]] = []

    def pusher(self, repo: str) -> "Calls":
        """Return the pusher of a repository."""
        self.log.append(("pusher", repo))
        return self

    def checks(self, slug: str) -> "Calls":
        """Return the check runs poster of a repository."""
        self.log.append(("checks", slug))
        return self

    def pulls(self, slug: str) -> "Calls":
        """Return the pull requests of a repository."""
        self.log.append(("pulls", slug))
        return self

    def push(self, sha: str, branch: str) -> None:
        """Keep the push."""
        self.log.append(("push", sha, branch))

    def seed(self, head: str, gate: str, run: str, run_id: int, status: str, shadow: bool) -> None:
        """Keep the seeded check run's status as posted."""
        self.posted[(head, name(gate, shadow), run)] = status

    def status(self, key: tuple[str, str, str]) -> str | None:
        """Return the last status posted for a check run, or None before its first post."""
        return self.posted.get(key)

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        """Keep the check run post, its output and its last status."""
        self.log.append(("post", head, gate, status, conclusion, shadow))
        self.outputs.append(output)
        self.posted[(head, name(gate, shadow), output.run)] = status
        return len(self.outputs)

    def open(self, branch: str, title: str, body: str) -> str:
        """Keep the pull request call and answer that it opened one."""
        self.log.append(("open", branch, title, body))
        return f"opened {PULL}"

    def save(self, state: State) -> None:
        """Keep the check run ids the saved state holds."""
        self.saved.append(dict(state.runs))

    def ports(self) -> relay.Ports:
        """Return the relay ports over this recorder."""
        return relay.Ports(self.pusher, self.checks, self.pulls, self.save)


class Refusing(Calls):
    """Fake relay ports whose push GitHub refuses."""

    def push(self, sha: str, branch: str) -> None:
        """Refuse the push."""
        self.log.append(("push", sha, branch))
        raise relay.RelayRefused("git push answered 1: non fast forward")


class Notices:
    """A notices inbox that hands out its batches in order, then nothing."""

    def __init__(self, *batches: list[relay.Pending]) -> None:
        self.batches = list(batches)

    def pending(self) -> list[relay.Pending]:
        """Return the next batch."""
        return self.batches.pop(0) if self.batches else []


class Token:
    """A token source that always hands out the fixture token."""

    def get_token(self) -> str:
        """Return the fixture token."""
        return "fixture"


def answer(status: int, body: object = None) -> Mock:
    """Return a mocked HTTP response."""
    return Mock(status_code=status, json=Mock(return_value=body), text="", headers={})


def notice(ref: str, branch: str) -> dict[str, str]:
    """Return a lab head notice's data as the lab remote publishes it."""
    return {
        "event": "notice",
        "repo": REPO,
        "head": HEAD,
        "base": BASE,
        "actor": "agenticore-app[bot]",
        "action": "pushed",
        "ref": ref,
        "branch": branch,
    }


def test_a_grade_ref_notice_with_a_branch_is_pending() -> None:
    assert relay.pending(notice(REF, BRANCH)) == relay.Pending(REPO, HEAD, REF, BRANCH)


@pytest.mark.parametrize(
    ("ref", "branch"),
    [
        (REF, ""),
        ("refs/heads/engineer-1", BRANCH),
        ("refs/gradex/t1", BRANCH),
        ("", BRANCH),
        (REF, "dev"),
        (REF, "main"),
        (REF, "master"),
        (REF, "v1"),
    ],
)
def test_a_notice_of_another_ref_without_a_branch_or_to_a_protected_branch_is_not_pending(
    ref: str, branch: str
) -> None:
    assert relay.pending(notice(ref, branch)) is None


def test_a_notice_without_a_ref_is_not_pending() -> None:
    data = notice(REF, BRANCH)
    del data["ref"]
    assert relay.pending(data) is None


def test_a_green_grade_pushes_then_posts_every_check_run_completed_then_opens_the_draft_pull_request() -> None:
    calls = Calls()
    actions = relay.relay(grade("success"), BRANCH, calls.ports(), LAB)
    body = f"Relayed by the CI lab from grade run-7: {LAB}/grades/{HEAD}"
    assert calls.log == [
        ("pusher", REPO),
        ("push", HEAD, BRANCH),
        ("checks", SLUG),
        ("post", HEAD, "lint", "completed", "success", False),
        ("post", HEAD, "unit", "completed", "success", False),
        ("post", HEAD, "required", "completed", "success", False),
        ("pulls", SLUG),
        ("open", BRANCH, BRANCH, body),
    ]
    assert actions == [
        f"pushed {HEAD} to {BRANCH}",
        f"posted lab/lint success on {HEAD}",
        f"posted lab/unit success on {HEAD}",
        f"posted lab/required success on {HEAD}",
        f"opened {PULL}",
    ]
    assert calls.saved == [
        {"lab/lint": 1},
        {"lab/lint": 1, "lab/unit": 2},
        {"lab/lint": 1, "lab/unit": 2, "lab/required": 3},
    ]


@pytest.mark.feature("lab-relay-check-run-restart")
def test_a_relayed_grade_holding_the_scheduler_s_own_check_run_ids_still_posts_every_lab_check_run() -> None:
    calls = Calls()
    state = grade("success")
    state.runs = {"lint": 41, "unit": 42, "required": 43}
    relay.relay(state, BRANCH, calls.ports(), LAB)
    assert [call[2] for call in calls.log if call[0] == "post"] == ["lint", "unit", "required"]
    assert state.runs == {"lint": 41, "unit": 42, "required": 43, "lab/lint": 1, "lab/unit": 2, "lab/required": 3}


def test_relayed_check_runs_carry_the_grade_run_its_units_and_the_lab_grade_page() -> None:
    calls = Calls()
    relay.relay(grade("success"), BRANCH, calls.ports(), LAB)
    lint, unit, required = calls.outputs
    details = f"{LAB}/grades/{HEAD}"
    assert lint == Output(
        "pull_request grade relayed",
        f"1 units of lint on {HEAD} against {BASE}, relayed from the lab grade",
        (Unit("static", "ruff-1", ()),),
        "run-7",
        GRADER,
        details,
    )
    assert unit.units == (Unit("tests", "batch-2", ("tests/test_a.py",)),)
    assert required.units == (Unit("static", "ruff-1", ()), Unit("tests", "batch-2", ("tests/test_a.py",)))
    assert {output.run for output in calls.outputs} == {"run-7"}
    assert required.summary == f"2 units of required on {HEAD} against {BASE}, relayed from the lab grade"


@pytest.mark.feature("lab-skipped-authoritative-gates")
def test_a_relayed_grade_posts_each_authoritative_gate_it_ran_no_unit_of_as_skipped() -> None:
    calls = Calls()
    state = grade("success")
    state.posted["unit-selected"] = ["completed", "skipped"]
    actions = relay.relay(state, BRANCH, calls.ports(), LAB)
    posts = [call for call in calls.log if call[0] == "post"]
    skipped = calls.outputs[posts.index(("post", HEAD, "unit-selected", "completed", "skipped", False))]
    assert f"posted lab/unit-selected skipped on {HEAD}" in actions
    assert skipped.units == ()
    assert skipped.summary == f"0 units of unit-selected on {HEAD} against {BASE}, relayed from the lab grade"


def test_a_relayed_green_grade_updates_the_open_pull_request_head_with_its_check_runs_in_the_same_grade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = Calls()
    request = Mock(return_value=answer(200, [{"html_url": PULL, "number": 9}]))
    monkeypatch.setattr(requests, "request", request)
    ports = relay.Ports(calls.pusher, calls.checks, lambda slug: relay.PullRequests(slug, Token()), calls.save)
    actions = relay.relay(grade("success"), BRANCH, ports, LAB)
    assert [call for call in calls.log if call[0] in ("push", "post")] == [
        ("push", HEAD, BRANCH),
        ("post", HEAD, "lint", "completed", "success", False),
        ("post", HEAD, "unit", "completed", "success", False),
        ("post", HEAD, "required", "completed", "success", False),
    ]
    assert {output.run for output in calls.outputs} == {"run-7"}
    assert actions[-1] == f"updated {PULL}"
    request.assert_called_once()
    assert request.call_args.args == ("GET", f"https://api.github.com/repos/{SLUG}/pulls")


def test_a_red_grade_relays_nothing_and_names_its_failed_units() -> None:
    calls = Calls()
    actions = relay.relay(grade("failure", outcome=FAILED), BRANCH, calls.ports(), LAB)
    assert calls.log == []
    assert actions == [f"held {HEAD} for {BRANCH}: grade run-7 is failure, failed units tests:batch-2"]


def test_a_red_grade_names_every_failed_unit_separated_by_a_comma() -> None:
    state = grade("failure", outcome=FAILED)
    state.units["static:ruff-1"].outcome = FAILED
    actions = relay.relay(state, BRANCH, Calls().ports(), LAB)
    assert actions == [f"held {HEAD} for {BRANCH}: grade run-7 is failure, failed units static:ruff-1, tests:batch-2"]


def test_a_failed_grade_without_a_failed_unit_says_so() -> None:
    calls = Calls()
    actions = relay.relay(grade("failure"), BRANCH, calls.ports(), LAB)
    assert calls.log == []
    assert actions == [f"held {HEAD} for {BRANCH}: grade run-7 is failure, failed units none"]


def test_failed_names_every_unit_whose_outcome_is_not_passed_in_grade_order() -> None:
    state = grade("failure", outcome=FAILED)
    state.units["static:ruff-1"].outcome = "lost"
    assert relay.failed(state) == ["static:ruff-1", "tests:batch-2"]


def test_a_tick_holds_a_pending_head_until_a_grade_of_it_closes_then_relays_it_once() -> None:
    client, calls = fake_redis(), Calls()
    grades: dict[str, list[State]] = {}
    engine = relay.Relay(
        client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), grades.__getitem__, calls.ports(), LAB
    )
    grades[HEAD] = []
    assert engine.tick() == [f"pending {HEAD} for {BRANCH}"]
    assert calls.log == []
    assert client.hget(relay.PENDING, f"{HEAD} {BRANCH}") is not None
    grades[HEAD] = [grade("success")]
    actions = engine.tick()
    assert actions[0] == f"pushed {HEAD} to {BRANCH}"
    assert actions[-1] == f"opened {PULL}"
    assert client.hgetall(relay.PENDING) == {}
    assert {output.details for output in calls.outputs} == {f"{LAB}/grades/{HEAD}"}
    assert engine.tick() == []
    assert [call for call in calls.log if call[0] == "push"] == [("push", HEAD, BRANCH)]


def test_a_tick_holds_a_head_with_no_closed_grade_and_still_relays_the_next_one() -> None:
    client, calls = fake_redis(), Calls()
    found: dict[str, list[State]] = {HEAD: [], OTHER: [replace(grade("success"), head=OTHER)]}
    first, second = relay.Pending(REPO, HEAD, REF, BRANCH), relay.Pending(REPO, OTHER, "refs/grade/t2", "engineer-2")
    engine = relay.Relay(client, Notices([first, second]), found.__getitem__, calls.ports(), LAB)
    actions = engine.tick()
    assert actions[2] == f"pushed {OTHER} to engineer-2"
    assert list(client.hgetall(relay.PENDING)) == [f"{HEAD} {BRANCH}"]


def test_a_tick_relays_the_green_one_of_several_closed_grades() -> None:
    client, calls = fake_redis(), Calls()
    states = [grade("failure", outcome=FAILED, run="run-1"), grade("success", run="run-2")]
    found = {HEAD: states}
    engine = relay.Relay(
        client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.__getitem__, calls.ports(), LAB
    )
    actions = engine.tick()
    assert actions[1] == f"pushed {HEAD} to {BRANCH}"
    assert {output.run for output in calls.outputs} == {"run-2"}


def test_a_tick_holds_a_head_whose_closed_grades_are_all_red_and_forgets_it() -> None:
    client, calls = fake_redis(), Calls()
    found = {HEAD: [grade("failure", outcome=FAILED)]}
    engine = relay.Relay(
        client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.__getitem__, calls.ports(), LAB
    )
    assert engine.tick() == [
        f"pending {HEAD} for {BRANCH}",
        f"held {HEAD} for {BRANCH}: grade run-7 is failure, failed units tests:batch-2",
    ]
    assert calls.log == []
    assert client.hgetall(relay.PENDING) == {}


def test_a_tick_drops_a_head_whose_closed_grades_were_all_superseded() -> None:
    client, calls = fake_redis(), Calls()
    found = {HEAD: [grade("cancelled", superseded=True)]}
    engine = relay.Relay(
        client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.__getitem__, calls.ports(), LAB
    )
    assert engine.tick() == [f"pending {HEAD} for {BRANCH}", f"dropped {HEAD} for {BRANCH}: every grade superseded"]
    assert calls.log == []
    assert client.hgetall(relay.PENDING) == {}


def test_a_tick_names_a_refused_relay_and_forgets_the_head() -> None:
    client, calls = fake_redis(), Refusing()
    found = {HEAD: [grade("success")]}
    engine = relay.Relay(
        client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.__getitem__, calls.ports(), LAB
    )
    assert engine.tick() == [
        f"pending {HEAD} for {BRANCH}",
        f"refused {HEAD} for {BRANCH}: git push answered 1: non fast forward",
    ]
    assert calls.log == [("pusher", REPO), ("push", HEAD, BRANCH)]
    assert client.hgetall(relay.PENDING) == {}


def test_a_tick_keeps_each_pending_head_apart() -> None:
    client, calls = fake_redis(), Calls()
    found: dict[str, list[State]] = {HEAD: [], OTHER: []}
    first, second = relay.Pending(REPO, HEAD, REF, BRANCH), relay.Pending(REPO, OTHER, "refs/grade/t2", "engineer-2")
    engine = relay.Relay(client, Notices([first, second]), found.__getitem__, calls.ports(), LAB)
    assert engine.tick() == [f"pending {HEAD} for {BRANCH}", f"pending {OTHER} for engineer-2"]
    assert relay.Pending(**json.loads(client.hget(relay.PENDING, f"{OTHER} engineer-2"))) == second


def test_a_tick_keeps_one_head_pushed_to_two_branches_apart_and_relays_both() -> None:
    client, calls = fake_redis(), Calls()
    found: dict[str, list[State]] = {HEAD: []}
    first, second = relay.Pending(REPO, HEAD, REF, BRANCH), relay.Pending(REPO, HEAD, "refs/grade/t2", "engineer-2")
    engine = relay.Relay(client, Notices([first, second]), found.__getitem__, calls.ports(), LAB)
    assert engine.tick() == [f"pending {HEAD} for {BRANCH}", f"pending {HEAD} for engineer-2"]
    assert len(client.hgetall(relay.PENDING)) == 2
    found[HEAD] = [grade("success")]
    engine.tick()
    assert [call for call in calls.log if call[0] == "push"] == [("push", HEAD, BRANCH), ("push", HEAD, "engineer-2")]
    assert client.hgetall(relay.PENDING) == {}


class Failing(Calls):
    """Fake relay ports whose git calls for the first branch fail before GitHub answers."""

    def push(self, sha: str, branch: str) -> None:
        """Fail the first branch's push and keep the others."""
        if branch == BRANCH:
            raise subprocess.CalledProcessError(128, ["git", "fetch"])
        super().push(sha, branch)


@pytest.mark.parametrize("error", [OSError("connection reset"), requests.ConnectionError("connection reset")])
def test_a_relay_whose_call_fails_stays_pending_and_the_next_head_still_relays(error: Exception) -> None:
    class Down(Calls):
        """Fake relay ports whose pull requests call fails before GitHub answers."""

        def open(self, branch: str, title: str, body: str) -> str:
            """Fail the call."""
            raise error

    client, calls = fake_redis(), Down()
    found = {HEAD: [grade("success")]}
    engine = relay.Relay(
        client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.__getitem__, calls.ports(), LAB
    )
    assert engine.tick()[-1] == f"retry {HEAD} for {BRANCH}: connection reset"
    assert client.hget(relay.PENDING, f"{HEAD} {BRANCH}") is not None


class Flaky(Calls):
    """Fake relay ports whose first pull requests call fails before GitHub answers."""

    def __init__(self) -> None:
        super().__init__()
        self.down = True

    def open(self, branch: str, title: str, body: str) -> str:
        """Fail the first call, then answer that it opened one."""
        if self.down:
            self.down = False
            raise OSError("connection reset")
        return super().open(branch, title, body)


def test_a_retried_relay_posts_each_check_run_once_then_opens_the_pull_request() -> None:
    client, calls = fake_redis(), Flaky()
    found = {HEAD: [grade("success")]}
    engine = relay.Relay(
        client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.__getitem__, calls.ports(), LAB
    )
    assert engine.tick()[-1] == f"retry {HEAD} for {BRANCH}: connection reset"
    assert engine.tick() == [f"pushed {HEAD} to {BRANCH}", f"opened {PULL}"]
    assert [call[2] for call in calls.log if call[0] == "post"] == ["lint", "unit", "required"]
    assert client.hgetall(relay.PENDING) == {}


CHECK_RUNS = f"{API}/repos/{SLUG}/check-runs"


def github(sent: list[tuple[str, str]]) -> Callable[..., Mock]:
    """Answer check run requests as GitHub does: a create gets the next id, an update keeps the id its url names."""

    def request(method: str, url: str, **_: Any) -> Mock:
        sent.append((method, url))
        created = [verb for verb, _url in sent].count("POST")
        run = created if method == "POST" else int(url.rsplit("/", 1)[1])
        return answer(201 if method == "POST" else 200, {"id": run})

    return request


def started(environ: Mapping[str, str], lab: Scheduler, calls: Calls) -> relay.Relay:
    """Return the relay a scheduler start builds, its pushes and pull requests going to the recorder."""
    consumer = cast(Consumer, open_bus(BusConfig()).lab_heads.consumer(relay.DURABLE, relay.SUBJECT))
    engine = relay.build(environ, lab, consumer)
    engine.ports = replace(engine.ports, pusher=calls.pusher, pulls=calls.pulls)
    return engine


@pytest.mark.feature("lab-relay-check-run-restart")
def test_a_restarted_relay_posts_no_second_completed_check_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(requests, "request", github(sent))
    monkeypatch.setattr(relay, "Checks", partial(Checks, auth=Token()))
    client, calls = fake_redis(), Flaky()
    lab = Scheduler(
        client, LocalObjects(tmp_path / "objects"), cast(scheduler_module.Ports, Mock()), Settings(frozenset())
    )
    state = grade("success")
    client.set(scheduler_module.GRADES + state.run, json.dumps(asdict(state)))
    lab.objects.write_once(f"{head_prefix(HEAD)}{state.run}", b"")
    client.hset(relay.PENDING, f"{HEAD} {BRANCH}", json.dumps(asdict(relay.Pending(REPO, HEAD, REF, BRANCH))))
    environ = {
        relay.contract.GIT_MIRROR: str(tmp_path / "node"),
        relay.MIRROR_STORE: str(tmp_path / "stores"),
        post_receive.LAB_URL: LAB,
    }
    assert started(environ, lab, calls).tick() == [f"retry {HEAD} for {BRANCH}: connection reset"]
    again = started(environ, lab, calls)
    actions = again.tick()
    assert sent == [("POST", CHECK_RUNS)] * 3
    assert actions == [f"pushed {HEAD} to {BRANCH}", f"opened {PULL}"]
    stored = lab.grade(state.run)
    assert stored is not None
    assert stored.runs == {"lab/lint": 1, "lab/unit": 2, "lab/required": 3}
    assert cast(Checks, again.ports.checks(SLUG)).runs == {
        (HEAD, "lab/lint", "run-7"): (1, "completed"),
        (HEAD, "lab/unit", "run-7"): (2, "completed"),
        (HEAD, "lab/required", "run-7"): (3, "completed"),
    }
    assert client.hgetall(relay.PENDING) == {}


def test_a_failing_git_call_keeps_its_head_pending_and_the_next_head_still_relays() -> None:
    client, calls = fake_redis(), Failing()
    first, second = relay.Pending(REPO, HEAD, REF, BRANCH), relay.Pending(REPO, OTHER, "refs/grade/t2", "engineer-2")
    found = {HEAD: [grade("success")], OTHER: [replace(grade("success"), head=OTHER)]}
    engine = relay.Relay(client, Notices([first, second]), found.__getitem__, calls.ports(), LAB)
    actions = engine.tick()
    assert actions[2].startswith(f"retry {HEAD} for {BRANCH}: Command '['git', 'fetch']' returned non-zero")
    assert actions[3] == f"pushed {OTHER} to engineer-2"
    assert relay.Pending(**json.loads(client.hget(relay.PENDING, f"{HEAD} {BRANCH}"))) == first
    assert client.hget(relay.PENDING, f"{OTHER} engineer-2") is None


GADGETS = "https://github.com/acme/gadgets"
REFUSAL = "GitHub App cannot supply scoped contents write and workflows write credentials: GitHub answered 422"
REFUSALS = {
    "push": REFUSAL,
    "post": "GitHub App cannot supply scoped checks write credentials: GitHub answered 422",
    "open": "GitHub App cannot supply scoped pull_requests write credentials: GitHub answered 422",
}


class Unminted(Calls):
    """Fake relay ports whose App refuses the token of one relay call, the push by default, for the widgets
    repository while refusing is set."""

    def __init__(self, call: str = "push") -> None:
        super().__init__()
        self.call = call
        self.repo = ""
        self.refusing = True

    def pusher(self, repo: str) -> "Calls":
        """Return the pusher of a repository and remember the repository."""
        self.repo = repo
        return super().pusher(repo)

    def refuse(self, call: str) -> None:
        """Refuse the widgets repository's token for the call while refusing is set and the call is the refused one."""
        if self.refusing and call == self.call and self.repo == REPO:
            raise github_app.GitAuthRefused(REFUSALS[call], status_code=422)

    def push(self, sha: str, branch: str) -> None:
        """Refuse the push token or keep the push."""
        self.refuse("push")
        super().push(sha, branch)

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        """Refuse the check runs token or keep the post."""
        self.refuse("post")
        return super().post(head, gate, status, conclusion, output, shadow)

    def open(self, branch: str, title: str, body: str) -> str:
        """Refuse the pull requests token or keep the pull request call."""
        self.refuse("open")
        return super().open(branch, title, body)


@pytest.mark.feature("lab-relay-app-refusal")
def test_a_relay_whose_app_token_is_refused_stays_pending_and_another_repository_still_relays() -> None:
    client, calls = fake_redis(), Unminted()
    first = relay.Pending(REPO, HEAD, REF, BRANCH)
    second = relay.Pending(GADGETS, OTHER, "refs/grade/t2", "engineer-2")
    found = {HEAD: [grade("success")], OTHER: [replace(grade("success"), repo=GADGETS, head=OTHER)]}
    engine = relay.Relay(client, Notices([first, second]), found.__getitem__, calls.ports(), LAB)
    actions = engine.tick()
    assert actions[2:4] == [f"held {HEAD} for {BRANCH} for a later pass: {REFUSAL}", f"pushed {OTHER} to engineer-2"]
    assert actions[-1] == f"opened {PULL}"
    assert relay.Pending(**json.loads(client.hget(relay.PENDING, f"{HEAD} {BRANCH}"))) == first
    assert client.hget(relay.PENDING, f"{OTHER} engineer-2") is None


@pytest.mark.feature("lab-relay-app-refusal")
def test_a_refused_repository_relays_nothing_until_the_post_hold_ends_while_another_repository_relays() -> None:
    client, calls = fake_redis(), Unminted()
    start = 1000.0
    clock = Mock(return_value=start)
    first = relay.Pending(REPO, HEAD, REF, BRANCH)
    second = relay.Pending(GADGETS, OTHER, "refs/grade/t2", "engineer-2")
    found = {HEAD: [grade("success")], OTHER: [replace(grade("success"), repo=GADGETS, head=OTHER)]}
    ports = replace(calls.ports(), clock=clock)
    engine = relay.Relay(client, Notices([first], [second]), found.__getitem__, ports, LAB)
    assert engine.tick() == [f"pending {HEAD} for {BRANCH}", f"held {HEAD} for {BRANCH} for a later pass: {REFUSAL}"]
    calls.refusing = False
    calls.log.clear()
    clock.return_value = start + scheduler_module.POST_HOLD_MS / 1000 - 0.001
    assert engine.tick()[:2] == [f"pending {OTHER} for engineer-2", f"pushed {OTHER} to engineer-2"]
    assert ("pusher", REPO) not in calls.log
    assert relay.Pending(**json.loads(client.hget(relay.PENDING, f"{HEAD} {BRANCH}"))) == first
    clock.return_value = start + scheduler_module.POST_HOLD_MS / 1000
    actions = engine.tick()
    assert actions[0] == f"pushed {HEAD} to {BRANCH}"
    assert actions[-1] == f"opened {PULL}"
    assert client.hgetall(relay.PENDING) == {}


@pytest.mark.feature("lab-relay-app-refusal")
@pytest.mark.parametrize("call", ["post", "open"])
def test_a_token_refused_after_the_push_relays_again_after_the_hold_posting_each_check_run_once(call: str) -> None:
    client, calls = fake_redis(), Unminted(call)
    start = 1000.0
    clock = Mock(return_value=start)
    found = {HEAD: [grade("success")]}
    ports = replace(calls.ports(), clock=clock)
    engine = relay.Relay(client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.__getitem__, ports, LAB)
    held = f"held {HEAD} for {BRANCH} for a later pass: {REFUSALS[call]}"
    assert engine.tick() == [f"pending {HEAD} for {BRANCH}", held]
    calls.refusing = False
    clock.return_value = start + scheduler_module.POST_HOLD_MS / 1000
    actions = engine.tick()
    assert (actions[0], actions[-1]) == (f"pushed {HEAD} to {BRANCH}", f"opened {PULL}")
    assert [entry[2] for entry in calls.log if entry[0] == "post"] == ["lint", "unit", "required"]
    assert calls.log.count(("push", HEAD, BRANCH)) == 2
    assert client.hgetall(relay.PENDING) == {}


def test_bus_notices_keep_trusted_grade_ref_heads_with_a_branch_and_acknowledge_every_envelope() -> None:
    bus = open_bus(BusConfig())
    consumer = bus.lab_heads.consumer(relay.DURABLE, relay.SUBJECT)
    environ = {
        post_receive.REPOSITORY: REPO,
        post_receive.ACTOR: "agenticore-app[bot]",
        post_receive.LAB_URL: LAB,
        post_receive.OPTION_COUNT: "1",
        f"{post_receive.OPTION}0": f"branch={BRANCH}",
    }
    lines = [f"{'0' * 40} {HEAD} {REF}\n", f"{'0' * 40} {OTHER} refs/heads/x\n"]
    post_receive.run(lines, environ, bus.lab_heads, lambda sha: BASE, StringIO())
    bare = {key: value for key, value in environ.items() if not key.startswith("GIT_PUSH")}
    post_receive.run([f"{'0' * 40} {OTHER} refs/grade/t2\n"], bare, bus.lab_heads, lambda sha: BASE, StringIO())
    stranger = {**environ, post_receive.ACTOR: "stranger"}
    post_receive.run([f"{'0' * 40} {OTHER} refs/grade/t3\n"], stranger, bus.lab_heads, lambda sha: BASE, StringIO())
    notices = relay.BusNotices(cast(Consumer, consumer), frozenset({"agenticore-app[bot]"}))
    assert notices.pending() == [relay.Pending(REPO, HEAD, REF, BRANCH)]
    assert notices.pending() == []
    assert relay.SUBJECT == "ci.lab.head.notice"


def test_bus_notices_fetch_one_batch_in_the_scheduler_s_batch_shape() -> None:
    consumer = Mock(fetch=Mock(return_value=[]))
    assert relay.BusNotices(consumer, frozenset()).pending() == []
    consumer.fetch.assert_called_once_with(scheduler_module.BATCH, scheduler_module.FETCH_MS)


def test_closed_grades_of_a_head_are_the_runs_its_evidence_markers_name(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path)
    objects.write_once(f"{head_prefix(HEAD)}run-2", b"")
    objects.write_once(f"{head_prefix(HEAD)}run-1", b"")
    objects.write_once(f"{head_prefix(HEAD)}run-gone", b"")
    objects.write_once(f"{head_prefix(OTHER)}run-9", b"")
    states = {"run-1": grade("failure", run="run-1"), "run-2": grade("success", run="run-2")}
    scheduler = cast(Scheduler, Mock(objects=objects, grade=Mock(side_effect=states.get)))
    assert [state.run for state in relay.closed(scheduler, HEAD)] == ["run-1", "run-2"]


def mint(monkeypatch: pytest.MonkeyPatch, permissions: Mapping[str, str]) -> Mock:
    """Answer the App token request with a token holding the permissions on example/repo; return its mock."""
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture")
    monkeypatch.setattr(github_app.jwt, "encode", Mock(return_value="signed"))
    granted = {
        "token": "fixture",
        "expires_at": "2099-01-01T00:00:00Z",
        "permissions": {**permissions, "metadata": "read"},
        "repositories": [{"full_name": "example/repo"}],
    }
    minted = Mock(return_value=answer(201, granted))
    monkeypatch.setattr(requests, "post", minted)
    return minted


def test_an_open_pull_request_of_the_branch_is_reported_updated_with_no_create(monkeypatch: pytest.MonkeyPatch) -> None:
    request = Mock(return_value=answer(200, [{"html_url": PULL, "number": 9}]))
    monkeypatch.setattr(requests, "request", request)
    assert relay.PullRequests("example/repo", Token()).open(BRANCH, "title", "body") == f"updated {PULL}"
    request.assert_called_once_with(
        "GET",
        "https://api.github.com/repos/example/repo/pulls",
        headers=HEADERS,
        params={"head": f"example:{BRANCH}", "state": "open"},
        json=None,
        timeout=relay.TIMEOUT,
    )


def test_the_open_pull_request_lookup_names_the_first_path_segment_as_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = Mock(return_value=answer(200, [{"html_url": PULL, "number": 9}]))
    monkeypatch.setattr(requests, "request", request)
    relay.PullRequests("example/group/repo", Token()).open(BRANCH, "title", "body")
    assert request.call_args.kwargs["params"] == {"head": f"example:{BRANCH}", "state": "open"}


def test_a_branch_without_an_open_pull_request_gets_a_draft_into_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    request = Mock(side_effect=[answer(200, []), answer(201, {"html_url": PULL, "number": 9})])
    monkeypatch.setattr(requests, "request", request)
    assert relay.PullRequests("example/repo", Token()).open(BRANCH, "title", "body") == f"opened {PULL}"
    assert request.call_args.args == ("POST", "https://api.github.com/repos/example/repo/pulls")
    assert request.call_args.kwargs == {
        "headers": HEADERS,
        "params": None,
        "json": {"title": "title", "head": BRANCH, "base": "dev", "body": "body", "draft": True},
        "timeout": relay.TIMEOUT,
    }


@pytest.mark.parametrize("status", [404, 422, 500])
def test_a_refused_pull_request_call_raises_relay_refused_naming_it(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(requests, "request", Mock(return_value=answer(status, {})))
    with pytest.raises(relay.RelayRefused, match=f"^GET pulls answered {status}$"):
        relay.PullRequests("example/repo", Token()).open(BRANCH, "title", "body")


def test_the_pull_requests_token_asks_only_pull_requests_write(monkeypatch: pytest.MonkeyPatch) -> None:
    minted = mint(monkeypatch, relay.PULLS)
    monkeypatch.setattr(requests, "request", Mock(return_value=answer(200, [{"html_url": PULL}])))
    relay.PullRequests("example/repo").open(BRANCH, "title", "body")
    assert minted.call_args.kwargs["json"] == {"repositories": ["repo"], "permissions": {"pull_requests": "write"}}


def test_the_pusher_fetches_the_sha_and_pushes_it_to_the_branch_with_the_token_in_its_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mirror = Mock(spec=Mirror, store=tmp_path / "store")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(relay.subprocess, "run", run)
    relay.GitPusher(cast(Mirror, mirror), "example/repo", Token()).push(HEAD, BRANCH)
    mirror.fetch.assert_called_once_with(HEAD)
    argv = run.call_args.args[0]
    assert argv == [
        "git",
        "-C",
        str(tmp_path / "store"),
        "push",
        "https://github.com/example/repo.git",
        f"{HEAD}:refs/heads/{BRANCH}",
    ]
    assert "fixture" not in " ".join(argv)
    environ = run.call_args.kwargs["env"]
    secret = base64.b64encode(b"x-access-token:fixture").decode()
    assert {key: environ[key] for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_TERMINAL_PROMPT")} == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_TERMINAL_PROMPT": "0",
    }
    assert environ["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {secret}"
    assert run.call_args.kwargs["capture_output"] is True
    assert run.call_args.kwargs["text"] is True
    assert run.call_args.kwargs["check"] is False


def test_a_refused_push_raises_relay_refused_with_git_s_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mirror = Mock(spec=Mirror, store=tmp_path)
    refused = subprocess.CompletedProcess([], 1, "", " ! [rejected] non-fast-forward \n")
    monkeypatch.setattr(relay.subprocess, "run", Mock(return_value=refused))
    with pytest.raises(relay.RelayRefused, match=r"^git push of a+ to engineer-1 answered 1: ! \[rejected\]"):
        relay.GitPusher(cast(Mirror, mirror), "example/repo", Token()).push(HEAD, BRANCH)


@pytest.mark.feature("lab-remote-heads")
def test_the_push_token_asks_contents_and_workflows_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pushing = {"contents": "write", "workflows": "write"}
    minted = mint(monkeypatch, pushing)
    monkeypatch.setattr(relay.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, "", "")))
    relay.GitPusher(cast(Mirror, Mock(spec=Mirror, store=tmp_path)), "example/repo").push(HEAD, BRANCH)
    assert minted.call_args.kwargs["json"] == {"repositories": ["repo"], "permissions": pushing}


@pytest.mark.feature("lab-remote-heads")
def test_build_gives_the_pusher_mirror_the_lab_remote_the_environment_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture")
    environ = {
        relay.contract.GIT_MIRROR: str(tmp_path / "node"),
        relay.MIRROR_STORE: str(tmp_path / "stores"),
        post_receive.LAB_URL: LAB,
        "AGENTIHOOKS_LAB_REMOTE": "http://ci-lab-remote.ci-lab:8080/widgets.git",
        "AGENTIHOOKS_LAB_REMOTE_TOKEN": "lab-token",
    }
    scheduler = cast(Scheduler, Mock(client=fake_redis(), settings=Mock(trusted=frozenset[str]())))
    consumer = cast(Consumer, open_bus(BusConfig()).lab_heads.consumer(relay.DURABLE, relay.SUBJECT))
    pusher = cast(relay.GitPusher, relay.build(environ, scheduler, consumer).ports.pusher(REPO))
    found = Lab("http://ci-lab-remote.ci-lab:8080/widgets.git", "lab-token")
    assert pusher.mirror == Mirror(tmp_path / "node", tmp_path / "stores" / "acme--widgets", REPO, found)


def test_build_wires_the_relay_to_the_scheduler_grades_the_mirror_and_the_lab_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture")
    environ = {
        relay.contract.GIT_MIRROR: str(tmp_path / "node"),
        relay.MIRROR_STORE: str(tmp_path / "deep" / "stores"),
        post_receive.LAB_URL: f"{LAB}/",
    }
    client = fake_redis()
    trusted = frozenset({"agenticore-app[bot]"})
    settings = Mock(trusted=trusted)
    scheduler = cast(Scheduler, Mock(client=client, objects=LocalObjects(tmp_path / "objects"), settings=settings))
    consumer = cast(Consumer, open_bus(BusConfig()).lab_heads.consumer(relay.DURABLE, relay.SUBJECT))
    engine = relay.build(environ, scheduler, consumer)
    assert engine.client is client
    assert engine.lab == LAB
    assert cast(relay.BusNotices, engine.notices).consumer is consumer
    assert cast(relay.BusNotices, engine.notices).trusted is trusted
    pusher = cast(relay.GitPusher, engine.ports.pusher(REPO))
    assert pusher.slug == SLUG
    assert pusher.mirror == Mirror(tmp_path / "node", tmp_path / "deep" / "stores" / "acme--widgets", REPO)
    assert (tmp_path / "deep" / "stores").is_dir()
    assert cast(relay.PullRequests, engine.ports.pulls(SLUG)).slug == SLUG
    assert cast(relay.Checks, engine.ports.checks(SLUG)).repo == SLUG
    assert cast(relay.Checks, engine.ports.checks(SLUG)).registry is scheduler.registry
    assert engine.ports.checks(SLUG) is engine.ports.checks(SLUG)
    assert cast(relay.Checks, engine.ports.checks("acme/other")).repo == "acme/other"
    assert engine.grades(HEAD) == []
    again = relay.build({**environ, post_receive.LAB_URL: "http://lab.example/X/"}, scheduler, consumer)
    assert again.lab == "http://lab.example/X"
