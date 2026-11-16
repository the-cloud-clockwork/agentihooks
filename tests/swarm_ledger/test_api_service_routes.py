"""The ledger API service's write routes run in process: body length bounds, the bin's swarm stop, the artifact check,
the operator's verdict and terminate controls and the upkeep cadence."""

import io
import json
import threading
from collections.abc import Callable
from email.message import Message
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.swarm_ledger import api_service

SLUG = "rig"
MAX_BODY: int = api_service.server.MAX_BODY
UNKNOWN_ACTION = "action must be start, pause, stop, stop_now, close, reopen, set or verdict"
BIN_ACTION = "action must be delete or restore"
Sent = list[tuple[int, str, str]]


def handler(
    monkeypatch: pytest.MonkeyPatch, body: bytes, headers: dict[str, str]
) -> tuple[api_service.WriteHandler, Sent]:
    """A write handler reading ``body`` under ``headers``, admitted on an existing ledger, and the replies it sends."""
    built = api_service.WriteHandler.__new__(api_service.WriteHandler)
    sent: Sent = []
    message = Message()
    for name, value in headers.items():
        message[name] = value
    built.headers = message
    built.rfile = io.BytesIO(body)

    def send(code: int, text: str, ctype: str) -> None:
        sent.append((code, text, ctype))

    def exists(slug: str) -> bool:
        return slug == SLUG

    def refused(slug: str | None = None) -> bool:
        return False

    monkeypatch.setattr(built, "send", send)
    monkeypatch.setattr(built, "exists", exists)
    monkeypatch.setattr(built, "refused", refused)
    return built, sent


def padded(text: str) -> bytes:
    """``text`` padded with spaces to the largest body a write route reads."""
    return text.encode().ljust(MAX_BODY)


def page(body: bytes) -> dict[str, str]:
    """The headers the operator's page sends with a JSON ``body``."""
    return {
        "Origin": sorted(api_service.server.ALLOWED_ORIGINS)[0],
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }


@pytest.mark.parametrize("body", [padded("[]"), b""])
def test_a_swarm_control_of_the_largest_or_no_body_is_read_and_refused_for_its_action(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    built, sent = handler(monkeypatch, body, page(body))
    built.put_swarm(SLUG)
    assert sent == [(400, UNKNOWN_ACTION, "text/plain")]


def test_a_swarm_control_reads_only_the_length_it_declares(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"action": "quota_refresh"}).encode()
    built, sent = handler(monkeypatch, body + b"GET /next HTTP/1.1", page(body))

    def refreshed(slug: str) -> tuple[dict[str, str], str]:
        return {"refreshed": slug}, ""

    monkeypatch.setattr(api_service, "refresh_quota", refreshed)
    built.put_swarm(SLUG)
    assert sent == [(200, json.dumps({"refreshed": SLUG}), "application/json")]


@pytest.mark.parametrize("body", [padded("[]"), b""])
def test_a_bin_request_of_the_largest_or_no_body_is_read_and_refused_for_its_action(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    built, sent = handler(monkeypatch, body, page(body))
    built.post_bin()
    assert sent == [(400, BIN_ACTION, "text/plain")]


def test_binning_a_ledger_stops_the_swarm_running_on_it(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"action": "delete", "slug": SLUG}).encode()
    built, sent = handler(monkeypatch, body, page(body))
    deleted: list[str] = []
    stopped: list[tuple[str, list[str]]] = []

    def status(slug: str, state: object = None) -> dict[str, str] | None:
        return {"state": "running"} if slug == SLUG else None

    def control(slug: str, argv: list[str], command: str = "swarm") -> tuple[None, str]:
        stopped.append((slug, argv))
        return None, ""

    def entries() -> list[str]:
        return [SLUG]

    monkeypatch.setattr(api_service.ledger_bin, "delete", deleted.append)
    monkeypatch.setattr(api_service.ledger_bin, "entries", entries)
    monkeypatch.setattr(api_service.server, "swarm_status", status)
    monkeypatch.setattr(api_service, "swarm_control", control)
    built.post_bin()
    assert (deleted, stopped) == ([SLUG], [(SLUG, ["stop", "--now"])])
    assert sent == [(200, json.dumps({"binned": [SLUG]}), "application/json")]


def test_a_page_save_of_the_largest_body_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    body = padded("[]")
    built, sent = handler(monkeypatch, body, page(body))

    def document(slug: str) -> dict[str, list[Any]]:
        return {"tasks": []}

    monkeypatch.setattr(api_service.server, "repository", SimpleNamespace(get_document=document))
    built.put_ledger(SLUG)
    assert sent == [(400, 'body must be {"changes": [...], "ops": [...]}: body must be an object', "text/plain")]


def test_an_artifact_upload_is_checked_as_an_artifact_add_by_its_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    built, _ = handler(
        monkeypatch,
        b"",
        {"X-Artifact-Name": "plan.md", "X-Artifact-Request": json.dumps({"task": "t1"}), "X-Ledger-Agent": "eng@a"},
    )
    stores: list[Callable[[bytes], dict[str, Any]]] = []
    checked: list[dict[str, Any]] = []

    def receive(
        slug: str,
        limit: int,
        store: Callable[[bytes], dict[str, Any]],
        agents_only: bool = False,
        operator_plan: bool = False,
    ) -> None:
        stores.append(store)

    def check_add(op: dict[str, Any]) -> None:
        checked.append(op)
        raise ValueError("checked")

    monkeypatch.setattr(built, "receive", receive)
    monkeypatch.setattr(api_service.ledger_artifacts, "check_add", check_add)
    built.post_artifact(SLUG)
    with pytest.raises(api_service.ledger_media.Refused, match="^checked$"):
        stores[0](b"# Plan")
    assert checked == [
        {
            "op": "artifact_add",
            "by": "eng@a",
            "task": "t1",
            "id": "upload",
            "title": "",
            "file": {"id": "0" * 64 + ".md"},
        }
    ]


def test_terminating_an_agent_answers_the_status_of_its_own_swarm(monkeypatch: pytest.MonkeyPatch) -> None:
    submitted: list[tuple[object, ...]] = []
    store = SimpleNamespace(agents=lambda slug: [SimpleNamespace(name="eng@a")])

    def swarm_store() -> SimpleNamespace:
        return store

    def submit(*args: object) -> None:
        submitted.append(args)

    def status(slug: str, state: object = None) -> dict[str, str]:
        return {"slug": slug}

    monkeypatch.setattr(api_service.server, "swarm_store", swarm_store)
    monkeypatch.setattr(api_service.ports.swarm().commands, "submit", submit)
    monkeypatch.setattr(api_service.server, "swarm_status", status)
    assert api_service.terminate_control(SLUG, "eng@a") == ({"slug": SLUG}, "")
    assert submitted == [(store, SLUG, "swarm", ["terminate", "eng@a"])]


def test_an_unknown_verdict_is_refused_naming_every_verdict() -> None:
    verdicts = [str(verdict) for verdict in api_service.ports.swarm().verdicts.VERDICTS]
    with pytest.raises(ValueError) as refused:
        api_service.verdict_argv({"id": "stale-claim/t1", "verdict": "maybe"})
    assert str(refused.value) == f"verdict must be one of {', '.join(verdicts)}"


def test_the_upkeep_waits_its_interval_after_each_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    stopping = threading.Event()
    waited: list[float] = []

    def idle() -> None:
        return None

    def wait(interval: float) -> None:
        waited.append(interval)
        stopping.set()

    monkeypatch.setattr(api_service.ledger_bin, "tidy", idle)
    monkeypatch.setattr(api_service, "bin_closed_without_swarm", idle)
    monkeypatch.setattr(api_service, "expire_alerts", idle)
    monkeypatch.setattr(api_service, "unrelayed", dict)
    monkeypatch.setattr(api_service.server, "scheduled_wait", wait)
    api_service.upkeep(stopping, 7.5)
    assert waited == [7.5]


def test_the_write_handler_hands_its_request_address_and_server_to_the_http_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handed: list[tuple[object, object, object]] = []

    def init(self: object, request: object, client_address: object, owner: object) -> None:
        handed.append((request, client_address, owner))

    monkeypatch.setattr(api_service.server.Handler, "__init__", init)
    request, address, owner = object(), ("10.0.0.1", 4242), object()
    cast(Any, api_service.WriteHandler)(request, address, owner)
    assert handed == [(request, address, owner)]
