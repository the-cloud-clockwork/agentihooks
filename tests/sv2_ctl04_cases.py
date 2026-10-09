import hashlib
import json
from pathlib import Path

import pytest

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.kubernetes import watch
from tests.test_swarm_v2_controller import Pods, Transport, agent

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/reconcile-orphans.json"


def inputs():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def pod(spec, execution_id=None):
    if "labels" in spec:
        return watch.Pod(spec["name"], spec["uid"], dict(spec["labels"]))
    owner = spec.get("owner", watch.owner_for("fixture"))
    return watch.Pod(spec["name"], spec["uid"], watch.labels(owner, spec.get("execution_id", execution_id)))


class Rig:
    """One independent fixture: its own fake Redis server, fake clock and fake Pod source."""

    def __init__(self, patch, data, pods):
        import fakeredis

        self.store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
        self.store.create(SwarmConfig(data["swarm"], "agentihooks", 1, 0))
        self.clock = [1000]
        patch.setattr(lease, "now_ms", lambda store: self.clock[0])
        self.transport, self.pods, self.grant = Transport(), Pods(pods), {"allowed": True}

    def controller(self, cleanup=True):
        view = watch.PodView(self.pods)
        return Controller(
            self.store, "fixture", [self.transport], lambda: self.grant["allowed"], pods=view, orphan_cleanup=cleanup
        )

    def launch(self):
        launcher = Controller(self.store, "fixture", [self.transport], lambda: True)
        assert launcher.acquire()
        attempt = launcher.admit(agent(self.store))
        assert launcher.release()
        return attempt.execution_id

    def restart(self, cleanup=True):
        self.clock[0] += lease.TTL_MS
        controller = self.controller(cleanup)
        assert controller.acquire()
        return controller

    def protected(self):
        keys = [self.store.key("fixture", kind) for kind in ("executions", "controller-intents", "runtime-operations")]
        return {"pods": sorted(self.pods.pods), "redis": {key: self.store.redis.hgetall(key) for key in keys}}


def _a(patch, data):
    rig = Rig(patch, data, [])
    execution_id = rig.launch()
    rig.pods = Pods([pod(data["live"], execution_id), pod(data["managed_orphan"]), *map(pod, data["foreign"])])
    first = rig.restart()
    at_restart = first.controller_orphans_by_class()
    plan = first.reconcile()
    second = rig.restart()
    again = second.reconcile()
    assert rig.pods.deleted == [(data["managed_orphan"]["name"], data["managed_orphan"]["uid"])]
    assert plan.matched == again.matched == {execution_id: data["live"]["uid"]}
    assert plan.missing_pods == again.missing_pods == []
    assert rig.transport.creations == 0
    assert data["live"]["uid"] in rig.pods.pods
    return {
        "matched_pods": len(again.matched),
        "deleted": [uid for _, uid in rig.pods.deleted],
        "duplicate_launches": rig.transport.creations,
        "controller_orphans_by_class": at_restart,
        "after_second_restart": second.controller_orphans_by_class(),
    }


def _b(patch, data):
    lookalikes = [pod(data["same_name_lookalike"]), *map(pod, data["foreign"])]
    rig = Rig(patch, data, [*lookalikes, *map(pod, data["ambiguous"])])
    before = rig.protected()
    controller = rig.controller()
    assert controller.acquire()
    plan = controller.reconcile()
    deleted_before = list(rig.pods.deleted)
    unchanged = rig.protected() == before
    unlabelled = {p.uid for p in lookalikes if watch.OWNER_LABEL not in p.labels}
    listed = unlabelled & {p.uid for p in controller.pods.pods()}
    assert deleted_before == [] and plan.delete == [] and unchanged and listed == set()
    corrected = pod({**data["ambiguous"][0], "execution_id": "exec-corrected"})
    rig.pods.pods[corrected.uid] = corrected
    rig.pods.events.append(("MODIFIED", corrected, "corrected"))
    rig.grant["allowed"] = False
    with pytest.raises(SwarmError) as refused:
        controller.reconcile()
    assert rig.pods.deleted == []
    rig.grant["allowed"] = True
    fixed = controller.reconcile()
    assert rig.pods.deleted == [(corrected.name, corrected.uid)]
    assert [p.uid for p in fixed.delete] == [corrected.uid]
    return {
        "deleted_before_correction": [uid for _, uid in deleted_before],
        "protected_state_unchanged": unchanged,
        "unlabelled_lookalikes": sorted(unlabelled),
        "unlabelled_lookalikes_listed": sorted(listed),
        "quarantined": sorted(p.uid for p in plan.quarantine),
        "revoked_grant_refusal": str(refused.value),
        "deleted_after_corrected_labels": [uid for _, uid in rig.pods.deleted],
        "controller_orphans_by_class": controller.controller_orphans_by_class(),
    }


def _c(patch, data):
    rig = Rig(patch, data, [])
    execution_id = rig.launch()
    old, new = pod(data["live"], execution_id), pod(data["incarnation"], execution_id)
    rig.pods = Pods([old])
    controller = rig.restart()
    late = pod(data["late_orphan"])
    rig.pods.pods[late.uid] = late
    rig.pods.expire = True
    relisted = controller.reconcile()
    assert rig.pods.lists == 2 and rig.pods.deleted == [(late.name, late.uid)]
    assert relisted.matched == {execution_id: old.uid}
    rig.pods.pods = {new.uid: new}
    rig.pods.events += [("ADDED", new, "31"), ("DELETED", old, "32")]
    delayed = controller.reconcile()
    assert delayed.matched == {execution_id: new.uid} and delayed.delete == []
    lost = pod({**data["late_orphan"], "uid": "uid-late-lost-ack"})
    rig.pods.pods[lost.uid] = lost
    rig.pods.events.append(("ADDED", lost, "33"))
    rig.pods.interrupt = ConnectionError("watch reset")
    interrupted = controller.reconcile()
    assert rig.pods.lists == 3 and rig.pods.deleted[-1] == (lost.name, lost.uid)
    assert interrupted.matched == {execution_id: new.uid}
    replayed = rig.restart().reconcile()
    assert replayed.matched == delayed.matched and replayed.missing_pods == []
    orphan = pod(data["managed_orphan"])
    rig.pods.pods[orphan.uid] = orphan
    rollback = rig.restart(cleanup=False)
    held = rollback.reconcile()
    assert orphan.uid in rig.pods.pods and held.matched == {execution_id: new.uid}
    assert rig.transport.creations == 0
    return {
        "relists": rig.pods.lists,
        "deleted": [uid for _, uid in rig.pods.deleted],
        "matched_after_delayed_deletion": delayed.matched[execution_id] == new.uid,
        "matched_after_restart": replayed.matched[execution_id] == new.uid,
        "duplicate_launches": rig.transport.creations,
        "rollback_orphan_kept": orphan.uid in rig.pods.pods,
        "rollback_still_matched": held.matched[execution_id] == new.uid,
        "controller_orphans_by_class": rollback.controller_orphans_by_class(),
    }


def run_case(case):
    from pytest import MonkeyPatch

    data = inputs()
    with MonkeyPatch.context() as patch:
        observed = {"a": _a, "b": _b, "c": _c}[case](patch, data)
    return {
        "case": f"T-SV2-CTL-04-{case.upper()}",
        "state": "passed",
        "evidence_class": "mocked Redis, fake clock and fake Pod source; no Kubernetes, autoscaler or EC2 call",
        "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        **observed,
    }
