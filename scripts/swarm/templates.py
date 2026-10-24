"""Saved swarm templates: per lane role, cap, agent, model, effort and default task kind, plus a compact limit.

Built-in templates ship under profiles/package/swarm-templates; user templates live in
$AGENTIHOOKS_HOME/swarm-templates and win over a built-in of the same name.
"""

import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from scripts import agent_choice
from scripts.inbox import links
from scripts.swarm.store import AUTONOMY, SwarmError
from scripts.swarm_ledger import ledger_kinds

LANES = ("eng", "ci", "plan", "master")
DEFAULT_CAPS = {"eng": 2, "ci": 1, "plan": 1, "master": 1}
DEFAULT_PROFILES = {"eng": "engineer", "ci": "cicd", "plan": "planner", "master": "master", "dispatch": "dispatcher"}
AUTO = "auto"
LANE_FIELDS = ("role", "agent", "model", "effort", "kind", "profile")
LINK_FIELDS = {"from", "to", "kind"}
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
BUILT_IN = Path(__file__).resolve().parents[2] / "profiles" / "package" / "swarm-templates"


@dataclass(frozen=True)
class Lane:
    role: str = ""
    cap: int = 0
    agent: str = AUTO
    model: str = AUTO
    effort: str = AUTO
    kind: str = AUTO
    profile: str = ""


@dataclass(frozen=True)
class Template:
    name: str
    lanes: dict
    compact_limit: int = 0
    links: list = field(default_factory=list)
    autonomy: str = ""


def user_dir(environ):
    return Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks") / "swarm-templates"


def lane(name, data):
    unknown = set(data) - {f.name for f in fields(Lane)}
    if unknown:
        raise SwarmError(f"lane {name} has unknown fields {sorted(unknown)}")
    found = Lane(**{"cap": DEFAULT_CAPS[name], "profile": DEFAULT_PROFILES[name], **data})
    if not isinstance(found.cap, int) or isinstance(found.cap, bool) or found.cap < 0:
        raise SwarmError(f"lane {name} cap must be a whole number")
    if found.agent not in (AUTO, *agent_choice.AGENTS):
        raise SwarmError(f"lane {name} agent must be one of {(AUTO, *agent_choice.AGENTS)}")
    if found.kind not in (AUTO, *ledger_kinds.KINDS):
        raise SwarmError(f"lane {name} kind must be one of {(AUTO, *ledger_kinds.KINDS)}")
    if not isinstance(found.profile, str) or not NAME_RE.fullmatch(found.profile):
        raise SwarmError(f"lane {name} profile must be a profile name")
    if not all(isinstance(getattr(found, key), str) for key in LANE_FIELDS):
        raise SwarmError(f"lane {name} role, agent, model, effort and kind are text")
    return found


def link(data):
    if not isinstance(data, dict) or set(data) != LINK_FIELDS:
        raise SwarmError(f"a link is an object with exactly {sorted(LINK_FIELDS)}")
    if data["kind"] not in links.KINDS:
        raise SwarmError(f"a link kind is one of {links.KINDS}")
    if not all(isinstance(data[end], str) and data[end] for end in ("from", "to")):
        raise SwarmError("a link's from and to each name a seat or a lane")
    return dict(data)


def _checked(name):
    if not NAME_RE.match(name):
        raise SwarmError("a template name is lowercase letters, digits and dashes, starting with a letter")
    return name


def parse(data):
    name = _checked(data.get("name", ""))
    lanes = data.get("lanes") or {}
    custom = set(lanes) - set(LANES)
    if custom:
        raise SwarmError(
            f"template {name} names lanes {sorted(custom)}; a swarm has only the eng, ci, plan and master lanes"
        )
    if data.get("autonomy", "") not in ("", *AUTONOMY):
        raise SwarmError(f"template {name} autonomy must be one of {AUTONOMY}")
    return Template(
        name,
        {key: lane(key, lanes.get(key, {})) for key in LANES},
        int(data.get("compact_limit", 0)),
        [link(found) for found in data.get("links", [])],
        str(data.get("autonomy", "")),
    )


def _read(path):
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise SwarmError(f"cannot read the swarm template {path}: {exc}") from exc


def load(name, environ):
    for folder in (user_dir(environ), BUILT_IN):
        path = folder / f"{_checked(name)}.json"
        if path.is_file():
            return _read(path)
    raise SwarmError(f"no swarm template {name}; agentihooks swarm templates lists them")


def available(environ):
    found = {p.stem: (p, "built-in") for p in sorted(BUILT_IN.glob("*.json"))}
    found.update({p.stem: (p, "user") for p in sorted(user_dir(environ).glob("*.json"))})
    return [(_read(path), source) for _, (path, source) in sorted(found.items())]


def save(template, environ):
    path = user_dir(environ) / f"{template.name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**asdict(template), "lanes": {key: asdict(value) for key, value in template.lanes.items()}}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def lane_map(template):
    return {key: {f: getattr(value, f) for f in LANE_FIELDS} for key, value in template.lanes.items()}


def from_config(name, config):
    caps = {"eng": config.max_eng, "ci": config.max_ci, "plan": config.max_plan, "master": 1}
    return parse(
        {
            "name": name,
            "lanes": {key: {**config.lanes.get(key, {}), "cap": caps[key]} for key in LANES},
            "compact_limit": config.compact_limit,
            "links": config.links,
            "autonomy": config.autonomy,
        }
    )
