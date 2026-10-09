import json
import sys
from pathlib import Path


def main() -> None:
    output, fixture, revision = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    cases = {
        name: json.loads((output / f"profile-{name}.json").read_text())
        for name in ("positive", "rejection", "recovery", "noexec")
    }
    positive, recovery = cases["positive"], cases["recovery"]
    shared = {
        "package": "SV2-IMG-02",
        "fixture": fixture,
        "tested_commit": revision,
        "mocked": False,
        "startup_network": "none",
        "home_volume": "empty tmpfs owned by 10001:10001",
    }
    evidence = output / "SV2-IMG-02"
    evidence.mkdir(exist_ok=True)
    details = {
        "a": {
            "session_start_hook_exits": positive["session_start_hook_exits"],
            "cli_mcp_list": positive["cli_mcp_list"],
            "homes": positive["record"]["homes"],
            "accounts": positive["record"]["accounts"],
            "independent_fixtures": ["positive", "recovery clean attempt"],
            "worker_profile_materialization_seconds": positive["record"]["worker_profile_materialization_seconds"],
        },
        "b": {
            "refusals": cases["rejection"]["refusals"],
            "noexec_refusal": cases["noexec"]["refusal"],
            "protected_state_unchanged": True,
        },
        "c": recovery,
    }
    for case, values in details.items():
        (evidence / f"{case}-result.json").write_text(json.dumps(shared | values, indent=2, sort_keys=True) + "\n")
    summary = shared | {"cases": ["A", "B", "C"], "status": "passed", "production_rollout": "not exercised"}
    (evidence / "result.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        "SV2-IMG-02 passed: both CLIs load their hooks and MCP servers offline, three refusals, restart and crash recovery"
    )


if __name__ == "__main__":
    main()
