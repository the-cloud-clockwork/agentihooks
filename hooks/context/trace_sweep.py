"""Trace sweep: find every place a corrected directive still lives, clear the runtime ones, plan the rest.

A hit is one place the directive of an open correction lives, matched by its
source key (enforcement id, condition file, broadcast id) or by its text. Its
action: ``clear`` for runtime layers, ``pr`` for files tracked by git (never
edited here), ``followup`` for brain entries (brain regenerates them),
``manual`` for an untracked file no clear function owns. A correction closes
when a sweep finds nothing for it.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from hooks.context import broadcast, conditions, enforcement, injection_trace, profile_chain, quarantine

_NEEDLE_MAX = 120
_NEEDLE_MIN = 20
_WALK_DEPTH = 3
_SKIP_DIRS = {"node_modules", "__pycache__", "venv"}
PRIMING_LAYERS = ("culture", "learned")


@dataclass(frozen=True)
class Hit:
    source: str
    layer: str
    location: str
    key: str
    action: str
    repo: str = ""
    relpath: str = ""


def _closures_path() -> Path:
    return injection_trace._home() / "injection_corrections_closed.jsonl"


def _correction_key(row: dict) -> str:
    return "|".join((row.get("at", ""), row.get("session", ""), row.get("source", "")))


def open_corrections() -> list[dict]:
    closed = {row.get("correction") for row in injection_trace._read(_closures_path())}
    return [row for row in injection_trace.corrections() if _correction_key(row) not in closed]


def close(row: dict) -> None:
    injection_trace._append(_closures_path(), {"at": injection_trace._now(), "correction": _correction_key(row)})


def _norm(text: str) -> str:
    return " ".join(str(text).split())


def _needle(correction: dict) -> str:
    text = correction.get("text")
    if text is None:
        received = [
            r for r in injection_trace.trace(correction.get("session", "")) if r["source"] == correction["source"]
        ]
        text = received[-1]["text"] if received else ""
    needle = _norm(re.sub(r"^From [^:]+: ", "", _norm(text)))[:_NEEDLE_MAX]
    return needle if len(needle) >= _NEEDLE_MIN else ""


def _matches(correction: dict, needle: str, key: str, text: str) -> bool:
    return key == correction["source"] or bool(needle and needle in _norm(text))


def _git_home(path: Path) -> tuple[str, str] | None:
    def git(*args: str) -> str:
        out = subprocess.run(["git", *args], cwd=path.parent, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""

    try:
        if not git("ls-files", "--error-unmatch", path.name):
            return None
        top, common = (
            git("rev-parse", "--show-toplevel"),
            git("rev-parse", "--path-format=absolute", "--git-common-dir"),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    repo = Path(common).parent if common.endswith("/.git") else Path(top)
    return str(repo), str(path.resolve().relative_to(Path(top).resolve()))


def _repo_dirs(root: Path) -> list[Path]:
    found = []
    for directory, subdirs, _files in os.walk(root):
        here = Path(directory)
        if (here / ".agentihooks").is_dir():
            found.append(here)
        depth = len(here.relative_to(root).parts)
        subdirs[:] = (
            [] if depth >= _WALK_DEPTH else [d for d in subdirs if not d.startswith(".") and d not in _SKIP_DIRS]
        )
    return found


def _profile_dirs() -> list[Path]:
    bundle = enforcement._get_bundle_path()
    chain = profile_chain.profile_dirs(bundle, enforcement._get_active_profile(), enforcement._get_linked_profiles())
    dirs = [path for _name, path in chain]
    if bundle is not None:
        dirs.extend(sorted(p for p in (bundle / "profiles").glob("*") if p.is_dir()))
    return list(dict.fromkeys(dirs))


def _stores(root: Path) -> tuple[list[Path], list[Path]]:
    bundle = enforcement._get_bundle_path()
    profiles = _profile_dirs()
    repos = _repo_dirs(root)
    stores = [enforcement._store_path()]
    condition_dirs = [conditions.runtime_dir()]
    if bundle is not None:
        stores.append(bundle / "enforcements.json")
        condition_dirs.append(bundle / ".claude" / "conditions")
    stores += [p / "enforcements.json" for p in profiles] + [r / ".agentihooks" / "enforcements.json" for r in repos]
    condition_dirs += [p / ".claude" / "conditions" for p in profiles] + [
        r / ".agentihooks" / "conditions" for r in repos
    ]
    return list(dict.fromkeys(stores)), list(dict.fromkeys(condition_dirs))


def _file_hit(correction: dict, layer: str, path: Path, key: str) -> Hit:
    home = _git_home(path)
    if home:
        return Hit(correction["source"], layer, str(path), key, "pr", *home)
    runtime = path == enforcement._store_path() or path.parent == conditions.runtime_dir()
    project = path.parent.name == ".agentihooks" or path.parent.parent.name == ".agentihooks"
    return Hit(correction["source"], layer, str(path), key, "clear" if runtime or project else "manual")


def _enforcement_hits(correction: dict, needle: str, stores: list[Path]) -> list[Hit]:
    hits = []
    for store in stores:
        for entry in enforcement._load_json_enforcements(store, ""):
            text = entry.get("message") or entry.get("path", "")
            if _matches(correction, needle, entry.get("id", ""), text):
                hits.append(_file_hit(correction, "enforcement", store, entry.get("id", "")))
    return hits


def _condition_hits(correction: dict, needle: str, directories: list[Path]) -> list[Hit]:
    hits = []
    for directory in directories:
        for path in sorted(directory.glob("*")) if directory.is_dir() else []:
            if conditions.is_ignored(path.name) or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _matches(correction, needle, path.name, text):
                hits.append(_file_hit(correction, "condition", path, path.name))
    return hits


def _broadcast_hits(correction: dict, needle: str) -> list[Hit]:
    hits = []
    wanted = (correction.get("locator") or {}).get("id")
    for msg in broadcast.list_broadcasts():
        origin = msg.get("origin") or {}
        by_origin = bool(wanted and origin.get("id") == wanted)
        if not (by_origin or _matches(correction, needle, msg.get("id", ""), msg.get("message", ""))):
            continue
        if msg.get("source") == "brain-adapter":
            hits.append(Hit(correction["source"], "brain", origin.get("id") or msg["id"], msg["id"], "followup"))
        else:
            hits.append(Hit(correction["source"], "broadcast", msg["id"], msg["id"], "clear"))
    return hits


def find(correction: dict, root: str | Path) -> list[Hit]:
    needle = _needle(correction)
    stores, condition_dirs = _stores(Path(root).expanduser())
    hits = _enforcement_hits(correction, needle, stores)
    hits += _condition_hits(correction, needle, condition_dirs)
    hits += _broadcast_hits(correction, needle)
    hits += _file_source_hits(correction)
    hits += _priming_hits(correction)
    return list(dict.fromkeys(hits))


def _file_source_hits(correction: dict) -> list[Hit]:
    locator = correction.get("locator") or {}
    if correction.get("layer") not in injection_trace.FILE_LAYERS or not locator.get("path"):
        return []
    path = Path(locator["repo"]) / locator["path"]
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    quote = correction.get("quote")
    if quote and _norm(quote) not in _norm(text):
        return []
    return [Hit(correction["source"], correction["layer"], str(path), correction["source"], "pr", *_home_of(path))]


def _home_of(path: Path) -> tuple[str, str]:
    return _git_home(path) or (str(path.parent), path.name)


def _priming_texts(correction: dict) -> list[str]:
    from scripts.swarm.store import connect

    locator, store = correction["locator"], connect()
    if correction["layer"] == "culture":
        return store.culture.get(locator["swarm"]).splitlines()
    return [note["text"] for note in store.memory.learned(locator["seat"])]


def _priming_hits(correction: dict) -> list[Hit]:
    if correction.get("layer") not in PRIMING_LAYERS:
        return []
    hit = Hit(correction["source"], correction["layer"], correction["source"], correction["source"], "held")
    try:
        texts = _priming_texts(correction)
    except Exception:  # an unreadable store keeps the correction open rather than closing it unseen
        return [hit]
    needle, whole = quarantine.needle(correction), _norm(correction["text"])
    found = any((needle and needle in _norm(text)) or _norm(text) == whole for text in texts)
    return [hit] if found else []


def _file_followup(ledger: str, text: str, *flags: str) -> None:
    name = os.environ.get("AGENTIHOOKS_AGENT_NAME") or "trace-sweep"
    cmd = ["agentihooks", "ledger", "--slug", ledger, "--as", name, "followup", "add", text, *flags]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def _clear(hit: Hit, session_id: str) -> str:
    path = Path(hit.location)
    if hit.layer == "broadcast":
        return f"cleared {broadcast.clear_broadcasts(message_id=hit.key)}"
    if hit.layer == "enforcement":
        local = path != enforcement._store_path()
        return f"cleared {enforcement.clear_enforcement(hit.key, local=local, cwd=path.parent.parent)}"
    directory = path.parent != conditions.runtime_dir()
    try:
        conditions.remove_condition(
            file=hit.key,
            session_id=session_id,
            scope="directory" if directory else "",
            cwd=path.parent.parent.parent if directory else None,
        )
    except conditions.ConditionError as e:
        return f"refused: {e}"
    return "removed"


def _apply_one(hit: Hit, correction: dict, session_id: str, ledger: str) -> str:
    if hit.action == "clear":
        return _clear(hit, session_id)
    repo = Path(correction["repo"]).name
    if hit.action == "pr":
        plan = (
            f"pull request in {hit.repo}: remove {hit.key} from {hit.relpath} on a worktree off dev, "
            f"then gh pr create --base dev; file left unchanged"
        )
        text = (
            f"One {hit.layer} directive in the {Path(hit.repo).name} repo is marked wrong for the {repo} repo: "
            f"{correction['reason']}. Remove it by pull request into dev."
        )
        return f"{plan}; {_followup(ledger, text)}"
    if hit.action == "manual":
        return f"edit by hand: {hit.location} is outside git and has no clear function"
    if hit.action == "held":
        return "withheld from priming until its source is fixed"
    entry = re.sub(r"[-_]+", " ", hit.location)
    text = (
        f"The {entry} brain entry still carries a directive marked wrong for the {repo} repo: "
        f"{correction.get('reason', '')}. Brain regenerates it, so fix it at its source."
    )
    return _followup(ledger, text)


def _followup(ledger: str, text: str) -> str:
    if not ledger:
        return f"follow up to file: {text}"
    try:
        _file_followup(ledger, text)
    except (OSError, subprocess.SubprocessError) as e:
        return f"follow up not filed ({e}): {text}"
    return f"follow up filed on {ledger}"


def sweep(root: str | Path, apply: bool = False, session_id: str = "", ledger: str = "", only: str = "") -> dict:
    report = {"plan": [], "applied": [], "closed": [], "proposed": []}
    keys = quarantine.confirmed_keys()
    for correction in open_corrections():
        if only and correction["source"] != only:
            continue
        hits = find(correction, root)
        report["plan"] += hits
        if quarantine.is_proposed(correction, keys):
            report["proposed"].append(correction)
            continue
        if apply and hits:
            report["applied"] += [(hit, _apply_one(hit, correction, session_id, ledger)) for hit in hits]
            hits = find(correction, root)
        if not hits:
            close(correction)
            report["closed"].append(correction)
    return report


def plan_rows(report: dict) -> list[str]:
    rows = ["\t".join((h.source, h.layer, h.action, h.location, h.key)) for h in report["plan"]]
    rows += ["\t".join(("applied", h.source, h.layer, h.location, outcome)) for h, outcome in report["applied"]]
    rows += ["\t".join(("closed", c["source"], c.get("repo", ""), c.get("reason", ""))) for c in report["closed"]]
    rows += [
        "\t".join(("proposed", c["source"], c["repo"], "waits for the operator to confirm")) for c in report["proposed"]
    ]
    return rows
