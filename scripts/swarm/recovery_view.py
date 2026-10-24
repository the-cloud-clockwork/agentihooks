"""The ledger page's recovery projection: what each seat waiting on a recovery decision can recover from and which
continuations its authoritative state supports. The page only displays it; a choice is a request that the controller,
or the local restore, checks again against current state before anything starts."""

import json

from scripts.swarm import resume
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_v2.checkpoints import model

MATERIAL = "attempt-recovery-material"
METRIC = "recovery_decision_wait_seconds"
RECOVERABLE, CONTEXT_ONLY, FRESH_ONLY, PATH_ONLY = "recoverable", "context only", "fresh only", "path only"
LABELS = {"native": "native resume", "context": "context continuation", "fresh": "fresh start", "resume": "resume"}
CHOICES = ("native", "context", "fresh")
PENDING = "recovery_pending"
ACCEPTED = "a recovery decision was already accepted"
REQUESTED = "a request is waiting for the controller's next pass"
LOCAL_BLOCKERS = ("no conversation id", "no worktree", "worktree gone")
NO_CHECKPOINT = "no complete compatible checkpoint"
NO_WATERMARK = "a local snapshot records no transcript archive watermark"
LOCAL_NATIVE = "reopening the saved conversation is retried, not verified from a checkpoint"


def view(store: RedisStore, slug: str, now_ms: int) -> dict:
    cards = [*pod_cards(store, slug, now_ms), *local_cards(store, slug, now_ms)]
    return {"cards": cards, METRIC: {card["seat"]: card["wait_seconds"] for card in cards}}


def _choices(unavailable: dict[str, str], held: str, send: dict[str, str]) -> list[dict]:
    return [
        {"choice": choice, "label": LABELS[choice], "send": send[choice], "unavailable": held or unavailable[choice]}
        for choice in CHOICES
    ]


def _badge(unavailable: dict[str, str]) -> str:
    if not unavailable["native"]:
        return RECOVERABLE
    return CONTEXT_ONLY if not unavailable["context"] else FRESH_ONLY


def _seconds(start_ms: int, end_ms: int) -> int:
    return max(0, (end_ms - start_ms) // 1000)


def _accepted(choice: str, replacement: str) -> dict:
    return {"choice": choice, "replacement": replacement, "text": f"accepted: {LABELS[choice]}"}


def _current(store: RedisStore, slug: str, material: dict) -> bool:
    occupant = store.execution_occupants(slug).get(material["seat"])
    replacement = (material["decision"] or {}).get("replacement")
    return occupant is not None and occupant.execution_id in (material["execution_id"], replacement)


def _pod_material(material: dict) -> list[str]:
    checkpoint = material["checkpoint"]
    shown = NO_CHECKPOINT
    if checkpoint:
        shown = f"{checkpoint['checkpoint_id']} · sequence {checkpoint['sequence']} · {checkpoint['continuation']}"
    return [shown, f"archive watermark {material['archive_watermark']}", material["tail"]]


def _pod_diagnosis(material: dict) -> list[str]:
    unavailable, refused = material["choices"], material.get("refused_request")
    found = [f"native resume: {unavailable['native'] or 'available'}"]
    found += [f"passed over {entry['checkpoint_id']}: {entry['diagnosis']}" for entry in material["refused"]]
    if unavailable["native"] and not unavailable["context"]:
        found += list(model.CONTEXT_LIMITS)
    if refused:
        found.append(f"refused {LABELS[refused['choice']]}: {refused['reason']}")
    return found


def pod_card(material: dict, now_ms: int) -> dict:
    decision = material["decision"] or {}
    decided = decision.get("mode", PENDING) != PENDING
    requested = "" if decided else material["requested"]
    held = ACCEPTED if decided else REQUESTED if requested else ""
    end = material.get("decided_at_ms") if decided and material.get("decided_at_ms") else now_ms
    return {
        "seat": material["seat"],
        "agent": material["agent"],
        "task": material["task"],
        "backend": resume.KUBERNETES,
        "reason": material["reason"].replace("_", " "),
        "badge": _badge(material["choices"]),
        "material": _pod_material(material),
        "diagnosis": _pod_diagnosis(material),
        "choices": _choices(material["choices"], held, {choice: choice for choice in CHOICES}),
        "requested": requested,
        "requested_text": f"requested: {LABELS[requested]}" if requested else "",
        "accepted": _accepted(decision["mode"], decision["replacement"]) if decided else None,
        "wait_seconds": _seconds(material["fenced_at_ms"], end),
    }


def pod_cards(store: RedisStore, slug: str, now_ms: int) -> list[dict]:
    found = [json.loads(raw) for raw in store.redis.hvals(store.key(slug, MATERIAL))]
    requests = store.redis.hgetall(store.key(slug, resume.RECOVERY_REQUESTS))
    latest = {}
    for material in sorted(found, key=lambda shown: shown["generation"]):
        if _current(store, slug, material):
            latest[material["seat"]] = {**material, "requested": requests.get(material["execution_id"], "")}
    return [pod_card(latest[seat], now_ms) for seat in sorted(latest)]


def local_card(row: dict, agent: AgentRecord, now_ms: int) -> dict:
    """A local restore has only the snapshot's paths, so its badge stays path only whatever the reopen did."""
    decided = row["outcome"] != resume.AWAITING
    blocker = row["reason"] if row["reason"] in LOCAL_BLOCKERS else ""
    unavailable = {"native": blocker, "context": resume.LOCAL_CONTEXT, "fresh": ""}
    started = row.get("awaited_at") or row["at"]
    chosen = "native" if row["outcome"] == resume.RESUMED else row["outcome"]
    return {
        "seat": agent.seat or agent.name,
        "agent": agent.name,
        "task": agent.task,
        "backend": "local",
        "reason": row["reason"],
        "badge": PATH_ONLY,
        "material": [NO_CHECKPOINT, NO_WATERMARK],
        "diagnosis": [f"native resume: {blocker or LOCAL_NATIVE}", *model.PATH_ONLY_LIMITS],
        "choices": _choices(
            unavailable, ACCEPTED if decided else "", {"native": "native", "context": "", "fresh": "fresh"}
        ),
        "requested": "",
        "requested_text": "",
        "accepted": _accepted(chosen, "") if decided else None,
        "wait_seconds": _seconds(started, row["at"] if decided else now_ms),
    }


def local_cards(store: RedisStore, slug: str, now_ms: int) -> list[dict]:
    agents = {agent.name: agent for agent in store.agents(slug)}
    cards = []
    for row in store.restored(slug):
        agent = agents.get(row["name"])
        waiting = agent is not None and agent.state == resume.AWAITING
        if agent is not None and agent.runtime_backend != resume.KUBERNETES and (waiting or row.get("decided")):
            cards.append(local_card(row, agent, now_ms))
    return sorted(cards, key=lambda card: card["seat"])
