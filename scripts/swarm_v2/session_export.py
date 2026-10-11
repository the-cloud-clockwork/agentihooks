"""The worker image's session exporter: copies the agent's native session into a checkpoint once the supervisor quiesces it."""

import hashlib
import os
import stat
import time
from collections.abc import Mapping
from pathlib import Path

from scripts.swarm_v2.supervision_protocol import acknowledge, context, matches, read, write

SESSIONS = {"claude": ("CLAUDE_CONFIG_DIR", "projects"), "codex": ("CODEX_HOME", "sessions")}
SESSION_ID_LENGTH = 36


def _sessions(root: Path) -> list[Path]:
    found = []
    for folder, _, names in os.walk(root):
        for name in names:
            path = Path(folder) / name
            if name.endswith(".jsonl") and stat.S_ISREG(path.lstat().st_mode):
                found.append(path)
    return sorted(found)


def _copy(raw: bytes, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def export(attempt: Path, scope: dict, environ: Mapping[str, str]) -> dict:
    home = Path(environ["HOME"])
    variable, folder = SESSIONS[home.name]
    root = Path(environ[variable]) / folder
    if root.resolve() != home.resolve() / root.relative_to(home):
        return {"status": "incomplete", "reason": "linked_session_folder"}
    found = _sessions(root)
    transcripts = [path for path in found if "subagents" not in path.relative_to(root).parts]
    if not transcripts:
        return {"status": "incomplete", "reason": "no_native_session"}
    checkpoint = Path("checkpoints") / scope["incarnation"]
    files = []
    for source in found:
        relative = source.relative_to(root)
        raw = source.read_bytes()
        _copy(raw, attempt / checkpoint / "session" / relative)
        files.append({"path": str(relative), "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
    newest = max(transcripts, key=lambda path: path.stat().st_mtime_ns)
    manifest = checkpoint / "manifest.json"
    write(
        attempt / manifest,
        {
            **scope,
            "status": "complete",
            "checkpoint_id": f"ckp-{scope['incarnation']}",
            "harness": home.name,
            "native_session_id": newest.stem[-SESSION_ID_LENGTH:],
            "files": files,
        },
    )
    digest = hashlib.sha256((attempt / manifest).read_bytes()).hexdigest()
    return {"status": "complete", "manifest": str(manifest), "sha256": digest}


def main() -> int:
    root, scope = context()
    supervisor = os.getppid()
    acknowledge("exporter", pid=os.getpid(), status="ready")
    while not matches(quiesced := read(root / "quiesced.json"), scope):
        if os.getppid() != supervisor:
            return 1
        time.sleep(0.05)
    if quiesced.get("status") == "quiesced":
        fields = export(Path.cwd(), scope, os.environ)
    else:
        fields = {"status": "incomplete", "reason": "forced_quiescence"}
    acknowledge("exporter.checkpoint", **fields)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
