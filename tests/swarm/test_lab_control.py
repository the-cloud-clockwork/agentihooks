"""Tests for the swarm's calls to the CI lab control routes."""

import io
import json
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from scripts.swarm import lab_control

URL = "http://lab:8800"
CONTROL = "http://lab:8801"
ENVIRON = {lab_control.TOKEN: "tok"}
LAB = lab_control.Lab(URL, "tok")


class Reply:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "Reply":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def answering(monkeypatch: pytest.MonkeyPatch, body: object) -> list[tuple[str, str, dict[str, str], Any, float]]:
    asked: list[tuple[str, str, dict[str, str], Any, float]] = []

    def urlopen(request: Request, timeout: float) -> Reply:
        sent = request.data if isinstance(request.data, bytes) else b"null"
        asked.append((request.get_method(), request.full_url, dict(request.header_items()), json.loads(sent), timeout))
        return Reply(json.dumps(body).encode())

    monkeypatch.setattr(lab_control, "urlopen", urlopen)
    return asked


def failing(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def urlopen(request: Request, timeout: float) -> Reply:
        raise error

    monkeypatch.setattr(lab_control, "urlopen", urlopen)


@pytest.mark.feature("panic-reaches-lab")
@pytest.mark.parametrize(
    ("url", "control"),
    [
        (URL, CONTROL),
        (f"{URL}/lab/?x=1", CONTROL),
        ("https://lab.example", "https://lab.example:8801"),
        ("http://[::1]:8800", "http://[::1]:8801"),
    ],
    ids=["port", "path", "no-port", "ipv6"],
)
def test_lab_names_the_control_port_of_the_lab_host_and_the_token_from_the_environment(url: str, control: str) -> None:
    assert lab_control.lab(url, ENVIRON) == lab_control.Lab(control, "tok")


@pytest.mark.feature("panic-reaches-lab")
@pytest.mark.parametrize(
    ("url", "environ", "why"),
    [
        ("", ENVIRON, "the swarm has no lab url"),
        (URL, {}, "CI_LAB_CONTROL_TOKEN is not set on this host"),
        (URL, {lab_control.TOKEN: ""}, "CI_LAB_CONTROL_TOKEN is not set on this host"),
        ("http://lab:port", ENVIRON, "the swarm lab url http://lab:port names no valid port"),
    ],
    ids=["no-url", "no-token", "empty-token", "bad-port"],
)
def test_lab_refuses_a_swarm_without_a_lab_or_a_host_without_a_token(
    url: str, environ: dict[str, str], why: str
) -> None:
    with pytest.raises(lab_control.LabError) as refused:
        lab_control.lab(url, environ)
    assert str(refused.value) == why


@pytest.mark.feature("panic-reaches-lab")
def test_each_order_posts_its_body_with_the_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = answering(monkeypatch, {"ok": True})
    assert lab_control.priority(lab_control.Lab(f"{URL}/", "tok"), ["b", "a", "b"]) == {"ok": True}
    assert lab_control.pause(LAB, ["fix"]) == {"ok": True}
    assert lab_control.resume(LAB) == {"ok": True}
    assert lab_control.resume(lab_control.Lab("http://labX/", "tok")) == {"ok": True}
    headers = {"Authorization": "Bearer tok", "Content-type": "application/json"}
    assert asked == [
        ("POST", f"{URL}/priority", headers, {"heads": ["a", "b"], "class": "hotfix"}, lab_control.TIMEOUT_S),
        ("POST", f"{URL}/pause", headers, {"class": "pull_request", "except": ["fix"]}, lab_control.TIMEOUT_S),
        ("POST", f"{URL}/resume", headers, {}, lab_control.TIMEOUT_S),
        ("POST", "http://labX/resume", headers, {}, lab_control.TIMEOUT_S),
    ]
    assert lab_control.TIMEOUT_S == 3


@pytest.mark.feature("panic-reaches-lab")
@pytest.mark.parametrize(
    ("error", "why"),
    [
        (HTTPError(URL, 401, "Unauthorized", Message(), io.BytesIO()), f"the lab at {URL} refused the control token"),
        (HTTPError(URL, 503, "Unavailable", Message(), io.BytesIO()), f"the lab at {URL} answered 503 on /resume"),
        (URLError("connection refused"), f"the lab at {URL} is unreachable: <urlopen error connection refused>"),
        (TimeoutError("timed out"), f"the lab at {URL} is unreachable: timed out"),
    ],
    ids=["refused", "server-error", "unreachable", "timeout"],
)
def test_a_failed_order_names_why(monkeypatch: pytest.MonkeyPatch, error: Exception, why: str) -> None:
    failing(monkeypatch, error)
    with pytest.raises(lab_control.LabError) as refused:
        lab_control.resume(LAB)
    assert str(refused.value) == why


@pytest.mark.feature("panic-reaches-lab")
@pytest.mark.parametrize("body", [[1], "text"], ids=["list", "text"])
def test_a_reply_that_is_no_json_map_is_refused(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    answering(monkeypatch, body)
    with pytest.raises(lab_control.LabError) as refused:
        lab_control.resume(LAB)
    assert str(refused.value) == f"the lab at {URL} answered /resume with no JSON map"


def test_a_reply_that_is_no_json_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab_control, "urlopen", lambda request, timeout: Reply(b"<html>"))
    with pytest.raises(lab_control.LabError) as refused:
        lab_control.resume(LAB)
    assert str(refused.value) == f"the lab at {URL} answered /resume with no JSON map"


@pytest.mark.feature("merge-priority-reaches-lab")
def test_acknowledge_returns_the_lab_reply_to_a_head_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = {"priority": ["head9"], "class": "hotfix", "expires_ms": 5}
    asked = answering(monkeypatch, reply)
    assert lab_control.acknowledge(URL, "head9", ENVIRON) == reply
    assert [(path, body) for _, path, _, body, _ in asked] == [
        (f"{CONTROL}/priority", {"heads": ["head9"], "class": "hotfix"})
    ]


@pytest.mark.feature("merge-priority-reaches-lab")
def test_acknowledge_returns_why_the_lab_was_not_told(monkeypatch: pytest.MonkeyPatch) -> None:
    failing(monkeypatch, URLError("down"))
    assert lab_control.acknowledge(URL, "head9", ENVIRON) == {
        "error": f"the lab at {CONTROL} is unreachable: <urlopen error down>"
    }
    assert lab_control.acknowledge("", "head9", ENVIRON) == {"error": "the swarm has no lab url"}
