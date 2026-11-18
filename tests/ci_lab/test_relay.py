"""Tests for the lab relay: pending grade ref notices, a green grade's push, check runs and draft pull request, a red
grade held with its failed units, and the GitHub adapters' scoped tokens."""

import base64
import json
import subprocess
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
import requests

from scripts import github_app
from scripts.ci_lab import relay
from scripts.ci_lab.checks import Output
from scripts.ci_lab.evidence import head_prefix
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.outcomes import FAILED, PASSED
from scripts.ci_lab.remote import post_receive
from scripts.ci_lab.scheduler import Flags, Scheduler, Slot, State
from scripts.ci_lab.units import Unit
from scripts.ci_lab.worktrees import Mirror
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

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        """Keep the check run post and its output."""
        self.log.append(("post", head, gate, status, conclusion, shadow))
        self.outputs.append(output)
        return len(self.outputs)

    def open(self, branch: str, title: str, body: str) -> str:
        """Keep the pull request call and answer that it opened one."""
        self.log.append(("open", branch, title, body))
        return f"opened {PULL}"

    def ports(self) -> relay.Ports:
        """Return the relay ports over this recorder."""
        return relay.Ports(self.pusher, self.checks, self.pulls)


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
    ("ref", "branch"), [(REF, ""), ("refs/heads/engineer-1", BRANCH), ("refs/gradex/t1", BRANCH), ("", BRANCH)]
)
def test_a_notice_of_another_ref_or_without_a_branch_is_not_pending(ref: str, branch: str) -> None:
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


def test_a_red_grade_relays_nothing_and_names_its_failed_units() -> None:
    calls = Calls()
    actions = relay.relay(grade("failure", outcome=FAILED), BRANCH, calls.ports(), LAB)
    assert calls.log == []
    assert actions == [f"held {HEAD} for {BRANCH}: grade run-7 is failure, failed units tests:batch-2"]


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
    engine = relay.Relay(client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), grades.get, calls.ports(), LAB)
    grades[HEAD] = []
    assert engine.tick() == [f"pending {HEAD} for {BRANCH}"]
    assert calls.log == []
    assert client.hget(relay.PENDING, HEAD) is not None
    grades[HEAD] = [grade("success")]
    actions = engine.tick()
    assert actions[0] == f"pushed {HEAD} to {BRANCH}"
    assert actions[-1] == f"opened {PULL}"
    assert client.hgetall(relay.PENDING) == {}
    assert engine.tick() == []
    assert [call for call in calls.log if call[0] == "push"] == [("push", HEAD, BRANCH)]


def test_a_tick_relays_the_green_one_of_several_closed_grades() -> None:
    client, calls = fake_redis(), Calls()
    states = [grade("failure", outcome=FAILED, run="run-1"), grade("success", run="run-2")]
    found = {HEAD: states}
    engine = relay.Relay(client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.get, calls.ports(), LAB)
    actions = engine.tick()
    assert actions[1] == f"pushed {HEAD} to {BRANCH}"
    assert {output.run for output in calls.outputs} == {"run-2"}


def test_a_tick_holds_a_head_whose_closed_grades_are_all_red_and_forgets_it() -> None:
    client, calls = fake_redis(), Calls()
    found = {HEAD: [grade("failure", outcome=FAILED)]}
    engine = relay.Relay(client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.get, calls.ports(), LAB)
    assert engine.tick() == [
        f"pending {HEAD} for {BRANCH}",
        f"held {HEAD} for {BRANCH}: grade run-7 is failure, failed units tests:batch-2",
    ]
    assert calls.log == []
    assert client.hgetall(relay.PENDING) == {}


def test_a_tick_drops_a_head_whose_closed_grades_were_all_superseded() -> None:
    client, calls = fake_redis(), Calls()
    found = {HEAD: [grade("cancelled", superseded=True)]}
    engine = relay.Relay(client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.get, calls.ports(), LAB)
    assert engine.tick() == [f"pending {HEAD} for {BRANCH}", f"dropped {HEAD} for {BRANCH}: every grade superseded"]
    assert calls.log == []
    assert client.hgetall(relay.PENDING) == {}


def test_a_tick_names_a_refused_relay_and_forgets_the_head() -> None:
    client, calls = fake_redis(), Refusing()
    found = {HEAD: [grade("success")]}
    engine = relay.Relay(client, Notices([relay.Pending(REPO, HEAD, REF, BRANCH)]), found.get, calls.ports(), LAB)
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
    engine = relay.Relay(client, Notices([first, second]), found.get, calls.ports(), LAB)
    assert engine.tick() == [f"pending {HEAD} for {BRANCH}", f"pending {OTHER} for engineer-2"]
    assert relay.Pending(**json.loads(client.hget(relay.PENDING, OTHER))) == second


def test_bus_notices_keep_grade_ref_heads_with_a_branch_and_acknowledge_every_envelope() -> None:
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
    notices = relay.BusNotices(consumer)
    assert notices.pending() == [relay.Pending(REPO, HEAD, REF, BRANCH)]
    assert notices.pending() == []
    assert relay.SUBJECT == "ci.lab.head.notice"


def test_bus_notices_fetch_one_batch_of_fifty_within_a_hundred_milliseconds() -> None:
    consumer = Mock(fetch=Mock(return_value=[]))
    assert relay.BusNotices(consumer).pending() == []
    consumer.fetch.assert_called_once_with(50, 100)


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
    assert relay.PULLS == {"pull_requests": "write"}
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


def test_the_push_token_asks_only_contents_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    minted = mint(monkeypatch, relay.CONTENTS)
    monkeypatch.setattr(relay.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, "", "")))
    relay.GitPusher(cast(Mirror, Mock(spec=Mirror, store=tmp_path)), "example/repo").push(HEAD, BRANCH)
    assert relay.CONTENTS == {"contents": "write"}
    assert minted.call_args.kwargs["json"] == {"repositories": ["repo"], "permissions": {"contents": "write"}}


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
    scheduler = cast(Scheduler, Mock(client=client, objects=LocalObjects(tmp_path / "objects")))
    consumer = open_bus(BusConfig()).lab_heads.consumer(relay.DURABLE, relay.SUBJECT)
    relay.build(environ, scheduler, consumer)
    engine = relay.build(environ, scheduler, consumer)
    assert engine.client is client
    assert engine.lab == LAB
    assert cast(relay.BusNotices, engine.notices).consumer is consumer
    pusher = cast(relay.GitPusher, engine.ports.pusher(REPO))
    assert pusher.slug == SLUG
    assert pusher.mirror == Mirror(tmp_path / "node", tmp_path / "deep" / "stores" / "acme--widgets", REPO)
    assert (tmp_path / "deep" / "stores").is_dir()
    assert cast(relay.PullRequests, engine.ports.pulls(SLUG)).slug == SLUG
    assert cast(relay.Checks, engine.ports.checks(SLUG)).repo == SLUG
    assert engine.grades(HEAD) == []
