import json
from typing import Any
from urllib.request import Request

import pytest

from scripts.ci_lab import contract
from scripts.swarm import lab_control

pytestmark = pytest.mark.unit


class Reply:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "Reply":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


@pytest.mark.feature("lab-control-routes")
@pytest.mark.parametrize(("environ", "bearer"), [({contract.CONTROL_TOKEN: "w"}, "Bearer w"), ({}, "Bearer ")])
def test_send_posts_json_with_the_lab_token(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], bearer: str
) -> None:
    seen: list[tuple[Any, ...]] = []

    def urlopen(request: Request, timeout: float) -> Reply:
        assert isinstance(request.data, bytes)
        seen.append(
            (
                request.full_url,
                request.get_method(),
                request.get_header("Authorization"),
                request.get_header("Content-type"),
                json.loads(request.data),
                timeout,
            )
        )
        return Reply(b'{"paused": "pull_request"}')

    monkeypatch.setattr(lab_control, "urlopen", urlopen)
    reply = lab_control.send("http://lab/", "/pause", {"class": "pull_request"}, environ)
    assert reply == {"paused": "pull_request"}
    assert seen == [
        ("http://lab/pause", "POST", bearer, "application/json", {"class": "pull_request"}, lab_control.TIMEOUT_S)
    ]


def test_send_refuses_a_reply_that_is_no_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lab_control, "urlopen", lambda request, timeout: Reply(b"[]"))
    with pytest.raises(ValueError) as refused:
        lab_control.send("http://lab", "/resume", {}, {})
    assert str(refused.value) == "the lab answered /resume with no JSON object"


def test_hotfix_returns_the_lab_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def send(url: str, path: str, body: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
        calls.append((url, path, body, environ))
        return {"class": "hotfix", "heads": ["abc"]}

    monkeypatch.setattr(lab_control, "send", send)
    assert lab_control.hotfix("http://lab", "abc", {"K": "v"}) == {"class": "hotfix", "heads": ["abc"]}
    assert calls == [("http://lab", "/priority", {"heads": ["abc"], "class": "hotfix"}, {"K": "v"})]


@pytest.mark.parametrize("error", [OSError("connection refused"), ValueError("not json")])
def test_hotfix_returns_the_error_that_kept_it_from_the_lab(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def send(url: str, path: str, body: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(lab_control, "send", send)
    assert lab_control.hotfix("http://lab", "abc", {}) == {"error": f"lab at http://lab is unreachable: {error}"}


def test_prioritizer_posts_the_head_to_the_lab(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(lab_control, "send", lambda url, path, body, environ: calls.append((url, body, environ)) or {})
    found = lab_control.prioritizer("http://lab", {"K": "v"})
    assert found is not None
    assert found("abc") == {}
    assert calls == [("http://lab", {"heads": ["abc"], "class": "hotfix"}, {"K": "v"})]


def test_prioritizer_is_none_without_a_lab_url() -> None:
    assert lab_control.prioritizer("", {}) is None
