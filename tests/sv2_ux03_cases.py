import copy
import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from scripts.swarm import recovery_view, resume
from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2.artifacts.base import ArtifactRef
from scripts.swarm_v2.checkpoints import model
from scripts.swarm_v2.kubernetes import failures
from tests import sv2_ckp02_cases as ckp02
from tests import sv2_kub05_cases as kub05

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
CHECKPOINTS = FIXTURES / "checkpoints"
INPUTS = (
    "recovery-material.json",
    "pod-failures.json",
    "checkpoints/native-resume.json",
    "checkpoints/context-only.json",
)
SLUG = kub05.SLUG
CASES = {"a": "T-SV2-UX-03-A", "b": "T-SV2-UX-03-B", "c": "T-SV2-UX-03-C"}
EVIDENCE_CLASS = (
    "mocked: checkpoints committed and verified through a granted local artifact store, one with a corrupted native "
    "session object; fakeredis execution registry, fences and recovery records; a fake lease clock; an in process "
    "API server whose Pods carry fixture statuses; the ledger page projection read from the same store; no ledger "
    "HTTP server, browser, worker Pod or cluster runs"
)


def fixture() -> dict:
    return json.loads((FIXTURES / "recovery-material.json").read_text())


def listed(root: Path, spec: dict, compatibility: str) -> dict:
    """The checkpoint listing entry the controller reads: status and continuation come from verification alone."""
    bundle = json.loads((CHECKPOINTS / spec["bundle"]).read_text())
    durable = root / f"{spec['sequence']}-{spec['bundle']}"
    writer = model.Validator(ckp02.store(durable))
    for role, text in bundle["materials"].items():
        writer.store.put(f"{bundle['manifest']['checkpoint_id']}.{role}", text.encode())
    writer.commit(bundle["manifest"])
    if spec.get("corrupt_role"):
        ref = bundle["manifest"]["inventory"][spec["corrupt_role"]]
        writer.store.backend.write(writer.store.scope.key("objects", ref["sha256"]), b"x" * ref["size"])
        assert writer.store.stat(ArtifactRef(ref["sha256"], ref["size"])) != "verified"
    verdict = model.Validator(ckp02.store(durable, execution="e2", generation=3)).verify(
        bundle["manifest"]["checkpoint_id"]
    )
    return {
        "checkpoint_id": f"{bundle['manifest']['checkpoint_id']}-s{spec['sequence']}",
        "sequence": spec["sequence"],
        "status": "complete" if verdict.authoritative else verdict.completeness,
        "compatibility": compatibility,
        "continuation": verdict.continuation,
        "reason": verdict.reason,
    }


class World(kub05.World):
    def __init__(self, data: dict, root: Path) -> None:
        super().__init__()
        self.data, self.attempts = data, {}
        self._clock = self.clocked()
        self._clock.start()
        self.live, _ = self.controller()
        assert self.live.acquire()
        self.recovery_pass = self.recovery(self.live, automatic=False)
        for name, spec in data["attempts"].items():
            attempt = self.launch(self.live, self.fx[spec["run"]], spec["seat"])
            entries = [listed(root, entry, self.fx["compatibility"]) for entry in spec["checkpoints"]]
            self.checkpoints.by_execution[attempt.execution_id] = entries
            self.attempts[name] = attempt

    def close(self) -> None:
        self._clock.stop()

    def fail(self, name: str) -> failures.Decision:
        return self.recovery_pass.handle(self.attempts[name].execution_id, self.data["attempts"][name]["reason"])

    def page(self) -> dict:
        return recovery_view.view(self.client(), SLUG, self.clock[0])

    def card(self, name: str) -> dict:
        seat = self.attempts[name].seat
        return next(card for card in self.page()["cards"] if card["seat"] == seat)

    def click(self, name: str, choice: str) -> str:
        try:
            return resume.decide(
                self.client(), SLUG, self.attempts[name].name, choice, None, self.clock[0], None
            ).reason
        except SwarmError as error:
            return f"refused: {error}"

    def tick(self) -> dict:
        return self.reconcile(self.recovery_pass)

    def protected(self) -> dict:
        names = ("executions", "attempt-fences", "attempt-recoveries", "attempt-restores")
        return {name: self.store.redis.hgetall(self.store.key(SLUG, name)) for name in names}

    def seat_attempts(self, name: str) -> int:
        return len(self.attempts_of(self.data["attempts"][name]["seat"]))

    def attempts_of(self, seat: str) -> list:
        return self.store.executions(SLUG, f"{seat}@{SLUG}")


def _shown(card: dict) -> dict:
    keep = ("badge", "material", "diagnosis", "requested", "accepted", "wait_seconds")
    shown = {key: card[key] for key in keep}
    shown["choices"] = {choice["choice"]: choice["unavailable"] for choice in card["choices"]}
    if shown["accepted"]:
        shown["accepted"] = {**shown["accepted"], "replacement": bool(shown["accepted"]["replacement"])}
    return shown


def _decided(data: dict) -> tuple[dict, World]:
    with tempfile.TemporaryDirectory() as root:
        world = World(data, Path(root))
    for name in data["attempts"]:
        world.fail(name)
    before = {name: _shown(world.card(name)) for name in data["attempts"]}
    clicked = {name: world.click(name, spec["choice"]) for name, spec in data["attempts"].items()}
    requested = {name: _shown(world.card(name))["requested"] for name in data["attempts"]}
    world.clock[0] += data["wait_ms"]
    world.tick()
    restores = {}
    for name in data["attempts"]:
        decision = world.recovery_pass.decision(world.attempts[name].execution_id)
        restores[name] = {
            key: value
            for key, value in world.recovery_pass.restore(decision.replacement).items()
            if key != "session_id"
        }
    after = {name: _shown(world.card(name)) for name in data["attempts"]}
    shown = {
        "before": before,
        "clicked": clicked,
        "requested": requested,
        "restores": restores,
        "after": after,
        "attempts_per_seat": {name: world.seat_attempts(name) for name in data["attempts"]},
        recovery_view.METRIC: world.page()[recovery_view.METRIC],
    }
    return shown, world


def _case_a(data: dict) -> tuple[dict, dict]:
    first, world = _decided(data)
    world.close()
    second, other = _decided(data)
    other.close()
    context, native = first["before"]["context_only"], first["before"]["native"]
    checks = {
        "context_card_names_the_latest_complete_checkpoint_and_watermark": context["material"][:2]
        == ["ckpt-context-0001-s1 · sequence 1 · context", "archive watermark 4096"],
        "native_failure_is_explained": context["choices"]["native"] == failures.NO_NATIVE
        and "passed over ckpt-native-0001-s2: corrupt_object" in context["diagnosis"],
        "three_distinct_choices": set(context["choices"]) == {"native", "context", "fresh"},
        "click_shows_requested_until_the_controller_pass": first["requested"]
        == {"context_only": "context", "native": "native"},
        "context_badge_is_not_green": context["badge"] == recovery_view.CONTEXT_ONLY,
        "native_card_is_recoverable": native["badge"] == recovery_view.RECOVERABLE
        and native["choices"] == {"native": "", "context": "", "fresh": ""},
        "choices_restore_as_chosen": first["restores"]["context_only"]["recovery"] == "reconstructed"
        and first["restores"]["native"]["recovery"] == "native",
        "accepted_decision_shown": first["after"]["context_only"]["accepted"]["choice"] == "context"
        and first["after"]["native"]["accepted"]["choice"] == "native",
        "one_replacement_per_seat": set(first["attempts_per_seat"].values()) == {2},
        "wait_measured": set(first[recovery_view.METRIC].values()) == {data["wait_ms"] // 1000},
        "second_fixture_matches_the_first": second == first,
    }
    return checks, first


def _case_b(data: dict) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory() as root:
        world = World(data, Path(root))
    refused = data["refused_native"]
    world.fail("context_only")
    before = world.protected()
    clicked = world.click("context_only", refused["choice"])
    world.tick()
    after = world.protected()
    card = _shown(world.card("context_only"))
    local = data["local"]
    agent = AgentRecord(
        local["name"], "eng", local["task"], seat=f"{local['seat']}@{SLUG}", conversation_id=local["conversation_id"]
    )
    world.store.put_agent(SLUG, replace(agent, state=resume.AWAITING))
    row = resume.Outcome(local["name"], "eng", local["task"], resume.AWAITING, local["reason"], at=world.clock[0])
    world.store.put_restored(SLUG, [json.loads(json.dumps(row.__dict__))])
    path_only = _shown(next(c for c in world.page()["cards"] if c["agent"] == local["name"]))
    context_click = resume.LOCAL_CONTEXT
    try:
        resume.decide(world.client(), SLUG, local["name"], "context", None, world.clock[0], None)
    except SwarmError as error:
        context_click = f"refused: {error}"
    world.close()
    text = json.dumps([card, path_only])
    checks = {
        "native_request_refused_by_the_controller": card["diagnosis"][-1]
        == f"refused native resume: {failures.NO_NATIVE}",
        "protected_state_unchanged": after == before,
        "seat_still_awaiting_decision": world.occupant().state == resume.AWAITING,
        "no_replacement_admitted": len(world.attempts_of("eng-1")) == 1,
        "path_only_snapshot_is_never_green": path_only["badge"] == recovery_view.PATH_ONLY
        and path_only["badge"] != recovery_view.RECOVERABLE,
        "path_only_limits_named": all(limit in path_only["diagnosis"] for limit in model.PATH_ONLY_LIMITS),
        "local_context_continuation_refused": context_click == f"refused: {resume.LOCAL_CONTEXT}",
        "no_path_or_secret_in_the_page": "/" not in text and "token" not in text.lower(),
    }
    observed = {"clicked": clicked, "card": card, "path_only": path_only, "local_context": context_click}
    return checks, observed


def _case_c(data: dict) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory() as root:
        world = World(data, Path(root))
    world.fail("context_only")
    first = world.click("context_only", "context")
    world.clock[0] += data["wait_ms"]
    world.tick()
    accepted = world.page()
    replay = world.click("context_only", "context")
    again = world.recovery_pass.request(world.attempts["context_only"].execution_id, "context")
    world.clock[0] += data["wait_ms"]
    restarted, _ = world.controller()
    world.clock[0] += failures.lease.ttl_ms()
    assert restarted.acquire()
    world.recovery_pass = world.recovery(restarted, automatic=False)
    world.tick()
    refreshed = world.page()
    world.close()
    card = _shown(next(c for c in refreshed["cards"] if c["seat"] == world.attempts["context_only"].seat))
    checks = {
        "refresh_keeps_the_accepted_decision": card["accepted"]
        == {"choice": "context", "replacement": True, "text": "accepted: context continuation"},
        "choices_disabled_after_acceptance": set(card["choices"].values()) == {recovery_view.ACCEPTED},
        "replayed_click_refused_without_effect": replay.startswith("refused: "),
        "repeated_request_returns_the_same_decision": again.mode == "context",
        "no_second_replacement": len(world.attempts_of("eng-1")) == 2,
        "wait_stays_at_the_decision": accepted[recovery_view.METRIC] == refreshed[recovery_view.METRIC],
    }
    observed = {"first": first, "replay": replay, "card": card, "metric": refreshed[recovery_view.METRIC]}
    return checks, observed


RUNNERS = {"a": _case_a, "b": _case_b, "c": _case_c}


def run_case(case: str) -> dict:
    checks, observed = RUNNERS[case](copy.deepcopy(fixture()))
    return {
        "case": CASES[case],
        "package": "SV2-UX-03",
        "state": "passed" if all(checks.values()) else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "checks": checks,
        "observed": json.loads(json.dumps(observed, default=str)),
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
    }
