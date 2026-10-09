"""Launch any role or overlay profile on a swarm for the operator: a name from code, no task and no lane slot."""

from dataclasses import dataclass

from hooks.context import profile_chain
from scripts.swarm import prompt
from scripts.swarm.store import MASTER, SwarmError

KINDS = {"planner": "plan", "engineer": "eng", "qa": "eng", "cicd": "ci"}


@dataclass(frozen=True)
class Resolved:
    profile: str
    role: str
    lane: str


@dataclass(frozen=True)
class Launched:
    name: str
    pane: str
    profile: str
    role: str


def _role(profile):
    from scripts.targets._common import _install_module

    installer = _install_module()
    if installer._resolve_profile_dir(profile) is None:
        return None
    return profile_chain.role(installer._resolve_profile_chain(profile)) or ""


def resolve(profile):
    role = _role(profile)
    if role is None:
        raise SwarmError(f"profile {profile} is not installed: install it with agentihooks init")
    if role == MASTER:
        raise SwarmError("the master comes online with agentihooks swarm <id> master up")
    if role not in KINDS:
        raise SwarmError(f"profile {profile} wears no base role among {', '.join(KINDS)}")
    return Resolved(profile, role, KINDS[role])


def up(store, slug, runtime, profile, at):
    found = resolve(profile)
    config = store.ensure_code(slug)
    name = store.next_name(slug, found.lane, at)
    store.names.note(name, operator=found.profile)
    text = prompt.build_operator(slug, config.repo, name, found.role, found.profile)
    try:
        placed = runtime.operator(config, name, found.profile, text)
    except Exception as exc:
        store.names.retire(name, at)
        raise SwarmError(f"{name} could not start: {exc}") from exc
    return Launched(name, placed.pane_id, found.profile, found.role)


def retire(store, slug, name, at):
    entry = store.names.entry(name) if name else {}
    if not entry.get("operator") or entry.get("swarm") != slug:
        raise SwarmError(f"{name or 'this session'} was not launched with agentihooks swarm {slug} <profile> up")
    store.names.retire(name, at)
