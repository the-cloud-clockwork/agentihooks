import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from scripts.swarm_v2.checkpoints import model, native, restore
from tests import sv2_ckp03_cases as native_cases

EVIDENCE_CLASS = "mocked controller with real durable artifacts and native materialization"
FIXTURE_INPUTS = {
    "valid_checkpoint": "ckpt-1 captured from the context-only transcript and committed authoritative",
    "corrupt_object": "native session object of ckpt-1 overwritten after commit",
    "version_changed": "unsupported harness version: claude 9.0.0 against a checkpoint captured on 2.1",
    "conversation_mismatch": "native directive naming a session other than the checkpoint's",
    "duplicated_restore_request": "resume requested twice after a controller restart, then fresh",
}


def accepted_native(world, controller):
    from scripts.swarm_v2.kubernetes.replacement import Replacements
    from tests.test_swarm_v2_kubernetes_failures import _Ledger, _Relaunch

    old = world.launch(controller, {**world.fx["first"], "native_session": native_cases.SESSIONS["claude"]})
    world.checkpoints.by_execution[old.execution_id] = [
        {
            "checkpoint_id": "ckpt-1",
            "sequence": 1,
            "status": "complete",
            "compatibility": world.fx["compatibility"],
            "continuation": "native",
        }
    ]
    recovery = world.recovery(controller)
    decision = recovery.handle(old.execution_id, "evicted")
    ledger, launch = _Ledger(), _Relaunch()
    replacements = Replacements(recovery, launch, ledger)
    replacements()
    assert ledger.rows[0]["awaiting"] == "recovery_pending"
    assert ledger.rows[0]["recovery"]["status"] == "accepted"
    return old, recovery, decision, ledger, launch, replacements


def valid(root: Path) -> native.Qualifier:
    writer = native.Qualifier(model.Validator(native_cases.store(root / "durable")))
    cwd = root / "repo"
    home = native_cases.home_for("claude", root, cwd)
    taken = writer.capture(
        "claude", home, native_cases.SESSIONS["claude"], cwd, native_cases.VERSIONS["claude"], stopped=True
    )
    assert native_cases.commit(writer, taken, "ckpt-1", "claude").authoritative
    return writer


def restored(root: Path, directive: restore.Directive, version: str | None = None) -> restore.Restored:
    reader = native.Qualifier(model.Validator(native_cases.store(root / "durable", execution="e2", generation=3)))
    restorer = restore.Restorer(reader)
    return restorer.restore(
        directive, root / "worktree", root / "fresh", root / "fresh-cwd", version or native_cases.VERSIONS["claude"]
    )


def positive(root: Path) -> dict:
    from tests import sv2_kub05_cases as kub

    valid(root)
    result = restored(root, restore.Directive("ckpt-1", "native", native_cases.SESSIONS["claude"], 41))
    world = kub.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        old, recovery, decision, ledger, launch, replacements = accepted_native(world, controller)
        assert recovery.restore_outcomes_by_type()["native"] == 0
        recovery.confirm(decision.replacement, asdict(result))
        replacements()
        assert len(launch.calls) == 1
        assert len(world.attempts()) == 2
        assert ledger.rows[0]["recovery"] == recovery.outcome(decision.replacement)
        assert ledger.rows[0]["recovery"]["status"] == "verified"
        assert ledger.rows[0]["recovery"]["watermark"] == 41
        assert ledger.rows[0]["recovery"]["archive_watermark"] == 4096
        assert ledger.rows[0]["recovery"]["recovery"] == result.recovery
        fence = recovery.fence(old.execution_id)
        assert fence["generation"] == old.generation
        fenced = recovery.store.execution(recovery.slug, old.execution_id)
        return {
            "replacement_count": len(launch.calls),
            "fenced_generation": old.generation,
            "fence_released": fence["released"],
            "fenced_state": fenced.state,
            "ledger_recovery": ledger.rows[0]["recovery"],
            "restored": asdict(result),
            "restore_outcomes_by_type": recovery.restore_outcomes_by_type(),
        }


def negative(root: Path) -> dict:
    results = []
    for cause in ("corrupt_object", "version_changed", "conversation_mismatch"):
        path = root / cause
        writer = valid(path)
        session = native_cases.SESSIONS["claude"] if cause != "conversation_mismatch" else "other-session"
        directive = restore.Directive("ckpt-1", "native", session, 41)
        version = "9.0.0" if cause == "version_changed" else None
        if cause == "corrupt_object":
            doc = json.loads(writer.validator.store.get_range(writer.validator.store.recorded("ckpt-1")))
            ref = doc["inventory"][model.NATIVE_SESSION]
            writer.validator.store.backend.write(writer.validator.store.scope.key("objects", ref["sha256"]), b"corrupt")
        try:
            restored(path, directive, version)
        except restore.RestoreRefused as error:
            assert str(error) == cause
        else:
            raise AssertionError("restore was not refused")
        assert not (path / "fresh").exists()
        assert not (path / "worktree").exists()
        results.append({"cause": cause, "home_unchanged": True, "source_unchanged": True})
    return {"refusals": results, **refused_fresh_outcome()}


def refused_fresh_outcome() -> dict:
    from scripts.swarm.store import SwarmError
    from scripts.swarm_v2.kubernetes import failures
    from tests import sv2_kub05_cases as kub

    world = kub.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        _, recovery, decision, ledger, _, replacements = accepted_native(world, controller)
        names = (failures.FENCES, failures.RESTORES, failures.RECOVERIES, "attempt-restore-outcomes")

        def protected() -> str:
            hashes = {name: recovery.store.redis.hgetall(recovery.store.key(recovery.slug, name)) for name in names}
            return json.dumps({"redis": hashes, "ledger": ledger.rows}, sort_keys=True, default=str)

        before = protected()
        fresh = restore.Restored(restore.FRESH, "", 41)
        try:
            recovery.confirm(decision.replacement, asdict(fresh))
        except SwarmError as error:
            refusal = str(error)
        else:
            raise AssertionError("a fresh outcome was accepted for a native directive")
        replacements()
        assert protected() == before
        assert refusal == "restore outcome does not match the accepted directive"
        return {
            "fresh_outcome_refusal": refusal,
            "ledger_recovery_status": ledger.rows[0]["recovery"]["status"],
            "ledger_awaiting": ledger.rows[0]["awaiting"],
        }


def recovery_case(root: Path) -> dict:
    from scripts.swarm.store import SwarmError
    from tests import sv2_kub05_cases as kub

    world = kub.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        old = world.launch(controller, world.fx["first"])
        recovery = world.recovery(controller, automatic=False)
        recovery.handle(old.execution_id, "oom_killed")
        recovery.pause(True)
        checkpoint = world.checkpoints.list(old.execution_id)
        restarted = world.recovery(controller, automatic=False)
        rollback = {
            "automation_paused": restarted.paused(),
            "checkpoint_retained": world.checkpoints.list(old.execution_id) == checkpoint != [],
            "awaiting_retained": world.occupant().state == "awaiting-decision",
        }
        assert all(rollback.values()), rollback
        first = restarted.request(old.execution_id, "resume")
        directive = restarted.restore(first.replacement)
        attempts = world.attempts()
        repeated = world.recovery(controller).request(old.execution_id, "resume")
        assert repeated == first
        assert repeated.checkpoint == first.checkpoint != ""
        assert world.attempts() == attempts
        assert world.recovery(controller).restore(repeated.replacement) == directive
        try:
            world.recovery(controller).request(old.execution_id, "fresh")
        except SwarmError as error:
            conflicting = str(error)
        else:
            raise AssertionError("a later choice replaced the accepted decision")
        assert world.recovery(controller).decision(old.execution_id) == first
        assert world.attempts() == attempts
        return {
            "accepted_decision_reused": repeated == first,
            "conflicting_choice_refusal": conflicting,
            "checkpoint_reused": repeated.checkpoint == first.checkpoint != "",
            "replacement_count": len(attempts) - 1,
            "restore": directive,
            "rollback": rollback,
        }


def run_case(case: str, root: Path) -> dict:
    functions = {"A": positive, "B": negative, "C": recovery_case}
    first = functions[case](root / "first")
    second = functions[case](root / "second")
    assert first == second
    record = {
        "package": "SV2-CKP-05",
        "case": case,
        "mocked": True,
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {
            native_cases.TRANSCRIPT.name: hashlib.sha256(native_cases.TRANSCRIPT.read_bytes()).hexdigest()
        },
        "fixture_inputs": FIXTURE_INPUTS,
        "independent_fixtures": 2,
        "assertions": first,
        "status": "passed",
    }
    return json.loads(json.dumps(record))
