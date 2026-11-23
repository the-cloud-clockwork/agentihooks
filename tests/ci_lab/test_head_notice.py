"""The signed lab head notice an agent's push sends: its body, signature, timeout, settings and failures."""

import hashlib
import hmac
import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable

import pytest

from scripts.ci_lab import head_notice
from scripts.ci_lab.webhooks import NOTICE_SECRET_ENV

pytestmark = pytest.mark.unit

SIGNER = "notice-signer"
URL = "https://receiver.invalid/lab/head"
REPO = "https://github.com/acme/widgets"
ACTOR = "agenticore-app[bot]"
ENVIRON = {head_notice.URL_ENV: URL, NOTICE_SECRET_ENV: SIGNER, head_notice.ACTOR_ENV: ACTOR}


class Answer:
    """A receiver answer as urlopen gives it: a context manager carrying the HTTP status."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "Answer":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def recording(sent: list[tuple[urllib.request.Request, float]], status: int = 200) -> Callable[..., Answer]:
    """Return an opener that keeps each request and its timeout and answers status."""

    def opener(request: urllib.request.Request, timeout: float) -> Answer:
        sent.append((request, timeout))
        return Answer(status)

    return opener


def raising(error: BaseException) -> Callable[..., Answer]:
    """Return an opener whose send fails with error."""

    def opener(request: urllib.request.Request, timeout: float) -> Answer:
        raise error

    return opener


@pytest.mark.feature("lab-head-notice-push")
@pytest.mark.parametrize(("branch", "named"), [("", {}), ("fix-x", {"branch": "fix-x"})])
def test_a_configured_send_posts_one_notice_signed_over_its_exact_body(branch: str, named: dict[str, str]) -> None:
    sent: list[tuple[urllib.request.Request, float]] = []
    assert head_notice.send(ENVIRON, REPO, "a1", "b0", recording(sent, 202), branch=branch) == 202
    [(request, timeout)] = sent
    assert timeout == 5
    assert request.full_url == URL
    assert request.get_method() == "POST"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {"repo": REPO, "head": "a1", "base": "b0", "actor": ACTOR, **named}
    signed = "sha256=" + hmac.new(SIGNER.encode(), request.data, hashlib.sha256).hexdigest()
    assert request.header_items() == [("X-hub-signature-256", signed)]


@pytest.mark.feature("lab-head-notice-push")
@pytest.mark.parametrize("name", [head_notice.URL_ENV, NOTICE_SECRET_ENV, head_notice.ACTOR_ENV])
@pytest.mark.parametrize("absent", [True, False], ids=["absent", "empty"])
def test_without_a_receiver_a_secret_or_an_actor_nothing_is_sent(name: str, absent: bool) -> None:
    environ = {key: value for key, value in ENVIRON.items() if key != name} if absent else {**ENVIRON, name: ""}
    sent: list[tuple[urllib.request.Request, float]] = []
    assert head_notice.send(environ, REPO, "a1", "b0", recording(sent)) is None
    assert sent == []


FAILURES = [
    urllib.error.HTTPError(URL, 401, "Unauthorized", http.client.HTTPMessage(), None),
    urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
    TimeoutError("timed out"),
    ValueError("unknown url type"),
    http.client.IncompleteRead(b""),
]


@pytest.mark.feature("lab-head-notice-push")
@pytest.mark.parametrize("error", FAILURES, ids=["refused", "unreachable", "timeout", "bad url", "broken answer"])
def test_a_failed_send_is_reported_once_and_never_raised(
    error: BaseException, capsys: pytest.CaptureFixture[str]
) -> None:
    assert head_notice.send(ENVIRON, REPO, "a1", "b0", raising(error)) == 0
    assert capsys.readouterr().err == f"lab head notice for a1 was not accepted: {error!r}; the push stands\n"
