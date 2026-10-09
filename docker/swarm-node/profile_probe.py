import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from scripts.swarm_v2 import worker_home

PAYLOAD = {"hook_event_name": "SessionStart", "session_id": "profile-probe", "cwd": "/home/worker", "source": "startup"}
SERVERS = {"claude": ["agentihooks", "fixture-claude-mcp"], "codex": ["agentihooks", "fixture-codex-mcp"]}


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
    docs = {
        "claude": json.loads((homes / "claude/.claude/settings.json").read_text()),
        "claude_mcp": json.loads((homes / "claude/.claude.json").read_text())["mcpServers"],
        "codex": tomllib.loads((homes / "codex/.codex/config.toml").read_text()),
        "codex_hooks": json.loads((homes / "codex/.codex/hooks.json").read_text()),
    }
    return json.loads(json.dumps(docs).replace(str(attempt), "<attempt>"))


def run_hook(command: str, home: Path) -> list:
    env = {**os.environ, "HOME": str(home)}
    done = subprocess.run(
        ["sh", "-c", command],
        input=json.dumps(PAYLOAD),
        text=True,
        capture_output=True,
        env=env,
        cwd="/home/worker",
        timeout=120,
    )
    return [done.returncode, "CONTEXT INJECTION" in done.stdout or "additionalContext" in done.stdout]


def positive(attempt: Path) -> dict:
    record = json.loads(Path("/tmp/record.json").read_text())
    docs = configuration(attempt)
    claude_home, codex_home = attempt / "homes/claude", attempt / "homes/codex"
    claude_hooks = [h["command"] for g in docs["claude"]["hooks"]["SessionStart"] for h in g["hooks"]]
    codex_hooks = [h["command"] for g in docs["codex_hooks"]["hooks"]["SessionStart"] for h in g["hooks"]]
    exits = {
        "claude": [run_hook(c.replace("<attempt>", str(attempt)), claude_home) for c in claude_hooks],
        "codex": [run_hook(c.replace("<attempt>", str(attempt)), codex_home) for c in codex_hooks],
    }
    listed = {target: Path(f"/tmp/{target}-mcp.txt").read_text() for target in SERVERS}
    missing = {t: [s for s in names if s not in listed[t]] for t, names in SERVERS.items()}
    assert record["reused"] is False and all(code == 0 for codes in exits.values() for code, _ in codes), exits
    assert all(any(injected for _, injected in codes) for codes in exits.values()), exits
    assert not any(missing.values()), (missing, listed)
    return {
        "record": record,
        "session_start_hook_exits": exits,
        "cli_mcp_list": listed,
        "claude_hook_commands": len(claude_hooks),
        "codex_hook_commands": len(codex_hooks),
        "configuration": docs,
    }


def rejection(attempts: Path) -> dict:
    before, after = (json.loads(Path(f"/tmp/{name}.json").read_text()) for name in ("before", "after"))
    result = {
        name: {
            "exit": int(Path(f"/tmp/{name}.exit").read_text()),
            "stderr": Path(f"/tmp/{name}.err").read_text().strip(),
        }
        for name in ("workstation", "interpreter")
    }
    assert result["workstation"] == {
        "exit": 1,
        "stderr": "ERROR: claude hook command leaves the execution root: /home/operator/dev/tcc-ecosystem/.venv/bin/python",
    }, result
    assert result["interpreter"] == {
        "exit": 1,
        "stderr": "ERROR: interpreter cannot run agentihooks: /home/operator/dev/tcc-ecosystem/.venv/bin/python",
    }, result
    assert before == after and sorted(p.name for p in attempts.iterdir()) == ["a0"]
    return {"refusals": result, "protected_state_unchanged": True, "attempts": ["a0"]}


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
        json.loads(Path(f"/tmp/{n}.json").read_text()) for n in ("first", "second", "restarted")
    )
    before, after = (json.loads(Path(f"/tmp/{n}.json").read_text()) for n in ("before", "after"))
    clean, recovered = configuration(attempts / "clean"), configuration(attempts / "a2")
    assert second == {**first, "reused": True} and before == after
    assert int(Path("/tmp/crash.exit").read_text()) == 137
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
    refusal = {"exit": int(Path("/tmp/noexec.exit").read_text()), "stderr": Path("/tmp/noexec.err").read_text().strip()}
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
    elif mode == "crash":
        crash(path, sys.argv[3])
    else:
        print(
            json.dumps(
                {"positive": positive, "rejection": rejection, "recovery": recovery, "noexec": noexec}[mode](path),
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
