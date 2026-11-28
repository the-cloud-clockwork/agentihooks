"""The swarm priming an opening prompt carries, as trace rows: culture lines, recaps and learned notes.

A spawn writes them beside the prompt; the agent's SessionStart records them
into its injection trace, so a correction can name the seat and note.
"""

import json
from pathlib import Path
from typing import Any


def path(home: Path, slug: str, name: str) -> Path:
    return Path(home) / slug / "prompts" / f"{name}.sources.json"


def rows(slug: str, task: dict[str, Any]) -> list[dict[str, Any]]:
    return list(task.get("withheld") or [])


def withhold(slug: str, task: dict[str, Any]) -> dict[str, Any]:
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
        {**note, "withheld": True}
        if hit("learned", f"learned:{note.get('seat', seat)}#{note.get('number', number)}", note["text"])
        else note
        for number, note in enumerate(task.get("learned") or [], 1)
    ]
    return {**task, "culture": culture, "learned": learned, "withheld": logged}


def write(home: Path, slug: str, name: str, task: dict) -> None:
    target = path(home, slug, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(rows(slug, task)) + "\n")
