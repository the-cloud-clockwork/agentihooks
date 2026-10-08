import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.swarm_v2.auth_context import GrantRefused, LaunchAuthority, launch_grant_rejections_total
from tests.swarm.test_launch_grant import CASES, FIXTURE, KEY, admit, body, protected, world

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evidence/SV2-IDN-04"
EVIDENCE_CLASS = "local isolated fixture on an in-memory Redis with a fake clock; not live rollout proof"
REJECTIONS = (
    "expired",
    "wrong_audience",
    "mismatched_generation",
    "replaced_execution",
    "altered_project_body",
    "other_task",
    "other_brain",
    "other_account",
)
INPUTS = (
    "tests/fixtures/swarm_v2/launch-grant.json",
    "scripts/swarm_v2/auth_context.py",
    "docs/swarm-v2/schemas/launch-grant.json",
    "tests/swarm/test_launch_grant.py",
    "evidence/SV2-IDN-04/generate_case_results.py",
)


def case_a() -> dict:
    runs = []
    for _ in range(2):
        store, _clock, authority = world()
        execution = admit(store, FIXTURE["seat"])
        registration = authority.register(
            "fixture", authority.issue("fixture", execution.execution_id, **FIXTURE["grant"]), body(execution)
        )
        corpus = registration.session_grant()
        runs.append(
            {
                "registered_execution_is_admitted": (registration.execution_id, registration.generation)
                == (execution.execution_id, execution.generation),
                "registration_equals_store": authority.registration("fixture", execution.execution_id) == registration,
                "corpus": registration.project_ids,
                "corpus_refuses_other_project": not corpus.admits("github.com/the-cloud-clockwork/antoncore"),
                "brain": registration.brain_id,
                "issuer": registration.issuer,
                "audience": registration.audience,
                "launch_grant_rejections_total": launch_grant_rejections_total(store, "fixture"),
            }
        )
    passed = all(
        run["registered_execution_is_admitted"]
        and run["registration_equals_store"]
        and run["corpus_refuses_other_project"]
        and run["launch_grant_rejections_total"] == 0
        for run in runs
    )
    return {
        "then": "a correctly launched worker registers only its own execution and authorized corpus",
        "runs": runs,
        "passed": passed,
    }


def attempt(name: str) -> dict:
    mutate, error_class, message = CASES[name]
    store, clock, authority = world()
    execution = admit(store, FIXTURE["seat"])
    token = authority.issue("fixture", execution.execution_id, **FIXTURE["grant"])
    ctx = SimpleNamespace(
        store=store, clock=clock, execution=execution, verifier=authority, slug="fixture", token=token
    )
    ctx.request = body(execution)
    mutate(ctx)
    before = protected(store)
    try:
        ctx.verifier.register(ctx.slug, ctx.token, ctx.request)
        refused = None
    except GrantRefused as error:
        refused = error.detail()
    return {
        "refused": refused and {key: refused[key] for key in ("error_class", "message", "retry")},
        "expected": {"error_class": error_class, "message": message},
        "protected_state_unchanged": protected(store) == before,
        "registrations": store.redis.hlen(store.key("fixture", "launch-registrations")),
        "token_absent_from_error": bool(refused)
        and all(part not in json.dumps(refused) for part in token.split(".")[1:]),
        "launch_grant_rejections_total": launch_grant_rejections_total(store, "fixture"),
    }


def case_b() -> dict:
    results = {name: attempt(name) for name in REJECTIONS}
    passed = all(
        result["refused"]
        and {key: result["refused"][key] for key in ("error_class", "message")} == result["expected"]
        and result["protected_state_unchanged"]
        and result["registrations"] == 0
        and result["token_absent_from_error"]
        and result["launch_grant_rejections_total"] == 1
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
    history = store.executions("fixture", FIXTURE["seat"])
    revoked = restarted.disable("fixture")
    try:
        restarted.register("fixture", outstanding, body(other))
        after_disable = None
    except GrantRefused as error:
        after_disable = str(error)
    result = {
        "retry_returns_original_identity": retried == first,
        "retry_wrote_nothing": retry_wrote_nothing,
        "attempts_for_seat": len(history),
        "stale_attempt_refused": stale,
        "newer_registration_kept": restarted.registration("fixture", newer.execution_id) == current,
        "older_registration_readable": restarted.registration("fixture", execution.execution_id) == first,
        "rollback_revoked": len(revoked),
        "revoked_grant_refused": after_disable,
        "execution_history_unchanged_by_rollback": store.executions("fixture", FIXTURE["seat"]) == history,
        "registrations_kept": store.redis.hlen(store.key("fixture", "launch-registrations")),
    }
    passed = (
        result["retry_returns_original_identity"]
        and result["retry_wrote_nothing"]
        and result["attempts_for_seat"] == 2
        and result["stale_attempt_refused"] == "stale_generation"
        and result["newer_registration_kept"]
        and result["older_registration_readable"]
        and result["rollback_revoked"] == 1
        and result["revoked_grant_refused"] == "launch grant was revoked"
        and result["execution_history_unchanged_by_rollback"]
        and result["registrations_kept"] == 2
    )
    return {
        "then": "a retried registration with the same valid grant returns the original identity without creating another worker",
        "result": result,
        "passed": passed,
    }


def main() -> int:
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *INPUTS], cwd=ROOT).returncode:
        print("commit the case inputs first: results must name the commit that holds them")
        return 2
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    outcomes = {}
    for name, case in (("a", case_a), ("b", case_b), ("c", case_c)):
        result = {"tested_commit": commit, "evidence_class": EVIDENCE_CLASS, **case()}
        outcomes[name] = result["passed"]
        (OUTPUT / f"{name}-result.json").write_text(json.dumps(result, indent=2) + "\n")
    manifest = {
        "tested_commit": commit,
        "evidence_class": EVIDENCE_CLASS,
        "inputs": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in INPUTS},
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(outcomes))
    return 0 if all(outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
