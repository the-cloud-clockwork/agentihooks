"""Carry one real CLI conversation from a source home to a fresh home through a granted checkpoint store."""

import json
import sys
from pathlib import Path

from scripts.swarm_v2.checkpoints import model, native
from tests import sv2_ckp03_cases as cases


def newest(harness: str, home: Path, cwd: Path) -> str:
    if harness == "claude":
        found = sorted((home / "projects" / native.encoded(cwd)).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        return found[-1].stem
    found = sorted((home / "sessions").glob("*/*/*/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)
    files = {"rollout/x": found[-1].read_bytes()}
    return native.Codex().identity(files)[0]


def shown(result) -> dict:
    return json.loads(json.dumps(cases.outcome(result)))


def commit(harness: str, source: Path, source_cwd: Path, durable: Path, version: str) -> dict:
    session = newest(harness, source, source_cwd)
    writer = native.Qualifier(model.Validator(cases.store(durable)))
    captured = writer.capture(harness, source, session, source_cwd, version, stopped=True)
    verdict = cases.commit(writer, captured, f"ckpt-live-{harness}", harness)
    material = captured.material() if isinstance(captured, native.NativeCapture) else b""
    return {
        "session_id": session,
        "capture": shown(captured),
        "committed": cases.shown(verdict),
        "material_names_auth_files": any(name.encode() in material for name in cases.AUTH_FILES[harness]),
        "native_resume_success_by_target": {"capture": writer.metrics()},
    }


def restore(harness: str, fresh: Path, fresh_cwd: Path, durable: Path, version: str) -> dict:
    reader = native.Qualifier(model.Validator(cases.store(durable, execution="e2", generation=3)))
    restored = reader.restore(f"ckpt-live-{harness}", fresh, fresh_cwd, version)
    return {
        "restore": shown(restored),
        "auth_files_in_fresh_home": sorted(p.name for p in fresh.iterdir() if p.name in cases.AUTH_FILES[harness]),
        "native_resume_success_by_target": {"restore": reader.metrics()},
    }


def carry(harness: str, source: Path, source_cwd: Path, fresh: Path, fresh_cwd: Path, durable: Path, version: str):
    committed = commit(harness, source, source_cwd, durable, version)
    restored = restore(harness, fresh, fresh_cwd, durable, version)
    metrics = {**committed["native_resume_success_by_target"], **restored["native_resume_success_by_target"]}
    return {**committed, **restored, "native_resume_success_by_target": metrics}


def main(argv: list[str]) -> int:
    """carry runs both halves in one process; commit and restore split them across a host and a fresh container."""
    step, harness, *paths, version = argv
    paths = list(map(Path, paths))
    halves = {"carry": carry, "commit": commit, "restore": restore}
    print(json.dumps(halves[step](harness, *paths, version), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
