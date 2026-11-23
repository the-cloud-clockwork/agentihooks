"""Live check of the pull request body feature: this checkout ships the template with every recon section, the
protected workflow step that grades the body and the engineer step that fills and checks it. It prints one line per
part and reads only files in the checkout."""

import sys
from pathlib import Path

from scripts import pr_body

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ".github/pull_request_template.md"
SKILL = "profiles/package/roles/engineer/.claude/skills/swarm-engineer/SKILL.md"
PARTS = (
    ("protected body check", ".github/workflows/gate-protected.yml", "python -m scripts.pr_body check --event"),
    ("engineer fill step", SKILL, "agentihooks-pr-body fill"),
    ("engineer check step", SKILL, "agentihooks-pr-body check"),
)
HELD = "the checkout holds its {part}"
LACKS = "FAIL: the checkout lacks its {part}: {path}"


def _holds(root: Path, path: str, mark: str) -> bool:
    file = root / path
    return file.is_file() and mark.encode() in file.read_bytes()


def lines(root: Path) -> list[str]:
    """One line per template section heading and per authoring part, failing where a file is missing or no longer
    carries its mark."""
    sections = [(f"{section} template section", TEMPLATE, f"## {section}\n") for section in pr_body.SECTIONS]
    return [
        HELD.format(part=part) if _holds(root, path, mark) else LACKS.format(part=part, path=path)
        for part, path, mark in (*sections, *PARTS)
    ]


def main(root: Path | None = None) -> int:
    found = lines(ROOT if root is None else root)
    for line in found:
        print(line)
    return int(any(line.startswith("FAIL: ") for line in found))


if __name__ == "__main__":
    sys.exit(main())
