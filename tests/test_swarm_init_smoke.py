import os
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_swarm_init_installs_once_with_uv_and_runs_the_installed_cli_twice(tmp_path):
    repo = tmp_path / "repo"
    packaging = repo / "scripts/packaging"
    packaging.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/packaging/swarm-init-smoke.sh", packaging)
    (packaging / "pull-runtime-image.sh").write_text("exit 0\n")
    commands = tmp_path / "commands"
    commands.mkdir()
    calls = tmp_path / "calls"
    cli = tmp_path / "cli"
    cli.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'printf "init %s\\n" "$PWD" >> "$CALLS"\n'
        "printf 'Swarm stack ready: redis, nats, swarm and controller are healthy.\\n"
        "  1. Open the ledger: http://127.0.0.1:8765/\\n'\n"
    )
    cli.chmod(0o755)
    for name, body in {
        "docker": "exit 0\n",
        "python3": 'if [[ "${1:-}" == -m ]]; then mkdir -p "$3/bin"; cp "$CLI" "$3/bin/agentihooks"; printf "#!/usr/bin/env bash\\nexit 0\\n" > "$3/bin/pip"; chmod +x "$3/bin/pip"; fi\n',
        "uv": 'printf "uv %s\\n" "$*" >> "$CALLS"\nif [[ "$1" == venv ]]; then mkdir -p "$4/bin"; cp "$CLI" "$4/bin/agentihooks"; fi\n',
    }.items():
        command = commands / name
        command.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}")
        command.chmod(0o755)
    result = subprocess.run(
        ["bash", str(packaging / "swarm-init-smoke.sh")],
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "CALLS": str(calls),
            "CLI": str(cli),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    recorded = calls.read_text().splitlines()
    assert sum(line.startswith("uv venv --python python3 ") for line in recorded) == 1
    installs = [line for line in recorded if line.startswith("uv pip install ")]
    assert len(installs) == 1
    assert "--python " in installs[0] and installs[0].endswith(" .")
    assert len([line for line in recorded if line.startswith("init ")]) == 2
    for stage in ("package installation", "first init", "second init"):
        assert f"Swarm init smoke {stage}: " in result.stdout
    assert "two runs, one ledger address" in result.stdout


def test_swarm_init_workflow_caches_uv_downloads_before_parallel_scenarios():
    steps = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]["smoke"]["steps"]
    setup = next(step for step in steps if step.get("uses", "").startswith("astral-sh/setup-uv@"))
    assert setup["with"]["enable-cache"] is True
    assert setup["with"]["cache-dependency-glob"] == "pyproject.toml"
    parallel = next(step for step in steps if "swarm-smoke-parallel.sh" in step.get("run", ""))
    assert steps.index(setup) < steps.index(parallel)
