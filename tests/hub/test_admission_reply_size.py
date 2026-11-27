"""A hub admission carries only what the hive prompt renders, and one too large for the bus never keeps its claim."""

import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts.events import nats, nats_payload
from scripts.events.bus import PayloadTooLarge
from scripts.hive import admission as hive_admission
from scripts.hub import admission, slots
from scripts.swarm import prompt
from tests.hub.test_admission import SLUG, ask
from tests.hub.test_admission import rig as rig

SEAT = f"eng-1@{SLUG}"
REFUSAL = r"the admission answer cannot cross the bus, reply\.subject: payload \d+ exceeds server limit 200"


def hub(rig: SimpleNamespace, limit: int) -> Any:
    """A NATS bus whose server takes payloads up to the limit, serving the hub slots with the rig's admission."""
    opened: Any = object.__new__(nats.NatsBus)
    opened._loop = SimpleNamespace(run=asyncio.run)
    opened._serving = ThreadPoolExecutor(1)
    opened._serve_timing = nats.ServeTiming(nats.SERVE_WORKERS)
    opened._nc = SimpleNamespace(max_payload=limit, subscribe=AsyncMock(), flush=AsyncMock())
    opened.state = lambda bucket: None
    slots.serve(opened, clock=lambda: 7_000, admitter=rig.admitter)
    return opened


def delivered(opened: Any, request: dict[str, Any]) -> SimpleNamespace:
    """The admit request from the request's hive once the hub served it."""
    message = SimpleNamespace(
        subject=f"slots.{SLUG}.{request['hive']}",
        data=json.dumps({"op": "admit", **request}).encode(),
        reply="reply.subject",
        respond=AsyncMock(),
    )
    asyncio.run(opened._nc.subscribe.await_args.kwargs["cb"](message))
    opened._serving.shutdown(wait=True)
    return message


def asked(opened: Any, request: dict[str, Any]) -> bytes:
    """The answer the hub sent for one admit request from the request's hive."""
    message = delivered(opened, request)
    message.respond.assert_awaited_once()
    return message.respond.await_args.args[0]


def offer(rig: SimpleNamespace) -> str | None:
    """The stored admission of the rig's first offer, None once forgotten."""
    return rig.store.redis.get(rig.store.key(SLUG, admission.ADMITTED, "t1", "0"))


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_an_admission_carries_only_the_recaps_and_learned_notes_its_prompt_shows(rig: SimpleNamespace) -> None:
    for number in range(1, 10):
        rig.store.memory.add_recap(SEAT, f"{SLUG}-eng-{number}", "t1", f"recap {number}", number)
    rig.store.memory.learn(SEAT, f"{SLUG}-eng-9", "keep the queue warm", 10)

    task = rig.admitter.admit(SLUG, ask())["task"]

    assert task["seat"] == SEAT
    assert [recap["text"] for recap in task["recaps"]] == ["recap 9", "recap 8", "recap 7", "recap 6"]
    assert task[prompt.RECAPS_TOTAL] == 9
    assert [note["text"] for note in task["learned"]] == ["keep the queue warm"]
    assert "5 older recaps are kept on the seat and not shown." in prompt.priming_lines(task)


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_an_admission_over_the_bus_limit_reaches_the_hive_compressed(rig: SimpleNamespace) -> None:
    admitted = rig.admitter.admit(SLUG, ask())
    whole = json.dumps({"ok": True, "result": admitted}).encode()
    opened = hub(rig, len(whole) - 1)

    sent = asked(opened, ask())

    assert sent.startswith(nats_payload.COMPRESSED)
    opened._nc.request = AsyncMock(return_value=SimpleNamespace(data=sent))
    assert json.loads(opened.request(f"slots.{SLUG}.hive-a", b"{}", 100)) == json.loads(whole)
    assert rig.store.claimant(SLUG, "t1") == admitted["name"]


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_an_admission_that_cannot_cross_the_bus_releases_its_claim_with_a_named_refusal(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    answer = json.loads(asked(hub(rig, 200), ask()))

    reason = answer["result"][hive_admission.HELD]
    assert answer["ok"] is True
    assert re.fullmatch(REFUSAL, reason)
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert (rig.rows["t1"]["state"], rig.rows["t1"]["claimed_by"]) == ("open", "")
    assert offer(rig) is None
    assert f"hub: events admission swarm={SLUG} task=t1 hive=hive-a refused: {reason}" in capsys.readouterr().err


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_an_admission_whose_held_answer_cannot_cross_the_bus_releases_its_claim_and_sends_nothing(
    rig: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger=nats.__name__)

    message = delivered(hub(rig, 50), ask())

    message.respond.assert_not_awaited()
    logged = [record.getMessage() for record in caplog.records if record.name == nats.__name__]
    assert len(logged) == 1
    assert re.fullmatch(r"reply\.subject: payload \d+ exceeds server limit 50", logged[0])
    assert rig.store.claimant(SLUG, "t1") is None
    assert rig.store.agents(SLUG) == []
    assert (rig.rows["t1"]["state"], rig.rows["t1"]["claimed_by"]) == ("open", "")
    assert offer(rig) is None


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_an_unsent_admission_of_another_hive_releases_nothing(
    rig: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    admitted = rig.admitter.admit(SLUG, ask())

    held = rig.admitter.unsent(SLUG, ask(hive="hive-b"), "payload 9 exceeds server limit 8")

    assert held == {hive_admission.HELD: "the admission answer cannot cross the bus, payload 9 exceeds server limit 8"}
    assert rig.store.claimant(SLUG, "t1") == admitted["name"]
    assert [agent.name for agent in rig.store.agents(SLUG)] == [admitted["name"]]
    assert offer(rig) is not None
    assert "task=t1 hive=hive-b refused: the admission answer cannot cross the bus" in capsys.readouterr().err


@pytest.mark.feature("hub-admission-reply-fits-bus")
def test_another_slot_operation_that_cannot_cross_the_bus_answers_its_error(rig: SimpleNamespace) -> None:
    error = PayloadTooLarge("reply.subject: payload 9 exceeds server limit 8")
    request = json.dumps({"op": "launched", **ask()}).encode()

    answer = slots.refusal(f"slots.{SLUG}.hive-a", request, error, rig.admitter)

    assert json.loads(answer) == {"ok": False, "error": "reply.subject: payload 9 exceeds server limit 8"}
