import argparse
import os
import sys
from pathlib import Path

from scripts.swarm_v2 import filesystem
from scripts.swarm_v2.herdr.endpoints import MATERIAL_DIR, PRINCIPALS
from scripts.swarm_v2.kubernetes.spec import ATTEMPTS, IDENTITY, LAUNCH_DIR, LAUNCH_RECORD
from scripts.swarm_v2.supervision import Launch, LaunchRefused

SHELL = "/bin/bash"
KEPT = ("PATH", "TERM", "LANG", "USER", "LOGNAME")


def environment(material: Path, attempts: Path, launch: Path, environ: dict[str, str]) -> dict[str, str]:
    execution_id = (material / PRINCIPALS).read_text().strip()
    if not IDENTITY["execution_id"].fullmatch(execution_id):
        raise LaunchRefused("terminal principal is not an execution id")
    loaded = Launch.load(attempts / execution_id, launch)
    kept = {name: environ[name] for name in KEPT if name in environ}
    return {"TERM": "dumb", **kept, "SHELL": SHELL, **filesystem.environment(loaded.execution, loaded.harness)}


def main(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.herdr.session")
    parser.add_argument("--material", type=Path, default=Path(MATERIAL_DIR))
    parser.add_argument("--attempts", type=Path, default=Path(ATTEMPTS))
    parser.add_argument("--launch", type=Path, default=Path(LAUNCH_DIR) / LAUNCH_RECORD)
    args = parser.parse_args(argv)
    environ = dict(os.environ) if environ is None else environ
    try:
        env = environment(args.material, args.attempts, args.launch, environ)
    except (OSError, ValueError, KeyError) as exc:
        print(f"terminal session refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    command = environ.get("SSH_ORIGINAL_COMMAND")
    os.execve(SHELL, [SHELL, "-c", command] if command else [SHELL, "-l"], env)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
