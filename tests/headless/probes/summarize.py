import json
import sys
from pathlib import Path


def events(path: Path) -> list[dict]:
    found = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found


def claude(stream: list[dict]) -> dict:
    init = next((e for e in stream if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in stream if e.get("type") == "result"), {})
    return {
        "model": init.get("model"),
        "tools": init.get("tools"),
        "mcp_servers": init.get("mcp_servers"),
        "is_error": result.get("is_error"),
        "result": result.get("result"),
        "structured_output": result.get("structured_output"),
    }


def codex(stream: list[dict], run: Path) -> dict:
    items = [e.get("item", {}) for e in stream if e.get("type") == "item.completed"]
    last = run / "last.json"
    return {
        "items": [
            {
                k: item.get(k)
                for k in ("type", "message", "command", "exit_code", "status", "changes", "server", "tool")
                if k in item
            }
            for item in items
            if item.get("type") != "agent_message"
        ],
        "errors": [e for e in stream if e.get("type") in ("error", "turn.failed")],
        "last_message": last.read_text() if last.exists() else None,
    }


def main(out: Path) -> None:
    nonce = (out / "work" / "canary.txt").read_text().splitlines()[0]
    print(f"canary first line: {nonce}")
    for run in sorted((out / "runs").iterdir()):
        stream = events(run / "stdout.jsonl")
        hooks = run / "hooks.log"
        body = claude(stream) if run.name.startswith("claude") else codex(stream, run)
        text = json.dumps(body)
        summary = {
            "exit": (run / "exit").read_text().strip(),
            "written.txt": (run / "written").read_text().strip(),
            "hooks fired": hooks.read_text().split() if hooks.exists() else [],
            "mcp server started": (run / "mcp.log").exists(),
            "canary in answer": nonce in text,
            **body,
        }
        stderr = (run / "stderr.txt").read_text(errors="replace").strip()
        if stderr:
            summary["stderr tail"] = stderr.splitlines()[-3:]
        print(f"\n## {run.name}\n{(run / 'command.txt').read_text().strip()}\n{json.dumps(summary, indent=1)}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
