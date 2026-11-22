"""Live check of the expert role profile: the role this installation ships holds the persona, the rule, the two skills
and the conditions an expert launch mounts. It prints one line per part and never an account or a credential."""

import sys
from pathlib import Path

from hooks.context import profile_chain

ROLE = "expert"
CONDITIONS = ".claude/conditions"
PARTS = (
    ("persona", "CLAUDE.md", "# Role: Swarm Expert"),
    ("rule", ".claude/rules/expert-role.md", "agentihooks swarm <slug> expert complete <proof>"),
    ("loop skill", ".claude/skills/swarm-expert/SKILL.md", "name: swarm-expert"),
    ("handoff skill", ".claude/skills/expert-handoff/SKILL.md", "name: expert-handoff"),
    ("stop gate", f"{CONDITIONS}/stop-expert_stop.py", 'main(["expert-stop"])'),
    (
        "identity pin",
        f"{CONDITIONS}/pre-bash.agentihooks+bash.bash+bash.sh+bash.zsh+bash.eval-identity_pin.py",
        'main(["identity"])',
    ),
)
HELD = "the expert role holds its {part}"
LACKS = "FAIL: the expert role lacks its {part}: {path}"


def lines(roles: Path) -> list[str]:
    """One line per part of the expert role under the given package roles folder, failing where its file is missing
    or no longer carries its mark."""
    found: list[str] = []
    for part, path, mark in PARTS:
        file = roles / ROLE / path
        held = file.is_file() and mark in file.read_bytes().decode()
        found.append(HELD.format(part=part) if held else LACKS.format(part=part, path=path))
    return found


def main(roles: Path | None = None) -> int:
    found = lines(profile_chain.PACKAGE_ROLES if roles is None else roles)
    for line in found:
        print(line)
    return int(any(line.startswith("FAIL: ") for line in found))


if __name__ == "__main__":
    sys.exit(main())
