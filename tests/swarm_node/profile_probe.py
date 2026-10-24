import ctypes
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from hooks.proc import processes
from hooks.secrets import redact
from scripts.swarm_v2 import supervision_processes, worker_home
from scripts.targets.codex_hook_trust import trusted_state

SERVERS = {"claude": ["agentihooks", "fixture-claude-mcp"], "codex": ["agentihooks", "fixture-codex-mcp"]}
TMP = Path("/tmp")
REGISTRATION_BUDGET_SECONDS = 10.0
POLL_SECONDS = 0.25
POLL_HOOK_SECONDS = 0.01
HOOK_CHILD_SECONDS = 1.0
PR_SET_CHILD_SUBREAPER = 36
OUTPUT_LIMIT = 4000


def snapshot(root: Path) -> dict:
    return {
        str(p.relative_to(root)): [
            p.lstat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "",
        ]
        for p in sorted(root.rglob("*"))
        if not p.is_symlink()
    }


def configuration(attempt: Path) -> dict:
    homes = attempt / "homes"
    hooks = homes / "codex/.codex/hooks.json"
    docs = {
        "claude": json.loads((homes / "claude/.claude/settings.json").read_text(encoding="utf-8")),
        "claude_mcp": json.loads((homes / "claude/.claude.json").read_text(encoding="utf-8"))["mcpServers"],
        "codex": tomllib.loads((homes / "codex/.codex/config.toml").read_text(encoding="utf-8")),
        "codex_hooks": json.loads(hooks.read_text(encoding="utf-8")),
    }
    state = docs["codex"]["hooks"]["state"]
    assert state == trusted_state(hooks, hooks)
    for entry in state.values():
        entry["trusted_hash"] = "<trusted>"
    return json.loads(json.dumps(docs).replace(str(attempt), "<attempt>"))


def sessions(home: Path) -> dict:
    registered = json.loads((home / ".agentihooks" / "active-sessions.json").read_text(encoding="utf-8"))
    return {session: entry["cwd"] for session, entry in registered.items()}


def first_registration(home: Path, session: str) -> float | None:
    registry = home / ".agentihooks" / "active-sessions.json"
    try:
        before = registry.stat().st_mtime_ns
        registered = session in json.loads(registry.read_text(encoding="utf-8"))
        after = registry.stat().st_mtime_ns
    except (OSError, ValueError):
        return None
    return before / 1e9 if registered and before == after else None


def awaited_registration(
    home: Path,
    session: str,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    budget = REGISTRATION_BUDGET_SECONDS
    deadline = clock() + budget
    while (registered_at := first_registration(home, session)) is None:
        if clock() >= deadline:
            raise AssertionError(f"Codex session {session} never registered within {budget} seconds")
        sleep(POLL_SECONDS)
    return registered_at


def reaper(flag: int) -> None:
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, flag, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")


def hook_descendants(existing: dict) -> dict:
    return {
        pid: item
        for pid, item in supervision_processes.descendants(os.getpid(), processes()).items()
        if existing.get(pid) != item.start_time
    }


def surviving_hook_children(existing: dict, seconds: float = HOOK_CHILD_SECONDS) -> list[str]:
    deadline = time.monotonic() + seconds
    while True:
        new = hook_descendants(existing)
        survivors = {pid: item for pid, item in new.items() if item.state != "Z"}
        if not survivors or time.monotonic() >= deadline:
            break
        time.sleep(POLL_HOOK_SECONDS)
    supervision_processes.send(survivors, signal.SIGKILL)
    deadline = time.monotonic() + 1
    while (children := [pid for pid, item in new.items() if item.ppid == os.getpid()]) and time.monotonic() < deadline:
        for pid in children:
            with suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        time.sleep(POLL_HOOK_SECONDS)
        new = hook_descendants(existing)
    return sorted(f"{pid} {' '.join(item.argv) or item.comm}" for pid, item in survivors.items())


def codex_session_start(home: Path, tmp: Path | None = None) -> dict:
    tmp = tmp or home
    document = json.loads((home / ".codex/hooks.json").read_text(encoding="utf-8"))
    commands = [hook["command"] for group in document["hooks"]["SessionStart"] for hook in group["hooks"]]
    assert commands, "the rendered Codex profile has no SessionStart hook"
    session = "worker-profile-codex"
    payload = tmp / "codex-run.payload"
    payload.write_text(
        json.dumps(
            {"hook_event_name": "SessionStart", "session_id": session, "cwd": str(Path.cwd()), "source": "startup"}
        )
    )
    existing = {pid: item.start_time for pid, item in supervision_processes.living(os.getpid()).items()}
    registered_at = None
    failure = None
    (tmp / "codex-run.started").write_text(str(time.time_ns()))
    started = time.monotonic()
    reaper(1)
    try:
        try:
            with (tmp / "codex-run.txt").open("w") as output:
                for command in commands:
                    with payload.open() as stdin:
                        process = subprocess.Popen(
                            command,
                            shell=True,
                            stdin=stdin,
                            env=os.environ | {"HOME": str(home), "CODEX_HOME": str(home / ".codex")},
                            stdout=output,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                    deadline = time.monotonic() + 60
                    while process.poll() is None:
                        registered_at = registered_at or first_registration(home, session)
                        if time.monotonic() >= deadline:
                            with suppress(ProcessLookupError):
                                os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                            raise subprocess.TimeoutExpired(command, 60)
                        time.sleep(POLL_HOOK_SECONDS)
                    registered_at = registered_at or first_registration(home, session)
                    (tmp / "codex-run.exit").write_text(str(process.returncode))
                    if process.returncode:
                        raise subprocess.CalledProcessError(process.returncode, command)
        except BaseException as error:
            failure = error
        survivors = surviving_hook_children(existing)
    finally:
        reaper(0)
    if survivors:
        raise AssertionError(f"Codex hook children outlived registration: {survivors}") from failure
    if failure is not None:
        raise failure
    registered_at = registered_at or awaited_registration(home, session)
    assert sessions(home) == {session: str(Path.cwd())}
    return {
        "registered_at": registered_at,
        "registration_to_exit_seconds": time.time() - registered_at,
        "elapsed_seconds": time.monotonic() - started,
    }


def harness(target: str, attempt: Path, tmp: Path = TMP) -> dict:
    output = (tmp / f"{target}-run.txt").read_text(encoding="utf-8", errors="replace")
    return {
        "exit": int((tmp / f"{target}-run.exit").read_text(encoding="utf-8")),
        "output": redact(output.replace(str(attempt), "<attempt>"), mode="strict")[-OUTPUT_LIMIT:],
    }


def registration(
    home: Path,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    while True:
        try:
            found, error = sessions(home), None
        except (OSError, ValueError, KeyError) as caught:
            found, error = {}, f"{type(caught).__name__}: {caught}"
        if found or clock() >= deadline:
            return {"sessions": found} | ({"error": error} if error and not found else {})
        sleep(POLL_SECONDS)


def launched(target: str, home: Path, tmp: Path) -> float | None:
    registry = home / ".agentihooks" / "active-sessions.json"
    if not registry.exists():
        return None
    started = int((tmp / f"{target}-run.started").read_text(encoding="utf-8"))
    return round((registry.stat().st_mtime_ns - started) / 1e9, 3)


def diagnose(
    attempt: Path,
    tmp: Path = TMP,
    budget: float = REGISTRATION_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    started = clock()
    observed = {}
    for target in SERVERS:
        home = attempt / "homes" / target
        observed[target] = registration(home, started + budget, clock, sleep) | {
            "registry_written_after_launch_seconds": launched(target, home, tmp),
            "harness": harness(target, attempt, tmp),
        }
    diagnostics = {
        "registration_budget_seconds": budget,
        "waited_seconds": round(clock() - started, 3),
        "harnesses": observed,
    }
    if not all(list(entry["sessions"].values()) == ["/home/worker/work"] for entry in observed.values()):
        print(json.dumps({"missing_hook_registration": diagnostics}, sort_keys=True))
        raise SystemExit("missing hook registration: " + json.dumps(diagnostics, sort_keys=True))
    return diagnostics


def positive(attempt: Path, tmp: Path = TMP) -> dict:
    record = json.loads((tmp / "record.json").read_text(encoding="utf-8"))
    docs = configuration(attempt)
    listed = {target: (tmp / f"{target}-mcp.txt").read_text(encoding="utf-8") for target in SERVERS}
    missing = {t: [s for s in names if s not in listed[t]] for t, names in SERVERS.items()}
    assert record["reused"] is False
    assert not any(missing.values()), (missing, listed)
    codex = codex_session_start(attempt / "homes" / "codex", tmp=tmp)
    diagnostics = diagnose(attempt, tmp)
    return {
        "record": record,
        "session_start_invocation": {"claude": "native CLI", "codex": "rendered hook replay"},
        "cli_session_start_hook_registrations": {
            target: len(entry["sessions"]) for target, entry in diagnostics["harnesses"].items()
        },
        "harness_diagnostics": diagnostics,
        "cli_mcp_list": listed,
        "configuration": docs,
        "codex_probe": codex,
    }


def rejection(attempts: Path) -> dict:
    before, after = (json.loads(Path(f"/tmp/{name}.json").read_text(encoding="utf-8")) for name in ("before", "after"))
    result = {
        name: {
            "exit": int(Path(f"/tmp/{name}.exit").read_text(encoding="utf-8")),
            "stderr": Path(f"/tmp/{name}.err").read_text(encoding="utf-8").strip(),
        }
        for name in ("workstation", "interpreter")
    }
    seconds = {name: round(int(Path(f"/tmp/{name}.ns").read_text(encoding="utf-8")) / 1e9, 3) for name in result}
    assert result["workstation"] == {
        "exit": 1,
        "stderr": "ERROR: claude setting hooks leaves the execution root: /home/operator/dev/tcc-ecosystem/.venv/bin/python",
    }, result
    assert result["interpreter"] == {
        "exit": 1,
        "stderr": "ERROR: interpreter cannot run agentihooks: /home/operator/dev/tcc-ecosystem/.venv/bin/python",
    }, result
    assert before == after and sorted(p.name for p in attempts.iterdir()) == ["a0"]
    return {
        "refusals": result,
        "protected_state_unchanged": before == after,
        "attempts": ["a0"],
        "worker_profile_materialization_seconds": {"outcome": "refused", **seconds},
    }


def rollback(attempts: Path) -> dict:
    v1, v2, restored = (
        json.loads(Path(f"/tmp/{n}.json").read_text(encoding="utf-8")) for n in ("v1", "v2", "rollback")
    )
    before = json.loads(Path("/tmp/before.json").read_text(encoding="utf-8"))
    after = {path: entry for path, entry in snapshot(attempts).items() if not path.startswith("a3")}
    assert v1["profile_digests"] != v2["profile_digests"]
    assert restored["profile_digests"] == v1["profile_digests"]
    assert configuration(attempts / "a3") == configuration(attempts / "a1")
    assert before == after
    return {
        "selected_digests": {
            "first": v1["profile_digests"],
            "revised": v2["profile_digests"],
            "rollback": restored["profile_digests"],
        },
        "rollback_configuration_equals_first": True,
        "existing_homes_preserved": True,
    }


def crash(attempts: Path, attempt: str) -> None:
    render = worker_home.render

    def killed(home: Path, target: str) -> None:
        render(home, target)
        if target == "claude":
            os._exit(137)

    worker_home.render = killed
    worker_home.main(
        [
            "bootstrap",
            f"--root={attempts}",
            f"--attempt={attempt}",
            "--templates=/fixtures",
            "--profile=claude=fixture-claude",
            "--profile=codex=fixture-codex",
            "--account=claude=AH_CC_TOKEN_FIXTURE",
            "--account=codex=AH_CX_TOKEN_FIXTURE",
            "--endpoint=AGENTIHOOKS_LEDGER_URL=http://ledger.swarm.invalid:8765",
        ]
    )


def recovery(attempts: Path) -> dict:
    first, second, restarted = (
        json.loads(Path(f"/tmp/{n}.json").read_text(encoding="utf-8")) for n in ("first", "second", "restarted")
    )
    before, after = (json.loads(Path(f"/tmp/{n}.json").read_text(encoding="utf-8")) for n in ("before", "after"))
    clean, recovered = configuration(attempts / "clean"), configuration(attempts / "a2")
    assert second == {**first, "reused": True} and before == after
    assert int(Path("/tmp/crash.exit").read_text(encoding="utf-8")) == 137
    assert restarted["reused"] is False and recovered == clean
    return {
        "restart_reused": True,
        "restart_files_unchanged": True,
        "crash_exit": 137,
        "recovered_equals_clean": True,
        "enabled_plugins": list(recovered["claude"]["enabledPlugins"]),
        "codex_mcp_servers": sorted(recovered["codex"]["mcp_servers"]),
        "claude_session_start_groups": len(recovered["claude"]["hooks"]["SessionStart"]),
        "worker_profile_materialization_seconds": [
            first["worker_profile_materialization_seconds"],
            restarted["worker_profile_materialization_seconds"],
        ],
    }


def noexec(attempts: Path) -> dict:
    refusal = {
        "exit": int(Path("/tmp/noexec.exit").read_text(encoding="utf-8")),
        "stderr": Path("/tmp/noexec.err").read_text(encoding="utf-8").strip(),
    }
    assert refusal == {
        "exit": 1,
        "stderr": "ERROR: execution root is mounted noexec, so the codex hook wrapper cannot run",
    }, refusal
    assert list(attempts.iterdir()) == []
    return {"refusal": refusal, "attempts": []}


def main() -> None:
    mode, path = sys.argv[1], Path(sys.argv[2])
    if mode == "snapshot":
        print(json.dumps(snapshot(path)))
    elif mode == "codex-session-start":
        codex_session_start(path)
    elif mode == "crash":
        crash(path, sys.argv[3])
    else:
        print(
            json.dumps(
                {
                    "positive": positive,
                    "rejection": rejection,
                    "recovery": recovery,
                    "rollback": rollback,
                    "noexec": noexec,
                }[mode](path),
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
