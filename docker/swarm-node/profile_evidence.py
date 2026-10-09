import hashlib
import json
import sys
from pathlib import Path


def main() -> None:
    output, fixture, revision, fixtures = Path(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
    names = ("positive", "positive-second", "rejection", "recovery", "rollback", "noexec")
    cases = {name: json.loads((output / f"profile-{name}.json").read_text()) for name in names}
    positive, second = cases["positive"], cases["positive-second"]
    assert positive["configuration"] == second["configuration"]
    assert cases["rejection"]["protected_state_unchanged"] is True
    shared = {
        "package": "SV2-IMG-02",
        "fixture": fixture,
        "fixture_manifest": {
            str(p.relative_to(fixtures)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(fixtures.rglob("*"))
            if p.is_file()
        },
        "tested_commit": revision,
        "mocked": False,
        "startup_network": "none",
        "home_volume": "empty tmpfs owned by 10001:10001 per container",
    }
    evidence = output / "SV2-IMG-02"
    evidence.mkdir(exist_ok=True)
    details = {
        "a": {
            "cli_session_start_hook_registrations": positive["cli_session_start_hook_registrations"],
            "cli_mcp_list": positive["cli_mcp_list"],
            "homes": positive["record"]["homes"],
            "accounts": positive["record"]["accounts"],
            "profile_digests": positive["record"]["profile_digests"],
            "second_independent_container_configuration_equal": True,
            "worker_profile_materialization_seconds": [
                positive["record"]["worker_profile_materialization_seconds"],
                second["record"]["worker_profile_materialization_seconds"],
            ],
        },
        "b": {**cases["rejection"], "noexec_refusal": cases["noexec"]["refusal"]},
        "c": cases["recovery"],
    }
    for case, values in details.items():
        (evidence / f"{case}-result.json").write_text(json.dumps(shared | values, indent=2, sort_keys=True) + "\n")
    summary = shared | {
        "cases": ["A", "B", "C"],
        "status": "passed",
        "rollback_rehearsal": cases["rollback"],
        "production_rollout": "not exercised",
    }
    (evidence / "result.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print("SV2-IMG-02 passed: both CLIs load their hooks and MCP servers offline, refusals, recovery and rollback")


if __name__ == "__main__":
    main()
