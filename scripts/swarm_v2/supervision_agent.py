import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from hooks.proc import _process
from scripts.swarm_v2.supervision_protocol import acknowledge, context


def main(argv: list[str] | None = None) -> int:
    command = sys.argv[1:] if argv is None else argv
    root, _ = context()
    if (root / "drain.json").exists():
        acknowledge("agent.exit", exit_code=75)
        return 75
    child = subprocess.Popen(command, stdin=None)

    def forward(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    identity = _process(os.getpid(), Path("/proc"))
    acknowledge("agent", pid=os.getpid(), pid_start=identity.start_time, child_pid=child.pid, status="ready")
    code = child.wait()
    acknowledge("agent.exit", exit_code=code)
    return code if code >= 0 else 128 - code


def native_command(command: tuple[str, ...], directory: Path, environment: dict[str, str]) -> tuple[str, ...]:
    if Path(command[0]).name == "codex":
        trust = f'projects={{{json.dumps(str(directory))}={{trust_level="trusted"}}}}'
        return (command[0], "-c", trust, *command[1:])
    if Path(command[0]).name == "claude":
        from scripts.claude_trust import ensure_trusted

        state, _ = ensure_trusted(directory, environment)
        if state == "untrusted":
            raise ValueError("private execution directory is untrusted")
    return command


if __name__ == "__main__":
    raise SystemExit(main())
