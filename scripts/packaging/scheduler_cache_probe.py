from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from scripts.ci_lab import cache, contract, scheduler


def child(name: str) -> int:
    proof = Path("/proof")
    (proof / f"{name}.json").write_text(
        json.dumps({"pid": os.getpid(), "parent": os.getppid(), "session": os.getsid(0)})
    )
    with (proof / f"{name}.activity").open("ab", buffering=0) as stream:
        while True:
            stream.write(b"x")
            time.sleep(0.02)


def breach(environ: Mapping[str, str], _interval: float, _passes: int) -> int:
    proof = Path("/proof")
    processes = [
        subprocess.Popen([sys.executable, __file__, "child", name], start_new_session=detached)
        for name, detached in (("normal", False), ("detached", True))
    ]
    deadline = time.monotonic() + 30
    while not all((proof / f"{name}.activity").exists() for name in ("normal", "detached")):
        if time.monotonic() >= deadline or any(process.poll() is not None for process in processes):
            raise RuntimeError("cache proof children did not start")
        time.sleep(0.02)
    while min((proof / f"{name}.activity").stat().st_size for name in ("normal", "detached")) < 3:
        if time.monotonic() >= deadline:
            raise RuntimeError("cache proof children did not make progress")
        time.sleep(0.02)
    normal = json.loads((proof / "normal.json").read_text())
    detached = json.loads((proof / "detached.json").read_text())
    assert normal["parent"] == detached["parent"] == 1
    assert normal["session"] == os.getsid(0)
    assert detached["session"] == detached["pid"] != normal["session"]
    store = Path(environ["CI_LAB_MIRROR_STORE"])
    (proof / "generation").write_text(str(store.parent))
    (proof / "ready").write_text("both children active")
    (store / "oversize").write_bytes(b"x" * (4 * 1024 * 1024))
    while time.monotonic() < deadline:
        time.sleep(0.02)
    raise RuntimeError("cache policy did not terminate PID one")


def restart(environ: Mapping[str, str], _interval: float, _passes: int) -> int:
    store = Path(environ["CI_LAB_MIRROR_STORE"])
    assert not (store.parent / ".invalid").exists()
    assert list(store.iterdir()) == []
    assert Path(environ[contract.GIT_MIRROR]).is_dir()
    (store / "healthy").write_text("retained")
    print("invalid generation reset before consumers", flush=True)
    return 0


def reused(environ: Mapping[str, str], _interval: float, _passes: int) -> int:
    assert (Path(environ["CI_LAB_MIRROR_STORE"]) / "healthy").read_text() == "retained"
    print("healthy same-Pod cache retained", flush=True)
    return 0


def verify() -> int:
    proof = Path("/proof")
    assert (proof / "ready").read_text() == "both children active"
    generation = Path((proof / "generation").read_text())
    assert (generation / ".invalid").is_file()
    assert (generation / "store" / "oversize").stat().st_size == 4 * 1024 * 1024
    before = [(proof / f"{name}.activity").stat().st_size for name in ("normal", "detached")]
    time.sleep(0.3)
    after = [(proof / f"{name}.activity").stat().st_size for name in ("normal", "detached")]
    assert before == after and min(before) >= 3
    print(json.dumps({"normal_and_detached_activity_stopped": after, "invalid_generation": True}), flush=True)
    return 0


def main(argv: list[str]) -> int:
    mode = argv[0]
    if mode == "child":
        return child(argv[1])
    if mode == "verify":
        return verify()
    assert os.getpid() == 1 and os.getuid() == os.getgid() == 10001
    cache.SCAN_SECONDS = 0.1
    setattr(scheduler, "_run_service", {"breach": breach, "restart": restart, "reused": reused}[mode])
    environ: dict[str, str] = dict(os.environ)
    environ.update(dict.fromkeys(scheduler.GITHUB_APP_SETTINGS, "fixture"))
    return scheduler.main(["--require-cache-policy"], environ)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
