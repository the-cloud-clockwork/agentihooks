"""The placement gate: with agentihooks installed editable, a write into a profile, condition, rule, hook or skill
folder, or an enforcements file, of the package checkout or the linked bundle waits for the operator to say where the
change belongs.

The operator's AskUserQuestion answer is recorded per session at PostToolUse; each answer opens only its destination.
A worktree counts as its repository, matched by git common dir. A non editable install lets every write through.
"""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse
from urllib.request import url2pathname

from scripts.gates.base import Decision
from scripts.gates.build import EDIT_TOOLS, SERENA, SERENA_EDITS, _patch_targets
from scripts.gates.log import safe_name

NAME = "placement"
PACKAGE = "agentihooks package"
BUNDLE = "bundle profile extension"
QUESTION = "Where does this change belong?"
FOLDERS = frozenset({"profiles", "conditions", "rules", "hooks", "skills"})
ENFORCEMENTS = "enforcements.json"
SHOWN = 3


def editable_source(dist=None):
    try:
        raw = (dist or metadata.distribution("agentihooks")).read_text("direct_url.json")
        url = json.loads(raw) if raw else {}
    except (metadata.PackageNotFoundError, OSError, ValueError):
        return None
    if url.get("dir_info", {}).get("editable") and str(url.get("url", "")).startswith("file:"):
        return Path(url2pathname(urlparse(url["url"]).path))
    return None


def linked_bundle():
    from hooks.context.profile_chain import bundle_path, read_state

    return bundle_path(read_state())


def answers_home():
    from hooks.config import AGENTIHOOKS_HOME

    return AGENTIHOOKS_HOME / "placement"


def _answers_path(session, home):
    return Path(home or answers_home()) / f"{safe_name(session)}.json"


def answered(session, home=None):
    try:
        return set(json.loads(_answers_path(session, home).read_text()))
    except (OSError, ValueError, TypeError):
        return set()


def heard(payload, home=None):
    """Record the destinations an AskUserQuestion answer names for its session; True when it named one."""
    session = payload.get("session_id")
    response = payload.get("tool_response")
    if payload.get("tool_name") != "AskUserQuestion" or not (session and isinstance(response, dict)):
        return False
    words = " ".join(str(v) for v in (response.get("answers") or {}).values()).lower()
    chosen = {kind for kind in (PACKAGE, BUNDLE) if kind in words}
    if not chosen:
        return False
    path = _answers_path(session, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(answered(session, home) | chosen)))
    return True


def _repo(path):
    """(top level, git common dir) of the repository holding path, or None outside one."""
    where = next((p for p in (path, *path.parents) if p.is_dir()), None)
    if where is None:
        return None
    out = subprocess.run(
        ["git", "-C", str(where), "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"],
        capture_output=True,
        text=True,
        timeout=3,
    )
    lines = out.stdout.splitlines()
    if out.returncode or len(lines) != 2:
        return None
    return Path(lines[0]), Path(lines[1]).resolve()


def placed(relative):
    parts = relative.parts
    return parts[-1] == ENFORCEMENTS or bool(FOLDERS.intersection(parts[:-1]))


def refusal(missing, paths):
    shown = ", ".join(paths[:SHOWN])
    return (
        f"placement: agentihooks is installed editable and this write lands in the {' and the '.join(missing)} "
        f'({shown}). Before it, ask the operator one AskUserQuestion, "{QUESTION}", with the options '
        f'"{PACKAGE}" and "{BUNDLE}", and state your recommendation in it: reusable agentic behaviour any developer '
        f"could use belongs in the {PACKAGE}; anything about the operator's infrastructure or personal setup belongs "
        f"in the {BUNDLE}. Then write only into the destination the operator chose."
    )


@dataclass(frozen=True)
class PlacementGate:
    name: ClassVar[str] = NAME
    default_mode: ClassVar[str] = "enforce"
    source: Callable = editable_source
    bundle: Callable = linked_bundle
    home: Path | None = None

    def matches(self, call):
        if call.tool.startswith(SERENA):
            return call.tool[len(SERENA) :] in SERENA_EDITS
        return call.tool in EDIT_TOOLS

    def targets(self, call):
        if call.tool.startswith(SERENA):
            scope = call.tool_input.get("relative_path")
            return [] if not scope or call.tool_input.get("dry_run") else [Path(call.cwd) / scope]
        return [Path(call.cwd) / named for named in filter(None, _patch_targets(call, EDIT_TOOLS[call.tool]))]

    def destinations(self, call, source):
        """Each destination this call writes into a placement folder of, with the repo-relative paths."""
        kinds = {}
        for kind, root in ((PACKAGE, source), (BUNDLE, self.bundle())):
            found = _repo(Path(root)) if root else None
            if found:
                kinds[found[1]] = kind
        landing = {}
        for path in map(Path.resolve, self.targets(call)):
            found = _repo(path)
            if found and found[1] in kinds and placed(path.relative_to(found[0])):
                landing.setdefault(kinds[found[1]], []).append(path.relative_to(found[0]).as_posix())
        return landing

    def decide(self, call, who, state):
        source = self.source()
        if source is None:
            return Decision()
        landing = self.destinations(call, source)
        missing = sorted(set(landing) - answered(call.session, self.home))
        if not missing:
            return Decision()
        return Decision.deny(refusal(missing, [p for kind in missing for p in landing[kind]]))
