"""Venv Guard — blocks `uv run|sync --active` against a venv the project does not own.

`--active` makes uv sync $VIRTUAL_ENV to the current project's lockfile. When
that venv is shared (a workspace venv several editable repos install into),
the sync up- and downgrades packages the other repos depend on.
"""

import os
import re
from pathlib import Path

from hooks.hook_manager import BlockAction

_ACTIVE_SYNC_RE = re.compile(r"\buv\s+(?:run|sync)\b[^|&;\n]*--active\b")


def _project_root(cwd: Path) -> Path | None:
    for d in (cwd, *cwd.parents):
        if (d / "pyproject.toml").is_file():
            return d
    return None


def check_venv_guard(payload: dict) -> None:
    from hooks.context._strip import strip_non_command_content
    from hooks.context.branch_guard import _resolve_cwd

    command = payload.get("tool_input", {}).get("command", "")
    venv = os.environ.get("VIRTUAL_ENV", "")
    if not command or not venv or not _ACTIVE_SYNC_RE.search(strip_non_command_content(command)):
        return

    root = _project_root(Path(_resolve_cwd(command, payload.get("cwd", ""))).resolve())
    if root and Path(venv).resolve() == (root / ".venv").resolve():
        return

    raise BlockAction(
        f"BLOCKED: `uv --active` would sync {venv} to {root or 'this project'}'s lockfile, "
        "and that venv is not this project's own .venv. Other projects installed in it "
        "would lose their package versions.\n"
        f"Run tools from the venv directly ({venv}/bin/python -m pytest), or add one "
        f"package with `uv pip install --python {venv}/bin/python <pkg>`."
    )
