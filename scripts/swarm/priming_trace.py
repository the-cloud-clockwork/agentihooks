"""The swarm priming an opening prompt carries, as trace rows: culture lines, recaps and learned notes.

A spawn writes them beside the prompt; the agent's SessionStart records them
into its injection trace, so a correction can name the seat and note.
"""

import json
from pathlib import Path

from scripts.swarm.prompt import OLDER_RECAPS


def path(home: Path, slug: str, name: str) -> Path:
    return Path(home) / slug / "prompts" / f"{name}.sources.json"


def _row(layer, source, locator, text):
    return {"layer": layer, "source": source, "locator": locator, "text": text}


def rows(slug: str, task: dict) -> list[dict]:
    seat = task.get("seat", "")
    found = [
        _row("culture", f"culture:{slug}#{number}", {"swarm": slug, "line": number}, line)
        for number, line in enumerate((task.get("culture") or "").splitlines(), 1)
        if line.strip()
    ]
    recaps = task.get("recaps") or []
    for shown, recap in enumerate(recaps[: 1 + OLDER_RECAPS]):
        number = len(recaps) - shown
        found.append(_row("recap", f"recap:{seat}#{number}", {"seat": seat, "recap": number}, recap["text"]))
    for number, note in enumerate(task.get("learned") or [], 1):
        if note["maturity"] != "data" and not note.get("withheld"):
            found.append(_row("learned", f"learned:{seat}#{number}", {"seat": seat, "note": number}, note["text"]))
    return found + list(task.get("withheld") or [])


def withhold(slug: str, task: dict) -> dict:
    """The task with culture lines and learned notes under a confirmed correction taken out, and a row for each."""
    from hooks.context import quarantine

    current = quarantine.mode()
    held = quarantine.index() if current != "off" else []
    if not held:
        return task
    seat, logged = task["seat"], []

    def hit(layer, source, text):
        found = quarantine.match(held, [source], text)
        if found is not None:
            logged.append(quarantine.withheld_row(layer, source, found, current))
        return found is not None and current == "enforce"

    culture = "".join(
        "\n" if line.strip() and hit("culture", f"culture:{slug}#{number}", line) else line
        for number, line in enumerate((task.get("culture") or "").splitlines(keepends=True), 1)
    )
    learned = [
        {**note, "withheld": True} if hit("learned", f"learned:{seat}#{number}", note["text"]) else note
        for number, note in enumerate(task.get("learned") or [], 1)
    ]
    return {**task, "culture": culture, "learned": learned, "withheld": logged}


def write(home: Path, slug: str, name: str, task: dict) -> None:
    target = path(home, slug, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rows(slug, task)) + "\n")
