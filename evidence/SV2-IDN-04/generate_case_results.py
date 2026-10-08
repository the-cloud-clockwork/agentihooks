import hashlib
import json
import subprocess
from pathlib import Path

import fakeredis

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.auth_context import (
    GrantRefused,
    LaunchAuthority,
    LaunchKey,
    launch_grant_rejections,
    launch_grant_rejections_total,
)

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evidence/SV2-IDN-04"
FIXTURE_PATH = ROOT / "tests/fixtures/swarm_v2/launch-grant.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text())
KEY = LaunchKey(FIXTURE["key_id"], hashlib.sha256(FIXTURE["key_label"].encode()).digest())
EVIDENCE_CLASS = "local isolated fixture on an in-memory Redis with a fake clock; not live rollout proof"
INPUTS = (
    "tests/fixtures/swarm_v2/launch-grant.json",
    "scripts/swarm_v2/auth_context.py",
    "docs/swarm-v2/schemas/launch-grant.json",
    "tests/swarm/test_launch_grant.py",
)


class Clock:
    def __init__(self):
        self.now = FIXTURE["now"]

    def __call__(self):
        return self.now


def world(audience=FIXTURE["audience"]):
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(FIXTURE["swarm"], "agentihooks", 1, 0))
    clock = Clock()
    return store, clock, LaunchAuthority(store, KEY, FIXTURE["issuer"], audience, clock, FIXTURE["ttl"])


def admit(store, seat, previous=""):
    name = store.execution("fixture", previous).name if previous else store.next_name("fixture", "eng")
    return store.start_execution("fixture", AgentRecord(name, "eng", FIXTURE["task"], seat=seat), previous)


def body(execution, **changes):
    grant = FIXTURE["grant"]
    return {
        "swarm_id": "fixture",
        "execution_id": execution.execution_id,
        "generation": execution.generation,
        "seat_id": execution.seat,
        "task_id": execution.task,
        "account": grant["account"],
        "brain_id": grant["brain_id"],
        "project_ids": grant["project_ids"],
        **changes,
    }


def protected(store):
    return {
        key: store.redis.dump(key) for key in store.redis.scan_iter() if not key.endswith("launch-grant-rejections")
    }


def case_a() -> dict:
    runs = []
    for _ in range(2):
        store, _clock, authority = world()
        execution = admit(store, FIXTURE["seat"])
        token = authority.issue("fixture", execution.execution_id, **FIXTURE["grant"])
        registration = authority.register("fixture", token, body(execution))
        stored = authority.registration("fixture", execution.execution_id)
        corpus = registration.session_grant()
        runs.append(
            {
                "registered_execution_is_admitted_execution": (registration.execution_id, registration.generation)
                == (execution.execution_id, execution.generation),
                "registration_equals_store": stored == registration,
                "seat": registration.seat_id,
                "task": registration.task_id,
                "brain": registration.brain_id,
                "corpus": registration.project_ids,
                "corpus_admits_granted_project": corpus.admits(FIXTURE["grant"]["project_ids"][0]),
                "corpus_refuses_other_project": not corpus.admits("github.com/the-cloud-clockwork/antoncore"),
                "issuer": registration.issuer,
                "audience": registration.audience,
                "registrations": store.redis.hlen(store.key("fixture", "launch-registrations")),
                "launch_grant_rejections_total": launch_grant_rejections_total(store, "fixture"),
            }
        )
    passed = all(
        run["registered_execution_is_admitted_execution"]
        and run["registration_equals_store"]
        and run["corpus_admits_granted_project"]
        and run["corpus_refuses_other_project"]
        and run["registrations"] == 1
        and run["launch_grant_rejections_total"] == 0
        for run in runs
    )
    return {
        "then": "a correctly launched worker registers only its own execution and authorized corpus",
        "runs": runs,
        "passed": passed,
    }


def attempt(name: str) -> dict:
    case = FIXTURE["cases"].get(name, {})
    store, clock, authority = world()
    execution = admit(store, FIXTURE["seat"])
    token = authority.issue("fixture", execution.execution_id, **FIXTURE["grant"])
    verifier, request = authority, body(execution)
    if name == "expired":
        clock.now += case["advance"]
    elif name == "wrong_audience":
        verifier = LaunchAuthority(store, KEY, FIXTURE["issuer"], case["audience"], clock)
    elif name == "mismatched_generation":
        request = body(execution, generation=execution.generation + 1)
    elif name == "superseded_generation":
        admit(store, FIXTURE["seat"], execution.execution_id)
    elif name == "altered_project_body":
        request = body(execution, **case["body"])
    elif name == "other_task":
        request = body(execution, task_id="other-task")
    elif name == "other_brain":
        request = body(execution, brain_id="personal-brain")
    elif name == "other_account":
        request = body(execution, account="other-account")
    before = protected(store)
    try:
        verifier.register("fixture", token, request)
        refused = None
    except GrantRefused as error:
        refused = {"error_class": error.error_class, "message": str(error)}
    return {
        "refused": refused,
        "protected_state_unchanged": protected(store) == before,
        "registrations": store.redis.hlen(store.key("fixture", "launch-registrations")),
        "token_absent_from_message": bool(refused)
        and all(part not in refused["message"] for part in token.split(".")[1:]),
        "launch_grant_rejections": launch_grant_rejections(store, "fixture"),
    }


def case_b() -> dict:
    names = (
        "expired",
        "wrong_audience",
        "mismatched_generation",
        "superseded_generation",
        "altered_project_body",
        "other_task",
        "other_brain",
        "other_account",
    )
    results = {name: attempt(name) for name in names}
    passed = all(
        result["refused"]
        and result["protected_state_unchanged"]
        and result["registrations"] == 0
        and result["token_absent_from_message"]
        and sum(result["launch_grant_rejections"].values()) == 1
        for result in results.values()
    )
    return {
        "then": "changing a body field to another task or brain fails authorization before any registry write",
        "results": results,
        "passed": passed,
    }


def case_c() -> dict:
    store, clock, authority = world()
    execution = admit(store, FIXTURE["seat"])
    token = authority.issue("fixture", execution.execution_id, **FIXTURE["grant"])
    first = authority.register("fixture", token, body(execution))
    clock.now += 60
    restarted = LaunchAuthority(store, KEY, FIXTURE["issuer"], FIXTURE["audience"], clock, FIXTURE["ttl"])
    before = protected(store)
    retried = restarted.register("fixture", token, body(execution))
    retry_wrote_nothing = protected(store) == before
    newer = admit(store, FIXTURE["seat"], execution.execution_id)
    current = restarted.register(
        "fixture", restarted.issue("fixture", newer.execution_id, **FIXTURE["grant"]), body(newer)
    )
    try:
        restarted.register("fixture", token, body(execution))
        stale = None
    except GrantRefused as error:
        stale = error.error_class
    other = admit(store, "eng-2@fixture")
    outstanding = restarted.issue("fixture", other.execution_id, **FIXTURE["grant"])
    revoked = restarted.revoke_outstanding("fixture")
    try:
        restarted.register("fixture", outstanding, body(other))
        after_revoke = None
    except GrantRefused as error:
        after_revoke = str(error)
    result = {
        "retry_returns_original_identity": retried == first,
        "retry_wrote_nothing": retry_wrote_nothing,
        "attempts_for_seat": len(store.executions("fixture", FIXTURE["seat"])),
        "stale_attempt_refused": stale,
        "newer_registration_kept": restarted.registration("fixture", newer.execution_id) == current,
        "older_registration_read_only": restarted.registration("fixture", execution.execution_id) == first,
        "rollback_revoked": len(revoked),
        "revoked_grant_refused": after_revoke,
        "registered_grants_kept": store.redis.hlen(store.key("fixture", "launch-registrations")),
    }
    passed = (
        result["retry_returns_original_identity"]
        and result["retry_wrote_nothing"]
        and result["attempts_for_seat"] == 2
        and result["stale_attempt_refused"] == "stale_generation"
        and result["newer_registration_kept"]
        and result["older_registration_read_only"]
        and result["rollback_revoked"] == 1
        and result["revoked_grant_refused"] == "launch grant was revoked"
        and result["registered_grants_kept"] == 2
    )
    return {
        "then": "a retried registration with the same valid grant returns the original identity without creating another worker",
        "result": result,
        "passed": passed,
    }


def main() -> None:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    for name, case in (("a", case_a), ("b", case_b), ("c", case_c)):
        result = {"tested_commit": commit, "evidence_class": EVIDENCE_CLASS, **case()}
        (OUTPUT / f"{name}-result.json").write_text(json.dumps(result, indent=2) + "\n")
    manifest = {
        "tested_commit": commit,
        "evidence_class": EVIDENCE_CLASS,
        "inputs": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in INPUTS},
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
