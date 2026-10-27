"""agentihooks hive create: one workstation command brings a new Kubernetes hive live in herdr."""

import itertools
import json
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from scripts import openbao_kv
from scripts.hive import auth, create_github, create_render, invite, machine, member_swarms, nats_grant, registry, setup
from scripts.hive.bundle_origin import SETTINGS_PATH

if TYPE_CHECKING:
    from redis import Redis

STATE_FILE = "hive-create.json"
WORKSTATION_RECORD = "k8s/agentihooks-swarm-workstation-hive"
HOST_KEY = "ssh_host_key"
HOST_PUBLIC_KEY = "ssh_host_public_key"
AUTHORIZED_KEYS = "ssh_authorized_keys"
SSH_USER = "worker"
SIGNER_NAMESPACE = "agentihooks-swarm"
SIGNER_SYNC = "agentihooks-swarm-nats-signer"
POLL_S = 15
COMMAND_TIMEOUT_S = 60


@dataclass(frozen=True)
class Options:
    repo: str = "the-cloud-clockwork/antoncore"
    base: str = "dev"
    like: str = WORKSTATION_RECORD
    node: str = "worker-001"
    timeout_s: int = 1800
    dry_run: bool = False
    name: str | None = None


@dataclass
class Tools:
    redis: "Redis"
    bao: openbao_kv.OpenBao
    environ: Mapping[str, str]
    local_id: str
    local_redis_url: str
    ledger_ca: str | None = None
    run: create_github.Run = subprocess.run
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    out: Callable[[str], None] = print
    state: dict = field(default_factory=dict)


def create(tools: Tools, options: Options) -> str:
    _placing(tools)
    _signer_path(tools.environ)
    tools.state = _start(tools, options)
    name = tools.state["name"]
    if not options.dry_run:
        _save(tools)
    for title, step in STEPS:
        tools.out(f"hive create {name}: {title}: {step(tools, options)}")
        if not options.dry_run:
            _save(tools)
    if options.dry_run:
        return f"dry run of hive create {name}: nothing written"
    tools.state["finished"] = True
    _save(tools)
    return f"hive {name} is live in herdr as {_target(tools.state)}"


def _placing(tools: Tools) -> None:
    from scripts.swarm.store import RedisStore

    store = RedisStore(tools.redis)
    configs = [store.config(slug) for slug in store.slugs()]
    if not any(config.hive_placement and config.state == "running" for config in configs):
        raise auth.HiveError(
            "no running swarm places engineers on hives, so the new hive would wait for one forever: "
            "run agentihooks swarm <slug> set hive-placement=on on a running swarm"
        )


def _state_path(tools: Tools) -> Path:
    return registry.home(tools.environ) / STATE_FILE


def _start(tools: Tools, options: Options) -> dict:
    try:
        saved = json.loads(_state_path(tools).read_text())
    except (OSError, ValueError):
        saved = {}
    if saved.get("name") and not saved.get("finished"):
        if options.name not in (None, saved["name"]):
            raise auth.HiveError(
                f"an unfinished hive create of {saved['name']} is saved; rerun hive create without --name to finish it"
            )
        tools.out(f"hive create {saved['name']}: resuming the unfinished create")
        return saved
    name = _named(tools, options)
    services = json.loads(_kubectl(tools, ["get", "service", "-A", "-o", "json"]))["items"]
    lan_ip = create_render.lan_address(services)
    port = create_render.kept_port(services, name) or create_render.next_port(services, lan_ip)
    return {"name": name, "record": invite.default_path(name), "ssh_port": port, "lan_ip": lan_ip, "node": options.node}


def _named(tools: Tools, options: Options) -> str:
    held = machine.held_names(tools.redis, tools.run) | set(tools.redis.hvals(machine.MACHINES))
    template = create_github.read(tools.run, options.repo, create_render.TEMPLATE_PATH, options.base)
    present = create_github.tree(tools.run, options.repo, options.base)

    def holders(name: str) -> list[str]:
        found = ["a hive"] if name in held else []
        found += [f"the {options.repo} file {path}" for path in sorted(create_render.paths(template, name) & present)]
        return found

    name = options.name or next(
        machine.hive_name(ordinal) for ordinal in itertools.count(1) if not holders(machine.hive_name(ordinal))
    )
    found = holders(name)
    if found:
        raise auth.HiveError(
            f"hive create refuses {name}: it is already held by {' and '.join(found)}; "
            "pass --name with an unused hive name, or leave it out to take the next one"
        )
    return name


def _save(tools: Tools) -> None:
    path = _state_path(tools)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tools.state, indent=2) + "\n")


def _kubectl(tools: Tools, argv: list[str]) -> str:
    return machine.run_checked(tools.run, ["kubectl", *argv], None, COMMAND_TIMEOUT_S)


def _record(tools: Tools) -> dict[str, str]:
    try:
        return openbao_kv.read(tools.bao, tools.state["record"])[0]
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None


def _member(tools: Tools) -> dict[str, str]:
    return invite._env(_record(tools).get(invite.CREDENTIAL))


def _invite(tools: Tools, options: Options) -> str:
    listing = Listing(_signer_path(tools.environ), member_swarms.swarms(tools.redis, ()))
    member = _member(tools).get(invite.HIVE_ID)
    where = f"OpenBao {openbao_kv.MOUNT}/{tools.state['record']}"
    if member and tools.redis.exists(f"{auth.PREFIX}:member:{member}"):
        done = f"kept hive member {member} in {where}"
    elif options.dry_run:
        done, member = f"would mint a hive member into {where} like {options.like}", "the new member"
    else:
        name, record = tools.state["name"], tools.state["record"]
        request = invite.Request(
            name, record, options.like, ledger_ca=tools.ledger_ca, signer_path=listing.path, swarms=listing.swarms
        )
        done = invite.invite(tools.redis, tools.bao, request, tools.local_redis_url)
        member = _member(tools)[invite.HIVE_ID]
    return f"{done}; {_listed(tools, options, listing, member)}"


@dataclass(frozen=True)
class Listing:
    path: str
    swarms: tuple[str, ...]


def _signer_path(environ: Mapping[str, str]) -> str:
    path = member_swarms.signer_path(environ)
    if path is None:
        raise auth.HiveError(
            "hive create lists the new hive member on the hub NATS signer record, "
            f"so {nats_grant.SIGNER_ENV} must name an OpenBao record under {nats_grant.HUB_PATH}"
        )
    return path


def _listed(tools: Tools, options: Options, listing: Listing, member: str) -> str:
    try:
        listed = openbao_kv.read(tools.bao, listing.path)[0].get(member_swarms.FIELD)
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None
    if listed is None:
        return f"the hub signer record {listing.path} lists no member swarms, so the hub scopes no member"
    if member in listed and tools.state.get("resynced") == member:
        return f"kept {member} on the hub signer record {listing.path}"
    swarms = ", ".join(listing.swarms)
    if options.dry_run:
        return f"would list {member} on the hub signer record {listing.path} for {swarms} and refresh {SIGNER_SYNC}"
    member_swarms.assign(tools.bao, listing.path, [member], listing.swarms)
    resync = f"force-sync={int(tools.clock() * 1000)}"
    _kubectl(tools, ["-n", SIGNER_NAMESPACE, "annotate", "externalsecret", SIGNER_SYNC, resync, "--overwrite"])
    tools.state["resynced"] = member
    return (
        f"listed {member} on the hub signer record {listing.path} for {swarms} "
        f"and asked the hub's {SIGNER_SYNC} sync to refresh it now"
    )


def _ssh_keys(tools: Tools, options: Options) -> str:
    where = f"OpenBao {openbao_kv.MOUNT}/{tools.state['record']}"
    if _record(tools).get(HOST_KEY):
        return f"kept the SSH host key in {where}"
    keys = sorted((Path(tools.environ.get("HOME") or Path.home()) / ".ssh").glob("*.pub"))
    if not keys:
        raise auth.HiveError("no public key under ~/.ssh to authorize on the hive sshd")
    if options.dry_run:
        return f"would write a new SSH host key and {len(keys)} authorized keys into {where}"
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "host_key"
        argv = ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", tools.state["name"], "-f", str(path)]
        machine.run_checked(tools.run, argv, None, COMMAND_TIMEOUT_S)
        entries = {
            HOST_KEY: path.read_text(),
            HOST_PUBLIC_KEY: path.with_suffix(".pub").read_text().strip(),
            AUTHORIZED_KEYS: "".join(key.read_text() for key in keys),
        }
    try:
        total = openbao_kv.merge(tools.bao, tools.state["record"], entries)
    except openbao_kv.OpenBaoError as exc:
        raise auth.HiveError(str(exc)) from None
    return openbao_kv.summary(tools.state["record"], entries, total)


def _files(tools: Tools, options: Options, cwd: str) -> dict[str, str]:
    state = tools.state
    hive = create_render.Hive(state["name"], state["record"], state["ssh_port"], state["node"], state["lan_ip"])
    template = create_github.read(tools.run, options.repo, create_render.TEMPLATE_PATH, options.base, cwd)
    return create_render.render(template, hive)


def _manifests(tools: Tools, options: Options) -> str:
    if tools.state.get("pr"):
        return f"kept pull request {tools.state['pr']}"
    with tempfile.TemporaryDirectory() as folder:
        clone = create_github.clone(tools.run, options.repo, folder)
        files = _files(tools, options, clone)
        if options.dry_run:
            for path, text in files.items():
                tools.out(f"--- {path}\n{text}")
            return f"would open a pull request into {options.repo} {options.base} with {', '.join(files)}"
        name = tools.state["name"]
        message = f"Add worker hive {name}\n\nRendered by agentihooks hive create from {create_render.TEMPLATE_PATH}."
        tools.state["pr"] = create_github.open_pr(tools.run, options.repo, options.base, name, files, message, clone)
    return f"opened pull request {tools.state['pr']}"


def _merged(tools: Tools, options: Options) -> str:
    if options.dry_run:
        return "would merge the pull request once its checks pass"
    url = tools.state["pr"]
    found = create_github.pr_state(tools.run, url)
    if found == "CLOSED":
        del tools.state["pr"]
        _save(tools)
        raise auth.HiveError(f"pull request {url} was closed without merging; rerun hive create to open a new one")
    if found != "MERGED":
        if not create_github.merge(tools.run, url):
            checks = f"pull request {url} checks to pass"
            required = create_github.required_checks(tools.run, options.repo, options.base)
            _wait(tools, options, checks, lambda: create_github.checks_passed(tools.run, url, required))
            create_github.squash(tools.run, url)
        _wait(
            tools, options, f"pull request {url} to merge", lambda: create_github.pr_state(tools.run, url) == "MERGED"
        )
    return f"merged {url}"


def _settings(tools: Tools, options: Options) -> str:
    return setup.publish(tools.environ, SETTINGS_PATH, options.dry_run)


def _live(tools: Tools, options: Options) -> str:
    if options.dry_run:
        return "would wait for the hive pod's heartbeat"
    member = _member(tools)[invite.HIVE_ID]

    def beating() -> bool:
        record = registry.show(tools.redis, member)
        return bool(record) and registry.live(record, int(tools.clock() * 1000))

    _wait(tools, options, f"hive {member} to send a heartbeat", beating)
    return f"hive {member} is live"


def _target(state: Mapping) -> str:
    return f"ssh://{SSH_USER}@{state['lan_ip']}:{state['ssh_port']}"


def _trust(tools: Tools) -> None:
    known = Path(tools.environ.get("HOME") or Path.home()) / ".ssh" / "known_hosts"
    line = f"{machine.known_host(_target(tools.state))} {_record(tools)[HOST_PUBLIC_KEY]}"
    lines = known.read_text().splitlines() if known.is_file() else []
    if line not in lines:
        known.parent.mkdir(exist_ok=True)
        known.write_text("".join(f"{entry}\n" for entry in [*lines, line]))


def _herdr(tools: Tools, options: Options) -> str:
    target = _target(tools.state)
    if tools.state.get("added"):
        return f"kept {target} in herdr"
    if options.dry_run:
        return f"would trust the hive host key and save {target} in herdr as {tools.state['name']}"
    _trust(tools)
    machine.save(tools.redis, tools.local_id, target, tools.state["name"], tools.run)
    member = _member(tools)[invite.HIVE_ID]
    tools.state["added"] = member
    return f"added {target} to herdr; its hive member is {member}"


def _engineer(tools: Tools, options: Options) -> str:
    if options.dry_run:
        return "would wait for an engineer working on the hive"
    from scripts.swarm.store import RedisStore

    store = RedisStore(tools.redis)
    hives = {_member(tools)[invite.HIVE_ID], tools.state["added"]}
    found: list[str] = []

    def working() -> bool:
        found[:] = [
            f"{agent.name} in {slug}"
            for slug in store.slugs()
            for agent in store.agents(slug)
            if agent.hive in hives and agent.lane != "master" and agent.state == "working"
        ]
        return bool(found)

    _wait(tools, options, "an engineer working on the hive", working)
    return f"{found[0]} works on the hive"


def _wait(tools: Tools, options: Options, what: str, check: Callable[[], bool]) -> None:
    deadline = tools.clock() + options.timeout_s
    while not check():
        if tools.clock() >= deadline:
            raise auth.HiveError(
                f"timed out after {options.timeout_s}s waiting for {what}; rerun hive create to resume"
            )
        tools.sleep(POLL_S)


STEPS = (
    ("invite", _invite),
    ("ssh keys", _ssh_keys),
    ("manifests", _manifests),
    ("merge", _merged),
    ("settings", _settings),
    ("live", _live),
    ("herdr", _herdr),
    ("engineer", _engineer),
)
