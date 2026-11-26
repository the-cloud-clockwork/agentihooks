import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.swarm.runtime import codex_plan_mode


def qualify(home: Path) -> dict[str, object]:
    repository = home / "planner-probe-repository"
    repository.mkdir(exist_ok=True)
    report = home / ".agentihooks/swarm/profile-reports/planner-probe.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    forbidden = repository / "forbidden"
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / "planner-probe-codex")}
    command = ["codex", *codex_plan_mode(env), "sandbox", "--permission-profile", "planner"]
    write = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('validated')"
    results = []
    for name, argv in (
        ("command", ["/usr/bin/true"]),
        ("report", [sys.executable, "-c", write, str(report)]),
        ("repository", [sys.executable, "-c", write, str(forbidden)]),
    ):
        result = subprocess.run([*command, *argv], cwd=repository, env=env, capture_output=True, text=True, timeout=30)
        results.append(
            {"case": name, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        )
    passed = (
        results[0]["returncode"] == 0
        and results[1]["returncode"] == 0
        and report.is_file()
        and report.read_text() == "validated"
        and results[2]["returncode"] != 0
        and not forbidden.exists()
    )
    return {"passed": passed, "results": results}


def main() -> int:
    report = qualify(Path.home())
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
