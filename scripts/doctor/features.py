"""Every feature map entry's live check, run against the running swarms: a failed check is a regressed feature."""

import json
import subprocess
import sys
from pathlib import Path

from hooks.secrets import redact
from scripts.swarm.health.findings import Finding

KIND = "feature regressed"
MAP = Path(__file__).resolve().parents[2] / "feature-map.json"
TIMEOUT_S = 120
OUTPUT_LINES = 6
OUTPUT_CHARS = 300
PYTHONS = ("python", "python3")


def entries(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return json.loads(path.read_text())["features"]


def argv(command: list[str]) -> list[str]:
    return [sys.executable if command[0] in PYTHONS else command[0], *command[1:]]


def run(command: list[str], root: Path) -> tuple[str, str]:
    """How the check ended, '' when it passed, and what it printed."""
    try:
        done = subprocess.run(argv(command), cwd=root, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return f"timed out after {TIMEOUT_S} seconds", ""
    except OSError as exc:
        return f"could not run: {type(exc).__name__}: {exc}", ""
    return ("" if done.returncode == 0 else f"exited {done.returncode}"), done.stdout + done.stderr


def _output(text: str) -> tuple[str, ...]:
    lines = [" ".join(redact(line, mode="strict").split())[:OUTPUT_CHARS] for line in text.splitlines()]
    return tuple(f"output: {line}" for line in lines if line)[-OUTPUT_LINES:]


def _finding(entry: dict, ended: str, output: str) -> Finding:
    return Finding(
        KIND,
        entry["id"],
        f"feature {entry['id']} regressed: {entry['behaviour']}",
        (
            f"operator words: {entry['operator_words']}",
            f"ordered {entry['ordered_at']} in {entry['ledger_item']}",
            f"live check {' '.join(entry['live_check'])} {ended}",
            *_output(output),
        ),
        "a feature map live check that does not exit zero",
        1,
    )


def findings(path: Path | None = None) -> list[Finding]:
    path = path or MAP
    found = []
    for entry in entries(path):
        ended, output = run(entry["live_check"], path.parent)
        if ended:
            found.append(_finding(entry, ended, output))
    return found
