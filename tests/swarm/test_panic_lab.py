"""Tests for panic reaching the CI lab: the panic button and the alarm tell the swarm's lab to grade the fixes first,
hold the rest of pull request grading and resume it."""

import json
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from hooks.observability import otel
from scripts.swarm import cli, lab_control, panic
from scripts.swarm.store import PREFIX, RedisStore, SwarmConfig, SwarmError
from scripts.tick.ports.source_control import ChangeRef, ChangeState

SLUG = "sw"
CODE = "a1b2c3"
URL = "http://lab:8800"
CONTROL = "http://lab:8801"
PULL = "https://github.com/the-cloud-clockwork/agentihooks/pull/1"
BRANCH = "https://github.com/the-cloud-clockwork/agentihooks/tree/fix-branch"
LAB_KEY = f"{PREFIX}:{SLUG}:panic:lab"


class Reply:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def __enter__(self) -> "Reply":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.body).encode()


def _store(monkeypatch: pytest.MonkeyPatch, lab_url: str) -> RedisStore:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    found = RedisStore(redis)
    found.names.mint = lambda: CODE
    found.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1, lab_url=lab_url))
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda environ=None: redis)
    monkeypatch.setattr(otel, "add_counter", lambda name, value, attrs: None)
    return found


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    return _store(monkeypatch, URL)


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Answer each lab order the way the lease API does and record the path, the authorization and the body."""
    monkeypatch.setenv(lab_control.TOKEN, "tok")
    orders: list[tuple[str, str, dict[str, Any]]] = []

    def urlopen(request: Request, timeout: float) -> Reply:
        body = json.loads(request.data if isinstance(request.data, bytes) else b"{}")
        path = request.full_url.removeprefix(CONTROL)
        orders.append((path, str(request.get_header("Authorization")), body))
        if path == "/priority":
            return Reply({"priority": body["heads"], "class": "hotfix", "expires_ms": 9})
        if path == "/pause":
            return Reply({"paused": body["class"], "except": body["except"]})
        return Reply({"resumed": ["pull_request"]})

    monkeypatch.setattr(lab_control, "urlopen", urlopen)
    return orders


@pytest.fixture
def heads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Read every fix pull request's head branch as pr-branch, and record each read."""
    read: list[str] = []

    def fix_change(url: str) -> ChangeState:
        read.append(url)
        return ChangeState(ChangeRef("o/r", 1), "open", "a" * 40, "dev", False, url, "pr-branch")

    monkeypatch.setattr(panic, "fix_change", fix_change)
    monkeypatch.setattr(panic, "cancel_runs", lambda *args, **kwargs: [])
    return read


def _press(store: RedisStore, capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    args = cli.build_parser().parse_args([SLUG, "panic", *argv])
    args.handler(store, args)
    return json.loads(capsys.readouterr().out)


@pytest.mark.feature("panic-reaches-lab")
def test_panic_on_grades_the_fixes_first_and_pauses_the_rest_of_pull_request_grading(
    store: RedisStore,
    lab: list[tuple[str, str, dict[str, Any]]],
    heads: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _press(store, capsys, "on", "--fix", PULL, "--fix", BRANCH)
    assert out["lab"] == "lab paused pull request grading except fix-branch, pr-branch"
    assert lab == [
        ("/priority", "Bearer tok", {"heads": ["fix-branch", "pr-branch"], "class": "hotfix"}),
        ("/pause", "Bearer tok", {"class": "pull_request", "except": ["fix-branch", "pr-branch"]}),
    ]
    assert store.redis.get(LAB_KEY) == CONTROL


@pytest.mark.feature("panic-reaches-lab")
def test_panic_on_spares_the_recorded_fixer_branches_in_the_lab_too(
    store: RedisStore,
    lab: list[tuple[str, str, dict[str, Any]]],
    heads: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    panic.keep_fixers(store, SLUG, [{"id": "promo-1-1", "state": "claimed", "branch": f"ci-{CODE}-0002"}])
    out = _press(store, capsys, "on", "--fix", BRANCH)
    assert out["lab"] == f"lab paused pull request grading except ci-{CODE}-0002, fix-branch"
    assert lab[1][2] == {"class": "pull_request", "except": [f"ci-{CODE}-0002", "fix-branch"]}


@pytest.mark.feature("panic-reaches-lab")
def test_panic_on_without_a_fix_pauses_every_pull_request_grade(
    store: RedisStore,
    lab: list[tuple[str, str, dict[str, Any]]],
    heads: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _press(store, capsys, "on")["lab"] == "lab paused pull request grading except no head"
    assert [body for _, _, body in lab] == [
        {"heads": [], "class": "hotfix"},
        {"class": "pull_request", "except": []},
    ]


@pytest.mark.feature("panic-reaches-lab")
def test_panic_on_with_no_lab_url_is_an_action_line_that_reads_no_fix(
    monkeypatch: pytest.MonkeyPatch,
    lab: list[tuple[str, str, dict[str, Any]]],
    heads: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(monkeypatch, "")
    panic.engage(store, SLUG, "pressed", False, [PULL], 0)
    out = _press(store, capsys, "on")
    assert out == {"panic": "on", "fixes": [PULL], "cancelled": [], "lab": "lab not told: the swarm has no lab url"}
    assert (lab, heads, store.redis.get(LAB_KEY)) == ([], [], None)


@pytest.mark.feature("panic-reaches-lab")
def test_panic_on_with_no_token_is_an_action_line(
    store: RedisStore,
    lab: list[tuple[str, str, dict[str, Any]]],
    heads: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(lab_control.TOKEN)
    out = _press(store, capsys, "on", "--fix", BRANCH)
    assert out["lab"] == "lab not told: CI_LAB_CONTROL_TOKEN is not set on this host"
    assert lab == []


@pytest.mark.feature("panic-reaches-lab")
@pytest.mark.parametrize(
    ("error", "why"),
    [
        (URLError("down"), f"the lab at {CONTROL} is unreachable: <urlopen error down>"),
        (HTTPError(CONTROL, 401, "Unauthorized", Message(), None), f"the lab at {CONTROL} refused the control token"),
    ],
    ids=["unreachable", "refused"],
)
def test_panic_on_holds_when_the_lab_cannot_be_told(
    store: RedisStore,
    heads: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    why: str,
) -> None:
    monkeypatch.setenv(lab_control.TOKEN, "tok")

    def urlopen(request: Request, timeout: float) -> Reply:
        raise error

    monkeypatch.setattr(lab_control, "urlopen", urlopen)
    out = _press(store, capsys, "on", "--fix", BRANCH)
    assert out["lab"] == f"lab not told: {why}"
    assert panic.state(store, SLUG) is not None
    assert store.redis.get(LAB_KEY) is None


@pytest.mark.feature("panic-reaches-lab")
def test_panic_on_names_a_stored_fix_it_cannot_read(
    store: RedisStore,
    lab: list[tuple[str, str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unreadable(url: str) -> ChangeState:
        raise SwarmError(f"cannot read pull request {url}")

    monkeypatch.setattr(panic, "fix_change", unreadable)
    monkeypatch.setattr(panic, "cancel_runs", lambda *args, **kwargs: [])
    panic.engage(store, SLUG, "pressed", False, [PULL], 0)
    assert _press(store, capsys, "on")["lab"] == f"lab not told: cannot read pull request {PULL}"
    assert lab == []


@pytest.mark.feature("panic-reaches-lab")
def test_the_lab_reads_a_merged_fix_once(
    store: RedisStore, lab: list[tuple[str, str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[str] = []

    def fix_change(url: str) -> ChangeState:
        read.append(url)
        return ChangeState(
            ChangeRef("o/r", 1), "closed", "a" * 40, "dev", True, url, "pr-branch", merge_commit="b" * 40
        )

    monkeypatch.setattr(panic, "fix_change", fix_change)
    assert panic.lab_engage(store, SLUG, [PULL]) == "lab paused pull request grading except pr-branch"
    assert panic.lab_engage(store, SLUG, [PULL]) == "lab paused pull request grading except pr-branch"
    assert read == [PULL]


@pytest.mark.feature("panic-reaches-lab")
def test_panic_off_resumes_the_lab(
    store: RedisStore,
    lab: list[tuple[str, str, dict[str, Any]]],
    heads: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _press(store, capsys, "on", "--fix", BRANCH)
    assert _press(store, capsys, "off") == {"panic": "off", "lab": "lab resumed pull request grading"}
    assert lab[-1] == ("/resume", "Bearer tok", {})
    assert store.redis.get(LAB_KEY) is None


@pytest.mark.feature("panic-reaches-lab")
def test_panic_off_keeps_the_lab_mark_when_the_lab_cannot_be_told(
    store: RedisStore,
    lab: list[tuple[str, str, dict[str, Any]]],
    heads: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _press(store, capsys, "on", "--fix", BRANCH)
    monkeypatch.delenv(lab_control.TOKEN)
    assert _press(store, capsys, "off") == {
        "panic": "off",
        "lab": "lab not told: CI_LAB_CONTROL_TOKEN is not set on this host",
    }
    assert store.redis.get(LAB_KEY) == CONTROL


@pytest.mark.feature("panic-reaches-lab")
def test_panic_off_leaves_a_lab_its_panic_never_paused(
    store: RedisStore,
    lab: list[tuple[str, str, dict[str, Any]]],
    heads: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    panic.engage(store, SLUG, "pressed", False, [BRANCH], 0)
    assert _press(store, capsys, "off") == {"panic": "off", "lab": ""}
    assert lab == []


@pytest.mark.feature("panic-reaches-lab")
def test_the_alarm_resumes_the_lab_its_released_panic_paused(
    store: RedisStore, lab: list[tuple[str, str, dict[str, Any]]], heads: list[str]
) -> None:
    panic.engage(store, SLUG, "60 errors in the last hour", True, [], 0)
    assert panic.lab_engage(store, SLUG, [BRANCH]) == "lab paused pull request grading except fix-branch"
    assert panic.alarm(store, SLUG, 1_000) == (
        "panic released: the fleet is back at business as usual; lab resumed pull request grading"
    )
    assert lab[-1] == ("/resume", "Bearer tok", {})
    assert store.redis.get(LAB_KEY) is None


@pytest.mark.feature("panic-reaches-lab")
def test_the_alarm_leaves_a_lab_its_panic_never_paused(
    store: RedisStore, lab: list[tuple[str, str, dict[str, Any]]]
) -> None:
    panic.engage(store, SLUG, "60 errors in the last hour", True, [], 0)
    assert panic.alarm(store, SLUG, 1_000) == "panic released: the fleet is back at business as usual"
    assert lab == []
