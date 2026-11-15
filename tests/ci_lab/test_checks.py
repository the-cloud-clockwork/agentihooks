"""Tests for the lab check runs: the checks write token, names, transitions, output, batching and backoff."""

from collections.abc import Mapping
from dataclasses import replace
from unittest.mock import Mock

import pytest
import requests

from scripts import github_app
from scripts.ci_lab import checks
from scripts.ci_lab.checks import Checks, Output, PostRefused, Transition
from scripts.ci_lab.units import Unit

HEAD = "a" * 40
URL = "https://api.github.com/repos/example/repo/check-runs"
HEADERS = {
    "Authorization": "Bearer fixture",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
UNITS = (Unit("static", "ruff-1f2e", ()), Unit("tests", "batch-9c0d", ("tests/test_a.py",)))
OUTPUT = Output("2 units", "Grading", UNITS, "run-7", "b" * 40, "https://lab.example/grades/run-7")
GATES = ("lint", "x", "shadow/lint", "-shadow/lint", "lab-shadow/lint", "lab/lint", "ruff format")
LIMITED = "API rate limit exceeded for installation ID 456."
SECONDARY = "You have exceeded a secondary rate limit."


class Token:
    """A token source that always hands out the fixture token."""

    def get_token(self) -> str:
        """Return the fixture token."""
        return "fixture"


def answer(
    status: int, body: Mapping[str, object] | None = None, text: str = "", headers: Mapping[str, str] | None = None
) -> Mock:
    """Return a mocked HTTP response."""
    return Mock(status_code=status, json=Mock(return_value=dict(body or {})), text=text, headers=dict(headers or {}))


def lab(monkeypatch: pytest.MonkeyPatch, *answers: Mock) -> tuple[Checks, Mock, Mock]:
    """Return a poster for example/repo whose requests answer in order, with its request and sleep mocks."""
    request = Mock(side_effect=list(answers))
    monkeypatch.setattr(requests, "request", request)
    sleep = Mock()
    return Checks("example/repo", Token(), sleep=sleep, clock=lambda: 1000.0), request, sleep


def test_the_lab_token_request_carries_only_checks_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture")
    monkeypatch.setattr(github_app.jwt, "encode", Mock(return_value="signed"))
    granted = {
        "token": "lab-token",
        "expires_at": "2099-01-01T00:00:00Z",
        "permissions": {"checks": "write", "metadata": "read"},
        "repositories": [{"full_name": "example/repo"}],
    }
    mint = Mock(return_value=answer(201, granted))
    monkeypatch.setattr(requests, "post", mint)
    request = Mock(return_value=answer(201, {"id": 7}))
    monkeypatch.setattr(requests, "request", request)
    assert Checks("example/repo").post(HEAD, "lint", "queued", None, OUTPUT, False) == 7
    assert mint.call_args.kwargs["json"] == {"repositories": ["repo"], "permissions": {"checks": "write"}}
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer lab-token"


def test_shadow_names_never_collide_with_authoritative_names() -> None:
    authoritative = {checks.name(gate, False) for gate in GATES}
    shadow = {checks.name(gate, True) for gate in GATES}
    assert checks.name("lint", False) == "lab/lint"
    assert checks.name("lint", True) == "lab-shadow/lint"
    assert len(authoritative) == len(shadow) == len(GATES)
    assert not authoritative & shadow
    assert all(check.startswith("lab/") for check in authoritative)
    assert all(check.startswith("lab-shadow/") for check in shadow)


def test_a_check_run_needs_a_gate_name() -> None:
    with pytest.raises(ValueError, match="gate name"):
        checks.name("", True)


def test_shadow_and_authoritative_grades_of_one_gate_are_separate_check_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    poster, request, _ = lab(monkeypatch, answer(201, {"id": 1}), answer(201, {"id": 2}))
    assert poster.post(HEAD, "lint", "queued", None, OUTPUT, False) == 1
    assert poster.post(HEAD, "lint", "queued", None, OUTPUT, True) == 2
    assert [call.args for call in request.call_args_list] == [("POST", URL), ("POST", URL)]
    assert [call.kwargs["json"]["name"] for call in request.call_args_list] == ["lab/lint", "lab-shadow/lint"]


def test_a_check_run_is_created_queued_then_advanced_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    poster, request, sleep = lab(monkeypatch, answer(201, {"id": 7}), answer(200, {"id": 7}), answer(200, {"id": 7}))
    assert poster.post(HEAD, "lint", "queued", None, OUTPUT, False) == 7
    assert poster.post(HEAD, "lint", "in_progress", None, OUTPUT, False) == 7
    assert poster.post(HEAD, "lint", "completed", "success", OUTPUT, False) == 7
    created, started, finished = request.call_args_list
    shown = {"external_id": "run-7", "details_url": OUTPUT.details, "output": checks.render(OUTPUT)}
    assert created.args == ("POST", URL)
    assert created.kwargs == {
        "headers": HEADERS,
        "json": {"name": "lab/lint", "head_sha": HEAD, "status": "queued", **shown},
        "timeout": 30,
    }
    assert started.args == ("PATCH", f"{URL}/7")
    assert started.kwargs["json"] == {"status": "in_progress", **shown}
    assert finished.args == ("PATCH", f"{URL}/7")
    assert finished.kwargs["json"] == {"status": "completed", "conclusion": "success", **shown}
    sleep.assert_not_called()


@pytest.mark.parametrize(
    ("steps", "status", "conclusion"),
    [
        ((), "queued", None),
        ((), "in_progress", None),
        ((), "completed", "failure"),
        (("queued",), "completed", "neutral"),
        (("in_progress",), "completed", "cancelled"),
        (("queued", "in_progress"), "completed", "action_required"),
        (("queued",), "completed", "skipped"),
        (("queued",), "completed", "timed_out"),
    ],
)
def test_a_check_run_moves_forward_and_may_skip_a_status(
    monkeypatch: pytest.MonkeyPatch, steps: tuple[str, ...], status: str, conclusion: str | None
) -> None:
    poster, request, _ = lab(monkeypatch, *[answer(201, {"id": 7}) for _ in range(len(steps) + 1)])
    for step in steps:
        poster.post(HEAD, "lint", step, None, OUTPUT, False)
    assert poster.post(HEAD, "lint", status, conclusion, OUTPUT, False) == 7
    assert request.call_args.kwargs["json"].get("conclusion") == conclusion


@pytest.mark.parametrize(
    ("steps", "status", "conclusion"),
    [
        ((), "waiting", None),
        ((), "queued", "success"),
        ((), "in_progress", "failure"),
        ((), "completed", None),
        ((), "completed", "stale"),
        (("queued",), "queued", None),
        (("in_progress",), "in_progress", None),
        (("in_progress",), "queued", None),
        (("completed",), "completed", "failure"),
        (("completed",), "in_progress", None),
    ],
)
def test_a_check_run_never_moves_back_and_a_conclusion_comes_only_with_completed(
    monkeypatch: pytest.MonkeyPatch, steps: tuple[str, ...], status: str, conclusion: str | None
) -> None:
    poster, request, _ = lab(monkeypatch, *[answer(201, {"id": 7}) for _ in steps])
    for step in steps:
        poster.post(HEAD, "lint", step, "success" if step == "completed" else None, OUTPUT, False)
    with pytest.raises(ValueError):
        poster.post(HEAD, "lint", status, conclusion, OUTPUT, False)
    assert request.call_count == len(steps)


def test_a_new_grade_run_or_head_opens_a_fresh_check_run(monkeypatch: pytest.MonkeyPatch) -> None:
    poster, request, _ = lab(monkeypatch, answer(201, {"id": 7}), answer(201, {"id": 8}), answer(201, {"id": 9}))
    poster.post(HEAD, "lint", "completed", "failure", OUTPUT, False)
    assert poster.post(HEAD, "lint", "queued", None, replace(OUTPUT, run="run-8"), False) == 8
    assert poster.post("c" * 40, "lint", "queued", None, OUTPUT, False) == 9
    assert [call.args for call in request.call_args_list] == [("POST", URL)] * 3
    assert request.call_args.kwargs["json"]["head_sha"] == "c" * 40


def test_the_output_lists_every_unit_key_the_run_id_the_grader_revision_and_the_details_link() -> None:
    assert checks.render(OUTPUT) == {
        "title": "2 units",
        "summary": (
            "Grading\n\nRun: run-7\n"
            f"Grader revision: {'b' * 40}\n"
            "Details: https://lab.example/grades/run-7\n"
            "Unit keys: 2 of 2 listed"
        ),
        "text": "- static `ruff-1f2e`\n- tests `batch-9c0d`\n",
    }


def test_an_output_over_the_github_limit_keeps_the_full_count_and_points_at_the_full_list() -> None:
    units = tuple(Unit("tests", f"{index:064x}", ()) for index in range(2000))
    rendered = checks.render(replace(OUTPUT, units=units))
    tail = f"The full list of 2000 unit keys is at {OUTPUT.details}"
    line = len(f"- tests `{units[0].key}`\n")
    listed = (65535 - len(tail)) // line
    assert rendered["text"] == "".join(f"- tests `{unit.key}`\n" for unit in units[:listed]) + tail
    assert len(rendered["text"]) <= 65535 < len(rendered["text"]) + line
    assert rendered["summary"].endswith(f"Unit keys: {listed} of 2000 listed")


def test_an_output_exactly_at_the_github_limit_lists_every_key() -> None:
    line = len("- tests `k`\n")
    key = "k" * (65535 - 5 * line + 1)
    units = (Unit("tests", key, ()), *(Unit("tests", "k", ()) for _ in range(4)))
    rendered = checks.render(replace(OUTPUT, units=units))
    assert len(rendered["text"]) == 65535
    assert rendered["summary"].endswith("Unit keys: 5 of 5 listed")


def test_a_batch_posts_only_the_last_transition_of_each_check_run(monkeypatch: pytest.MonkeyPatch) -> None:
    poster, request, _ = lab(monkeypatch, answer(201, {"id": 1}), answer(201, {"id": 2}), answer(200, {"id": 2}))
    assert poster.batch(
        [
            Transition(HEAD, "lint", "queued", None, OUTPUT, False),
            Transition(HEAD, "lint", "queued", None, OUTPUT, True),
            Transition(HEAD, "lint", "in_progress", None, OUTPUT, False),
            Transition(HEAD, "lint", "completed", "success", OUTPUT, False),
        ]
    ) == [1, 2]
    assert poster.batch([Transition(HEAD, "lint", "in_progress", None, OUTPUT, True)]) == [2]
    sent = [(call.args, call.kwargs["json"]["status"]) for call in request.call_args_list]
    assert sent == [(("POST", URL), "completed"), (("POST", URL), "queued"), (("PATCH", f"{URL}/2"), "in_progress")]
    assert request.call_args_list[0].kwargs["json"]["name"] == "lab/lint"
    assert request.call_args_list[1].kwargs["json"]["name"] == "lab-shadow/lint"


@pytest.mark.parametrize(
    "moves",
    [
        [("in_progress", None), ("queued", None)],
        [("queued", None), ("completed", None)],
        [("completed", "success"), ("completed", "failure")],
    ],
)
def test_a_batch_with_a_refused_move_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, moves: list[tuple[str, str | None]]
) -> None:
    poster, request, _ = lab(monkeypatch)
    batch = [Transition(HEAD, "lint", "queued", None, OUTPUT, True)]
    batch += [Transition(HEAD, "lint", status, conclusion, OUTPUT, False) for status, conclusion in moves]
    with pytest.raises(ValueError):
        poster.batch(batch)
    request.assert_not_called()


def test_a_batch_refuses_a_move_back_from_a_status_already_posted(monkeypatch: pytest.MonkeyPatch) -> None:
    poster, request, _ = lab(monkeypatch, answer(201, {"id": 1}))
    poster.post(HEAD, "lint", "in_progress", None, OUTPUT, False)
    with pytest.raises(ValueError):
        poster.batch([Transition(HEAD, "lint", "queued", None, OUTPUT, False)])
    assert request.call_count == 1


@pytest.mark.parametrize(
    ("limited", "wait"),
    [
        (answer(403, text=LIMITED, headers={"x-ratelimit-reset": "1120"}), 121.0),
        (answer(429, text=LIMITED, headers={"x-ratelimit-reset": "1120"}), 121.0),
        (answer(403, text=SECONDARY, headers={"x-ratelimit-reset": "1120"}), 60),
        (answer(403, text=LIMITED), 60),
        (answer(403, text=LIMITED, headers={"x-ratelimit-reset": "900"}), 1.0),
        (answer(403, text=LIMITED, headers={"x-ratelimit-reset": "99999"}), 900),
    ],
)
def test_a_rate_limited_post_waits_like_the_ci_budget_then_posts(
    monkeypatch: pytest.MonkeyPatch, limited: Mock, wait: float
) -> None:
    poster, request, sleep = lab(monkeypatch, limited, answer(201, {"id": 7}))
    assert poster.post(HEAD, "lint", "queued", None, OUTPUT, False) == 7
    sleep.assert_called_once_with(wait)
    assert request.call_count == 2


@pytest.mark.parametrize(
    "refused",
    [
        answer(422, text="Invalid request"),
        answer(403, text="Resource not accessible by integration"),
        answer(500, text=LIMITED),
        answer(404),
    ],
)
def test_a_refusal_outside_a_rate_limit_raises_without_waiting(monkeypatch: pytest.MonkeyPatch, refused: Mock) -> None:
    poster, _, sleep = lab(monkeypatch, refused)
    with pytest.raises(PostRefused) as error:
        poster.post(HEAD, "lint", "queued", None, OUTPUT, False)
    assert str(error.value) == f"POST check-runs answered {refused.status_code}"
    sleep.assert_not_called()


def test_the_backoff_stops_after_four_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    limited = answer(403, text=SECONDARY)
    poster, request, sleep = lab(monkeypatch, *[limited] * 5)
    with pytest.raises(PostRefused):
        poster.post(HEAD, "lint", "queued", None, OUTPUT, False)
    assert sleep.call_count == 4
    assert request.call_count == 5


def test_the_backoff_never_waits_past_fifteen_minutes_for_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    soon = answer(403, text=LIMITED, headers={"x-ratelimit-reset": "1700"})
    late = answer(403, text=LIMITED, headers={"x-ratelimit-reset": "99999"})
    poster, request, sleep = lab(monkeypatch, soon, late, late, answer(201, {"id": 7}))
    with pytest.raises(PostRefused):
        poster.post(HEAD, "lint", "queued", None, OUTPUT, False)
    assert [call.args for call in sleep.call_args_list] == [(701.0,), (199.0,)]
    assert request.call_count == 3
    assert poster.post(HEAD, "lint", "queued", None, OUTPUT, False) == 7
