import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm import context_view as cv
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.errors import APIError

pytestmark = pytest.mark.unit

SLUG = "rig"
FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "swarm_v2" / "context-evidence.json").read_text())
NOW = FIXTURE["now_ms"]
TASK = FIXTURE["task"]


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, ".", 3, 0, state="running"))
    cv.record(store, SLUG, TASK, FIXTURE["pack"])
    for ref, evidence in FIXTURE["evidence"].items():
        cv.put_evidence(store, SLUG, ref, evidence)
    return store


def protected(store):
    return {name: store.redis.hgetall(store.key(SLUG, name)) for name in (cv.PACKS, cv.EVIDENCE)}


def test_the_card_names_brain_purpose_receipt_delivery_and_omissions(store):
    [card] = cv.view(store, SLUG, NOW)["cards"]
    assert card["task"] == TASK
    assert card["brain"] == "swarm:rig"
    assert card["purpose"] == "task start"
    assert card["receipt"] == "rcpt-pb10-1"
    assert card["receipt_state"] == cv.FRESH
    assert card["delivery"] == ["claude delivered", "codex unsupported_event"]
    assert card["omitted"] == 1


def test_a_pack_without_a_receipt_shows_none():
    shown = cv.card(TASK, {**FIXTURE["pack"], "receipt": None}, {}, NOW)
    assert (shown["receipt"], shown["receipt_state"]) == ("", "no receipt")


def test_items_carry_trust_inferred_and_superseded_labels_and_hide_other_brains(store):
    items = cv.view(store, SLUG, NOW)["cards"][0]["items"]
    assert items[0] == {
        "ref": "arc:merge-queue",
        "kind": "arc",
        "labels": ["brain synthesis"],
        "summary": "Queue a pull request the moment its own checks pass",
        "unavailable": "",
    }
    assert items[1]["labels"] == ["inferred intent"]
    assert items[2]["labels"] == ["brain synthesis", "superseded by decision:queue-v2"]
    assert items[3]["labels"] == ["transcript evidence"]
    assert items[4] == {
        "ref": "",
        "kind": "session",
        "labels": ["transcript evidence"],
        "summary": "",
        "unavailable": cv.OUTSIDE,
    }


def test_receipt_freshness_turns_expired_at_its_expiry_and_says_when_there_is_none():
    receipt = FIXTURE["pack"]["receipt"]
    assert cv.freshness(receipt, receipt["expires_at_ms"] - 1) == "fresh"
    assert cv.freshness(receipt, receipt["expires_at_ms"]) == "expired"
    assert cv.freshness(None, NOW) == "no receipt"


def test_expanding_an_item_returns_its_record_range_or_lesson_revision(store):
    session = cv.expand(store, SLUG, TASK, "session:s-41")
    assert session == {
        **FIXTURE["evidence"]["session:s-41"],
        "ref": "session:s-41",
        "task": TASK,
        "labels": ["transcript evidence"],
    }
    decision = cv.expand(store, SLUG, TASK, "decision:queue-v1")
    assert (decision["revision"], decision["labels"]) == (1, ["brain synthesis", "superseded by decision:queue-v2"])
    assert cv.failures(store, SLUG) == {}


@pytest.mark.parametrize(
    ("ref", "status", "reason", "text"),
    [
        ("session:p-7", 403, "forbidden_scope", cv.OUTSIDE),
        ("arc:elsewhere", 404, "not_in_pack", cv.NOT_IN_PACK),
    ],
)
def test_a_copied_or_foreign_link_is_refused_without_mutating_state(store, ref, status, reason, text):
    before = protected(store)
    with pytest.raises(cv.Refused) as refused:
        cv.expand(store, SLUG, TASK, ref)
    assert (refused.value.status, refused.value.reason, str(refused.value)) == (status, reason, text)
    assert "private planning notes" not in str(refused.value)
    assert protected(store) == before
    assert cv.failures(store, SLUG) == {reason: 1}


def test_evidence_held_under_another_brain_is_refused_even_when_the_item_names_the_task_brain(store):
    cv.put_evidence(store, SLUG, "arc:merge-queue", {**FIXTURE["evidence"]["arc:merge-queue"], "brain": "personal:x"})
    with pytest.raises(cv.Refused) as refused:
        cv.expand(store, SLUG, TASK, "arc:merge-queue")
    assert (refused.value.status, refused.value.reason) == (403, "forbidden_scope")


def test_an_unknown_task_and_a_missing_source_are_refused():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    with pytest.raises(cv.Refused) as refused:
        cv.expand(store, SLUG, "nope", "arc:merge-queue")
    assert refused.value.reason == "not_in_pack"
    cv.record(store, SLUG, TASK, FIXTURE["pack"])
    with pytest.raises(cv.Refused) as refused:
        cv.expand(store, SLUG, TASK, "arc:merge-queue")
    assert (refused.value.status, refused.value.reason, str(refused.value)) == (410, "unavailable", cv.GONE)
    item = cv.view(store, SLUG, NOW)["cards"][0]["items"][0]
    assert (item["summary"], item["unavailable"]) == ("", cv.GONE)
    assert cv.failures(store, SLUG) == {"not_in_pack": 1, "unavailable": 1}


def test_a_revoked_source_is_unavailable_with_its_reason_and_keeps_no_content(store):
    reason = FIXTURE["revoked"]["session:s-41"]
    cv.revoke(store, SLUG, "session:s-41", reason)
    held = json.loads(store.redis.hget(store.key(SLUG, cv.EVIDENCE), "session:s-41"))
    assert held == {
        "ref": "session:s-41",
        "brain": "swarm:rig",
        "kind": "session",
        "state": "revoked",
        "reason": reason,
    }
    with pytest.raises(cv.Refused) as refused:
        cv.expand(store, SLUG, TASK, "session:s-41")
    assert (refused.value.status, refused.value.reason, str(refused.value)) == (410, "revoked", reason)
    item = cv.view(store, SLUG, NOW)["cards"][0]["items"][3]
    assert item == {
        "ref": "session:s-41",
        "kind": "session",
        "labels": ["transcript evidence"],
        "summary": "",
        "unavailable": reason,
    }
    assert cv.view(store, SLUG, NOW)[cv.METRIC] == {"revoked": 1}


def test_revoking_an_unknown_source_writes_an_empty_tombstone(store):
    cv.revoke(store, SLUG, "arc:never", "gone")
    held = json.loads(store.redis.hget(store.key(SLUG, cv.EVIDENCE), "arc:never"))
    assert held == {"ref": "arc:never", "brain": "", "kind": "", "state": "revoked", "reason": "gone"}


def test_a_source_revoked_before_any_write_expands_as_revoked(store):
    store.redis.hdel(store.key(SLUG, cv.EVIDENCE), "arc:merge-queue")
    cv.revoke(store, SLUG, "arc:merge-queue", "withdrawn")
    with pytest.raises(cv.Refused) as refused:
        cv.expand(store, SLUG, TASK, "arc:merge-queue")
    assert (refused.value.status, refused.value.reason, str(refused.value)) == (410, "revoked", "withdrawn")


def test_the_ledger_api_serves_evidence_and_maps_refusals(store):
    services = SimpleNamespace(swarm=SimpleNamespace(store=lambda: store))
    path = f"swarm/context/{TASK}/arc:merge-queue"
    assert routes.CONTEXT_RE.fullmatch(path)
    assert not routes.CONTEXT_RE.fullmatch(f"swarm/context/{TASK}/a/b")
    handler = SimpleNamespace(path=f"/api/v1/ledgers/{SLUG}/{path}", headers={})
    assert routes.ledger_get(handler, services, SLUG, path)["data"]["revision"] == 3
    with pytest.raises(APIError) as error:
        routes.context_evidence(services, SLUG, TASK, "session:p-7")
    assert (error.value.status, error.value.code, str(error.value)) == (403, "forbidden_scope", cv.OUTSIDE)


def test_the_swarm_view_carries_the_context_projection(store):
    from scripts.swarm import commands

    assert commands.view(store, SLUG)["context"]["cards"][0]["task"] == TASK


def test_a_revocation_is_terminal_and_a_replayed_write_cannot_restore_the_content(store):
    cv.revoke(store, SLUG, "session:s-41", "deleted")
    assert cv.put_evidence(store, SLUG, "session:s-41", FIXTURE["evidence"]["session:s-41"]) is False
    held = json.loads(store.redis.hget(store.key(SLUG, cv.EVIDENCE), "session:s-41"))
    assert (held["state"], held["reason"], "records" in held) == ("revoked", "deleted", False)
    assert cv.put_evidence(store, SLUG, "arc:new", {"brain": "swarm:rig", "kind": "arc", "state": "available"}) is True


def test_an_item_from_another_brain_keeps_its_successor_private():
    item = {**FIXTURE["pack"]["items"][2], "brain": "personal:operator"}
    shown = cv.item_view(item, "swarm:rig", None)
    assert shown == {
        "ref": "",
        "kind": "decision",
        "labels": ["brain synthesis"],
        "summary": "",
        "unavailable": cv.OUTSIDE,
    }


def test_a_ledger_records_only_swarm_brain_packs_and_never_expands_a_personal_one(store):
    with pytest.raises(ValueError, match=cv.NOT_SWARM):
        cv.record(store, SLUG, "p1", {**FIXTURE["pack"], "brain": "personal:operator"})
    assert store.redis.hget(store.key(SLUG, cv.PACKS), "p1") is None
    personal = {**FIXTURE["pack"], "brain": "personal:operator", "items": [FIXTURE["pack"]["items"][4]]}
    store.redis.hset(store.key(SLUG, cv.PACKS), "p1", json.dumps(personal))
    with pytest.raises(cv.Refused) as refused:
        cv.expand(store, SLUG, "p1", "session:p-7")
    assert (refused.value.status, refused.value.reason) == (403, "forbidden_scope")


def test_the_api_decodes_an_encoded_reference(store):
    services = SimpleNamespace(swarm=SimpleNamespace(store=lambda: store))
    path = f"swarm/context/{TASK}/session%3As-41"
    handler = SimpleNamespace(path=f"/api/v1/ledgers/{SLUG}/{path}", headers={})
    assert routes.ledger_get(handler, services, SLUG, path)["data"]["first"] == 120


def test_inspection_off_hides_the_projection_and_refuses_the_read(store, monkeypatch):
    from scripts.swarm import commands

    assert cv.enabled({}) is True
    assert cv.enabled({cv.SWITCH: "off"}) is False
    monkeypatch.setenv(cv.SWITCH, "off")
    assert commands.view(store, SLUG)["context"] is None
    services = SimpleNamespace(swarm=SimpleNamespace(store=lambda: store))
    with pytest.raises(APIError) as error:
        routes.context_evidence(services, SLUG, TASK, "arc:merge-queue")
    assert (error.value.status, error.value.code, str(error.value)) == (404, "resource_missing", cv.OFF)


def test_a_write_racing_a_revocation_retries_and_loses(store, monkeypatch):
    original, calls = cv._held, []

    def racing(pipe, key, ref):
        held = original(pipe, key, ref)
        if not calls:
            calls.append(ref)
            cv.revoke(store, SLUG, ref, "deleted")
        return held

    monkeypatch.setattr(cv, "_held", racing)
    assert cv.put_evidence(store, SLUG, "session:s-41", FIXTURE["evidence"]["session:s-41"]) is False
    held = json.loads(store.redis.hget(store.key(SLUG, cv.EVIDENCE), "session:s-41"))
    assert (held["state"], "records" in held) == ("revoked", False)
