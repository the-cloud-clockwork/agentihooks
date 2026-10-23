"""agentihooks hive add|invite|scope|create|refresh|revoke|remove|controller|serve|set|show|list|run|install|bundle-origin|setup."""

import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from scripts.hive import auth, bundle_origin, registry, server
from scripts.swarm_ledger import ledger_ca, ledger_link

LOCK_FILE = "hive.lock"

if TYPE_CHECKING:
    from redis import Redis

    from scripts.hive.nats_grant import Signer


def redis_client() -> "Redis":
    from scripts.swarm.store import redis_client as connect

    return connect()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def refresh_env(environ: MutableMapping[str, str]) -> str:
    saved = _saved_env(environ)
    url, credential = (
        environ.get(name) or saved.get(name) for name in ("AGENTIHOOKS_HIVE_URL", "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL")
    )
    if not (url and credential):
        raise auth.HiveError(
            "this host has not joined a hive: "
            "AGENTIHOOKS_HIVE_URL and AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL must both be set"
        )
    grant = _post(url, server.REFRESH_PATH, {"ledger_credential": credential}, "refresh", environ)
    from scripts.hive import join

    join.save(registry.home(environ), url, {**grant, "ledger_credential": credential}, saved)
    ledger_ca.place(registry.home(environ), grant.get("ledger_ca"), environ)
    environ["AGENTIHOOKS_HIVE_REDIS_URL"] = grant["redis_url"]
    return grant["id"]


def saved_redis_url(environ: Mapping[str, str]) -> str | None:
    return _saved_env(environ).get("AGENTIHOOKS_HIVE_REDIS_URL") or None


def saved_member(environ: Mapping[str, str]) -> dict[str, str]:
    saved = _saved_env(environ)
    return {name: saved[name] for name in ("AGENTIHOOKS_HIVE_ID", "AGENTIHOOKS_HIVE_REDIS_URL") if saved.get(name)}


def _saved_env(environ: Mapping[str, str]) -> dict[str, str]:
    try:
        lines = (registry.home(environ) / auth.ENV_FILE).read_text().splitlines()
    except OSError:
        return {}
    return dict(line.partition("=")[::2] for line in lines)


@contextlib.contextmanager
def refresh_lock(environ: Mapping[str, str]) -> Iterator[None]:
    path = registry.home(environ) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _trust(environ: Mapping[str, str]) -> dict:
    # The first refresh delivers the ledger CA, so a named file that does not exist yet means system trust.
    if environ.get(ledger_link.CA_ENV) or ledger_link.ca_file(environ).is_file():
        return ledger_link.transport(environ)
    return {}


def _post(url: str, path: str, body: dict, action: str, environ: Mapping[str, str]) -> dict:
    parts = urlsplit(url)
    if parts.scheme != "https" and not server.is_loopback(parts.hostname):
        raise auth.HiveError(f"a {action} off loopback carries credentials, so the hive URL must be https")
    request = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request, timeout=server.REQUEST_TIMEOUT_S, **_trust(environ)) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            reason = json.load(exc)["error"]
        except (ValueError, KeyError, TypeError):
            reason = f"HTTP {exc.code}"
        raise auth.HiveError(reason) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise auth.HiveError(f"the hive at {url} is unreachable ({exc})") from exc
    except ValueError as exc:
        raise auth.HiveError(f"the hive at {url} answered with a body that is not JSON") from exc


def _add(args: argparse.Namespace, label: str) -> dict:
    from scripts.hive import machine
    from scripts.swarm.commands import hive_id
    from scripts.swarm.store import redis_url

    return machine.add(
        redis_client(),
        hive_id(),
        args.target,
        label,
        machine.Delivery(args.url, args.redis_url or redis_url(os.environ), ledger_ca.served()),
    )


def _invite(args: argparse.Namespace) -> str:
    from dataclasses import replace

    from scripts import openbao_kv
    from scripts.hive import invite, member_swarms
    from scripts.swarm.store import redis_url

    bao = openbao_kv.from_env(os.environ)
    if bao is None:
        raise auth.HiveError(f"{openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} must both be set")
    request = invite.Request(
        args.label,
        args.path or invite.default_path(args.label),
        args.like,
        args.url,
        args.redis_url,
        args.swarm_url,
        ledger_ca.served(),
        bus_url=args.bus_url,
    )
    redis = redis_client()
    if signer_path := member_swarms.signer_path(os.environ):
        request = replace(request, signer_path=signer_path, swarms=member_swarms.swarms(redis, args.swarm))
    return invite.invite(redis, bao, request, redis_url(os.environ))


def _scope(args: argparse.Namespace) -> str:
    from scripts import openbao_kv
    from scripts.hive import member_swarms, nats_grant

    bao = openbao_kv.from_env(os.environ)
    path = member_swarms.signer_path(os.environ)
    if bao is None or path is None:
        raise auth.HiveError(
            f"{openbao_kv.URL_ENV}, {openbao_kv.TOKEN_ENV} and {nats_grant.SIGNER_ENV} must all be set "
            "to list member swarms"
        )
    return member_swarms.backfill(redis_client(), bao, path, args.swarm)


def _create(args: argparse.Namespace) -> str:
    import hooks.config  # noqa: F401  loads the agentihooks env files into os.environ
    from scripts import openbao_kv
    from scripts.hive import create
    from scripts.swarm.commands import hive_id
    from scripts.swarm.store import redis_url

    bao = openbao_kv.from_env(os.environ)
    if bao is None:
        raise auth.HiveError(f"{openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} must both be set")
    tools = create.Tools(redis_client(), bao, os.environ, hive_id(), redis_url(os.environ), ledger_ca.served())
    given = {"repo": args.repo, "base": args.base, "like": args.like, "node": args.node, "timeout_s": args.timeout}
    options = create.Options(**{key: value for key, value in given.items() if value is not None}, dry_run=args.dry_run)
    return create.create(tools, options)


def _remove(args: argparse.Namespace) -> list[str]:
    from scripts import openbao_kv
    from scripts.hive import invite, remove
    from scripts.swarm.store import RedisStore

    bao = openbao_kv.from_env(os.environ)
    if bao is None:
        raise auth.HiveError(f"{openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} must both be set")
    store = RedisStore(redis_client())
    label = args.label or remove.label(store.redis, args.id)
    plan = remove.Plan(args.id, label, args.path or invite.default_path(label), args.antoncore, args.wait, _signer())
    return remove.remove(store, bao, plan, remove.Host())


def _retire(args: argparse.Namespace) -> str:
    if args.command == "revoke":
        from scripts.hive import nats_grant

        redis, signer = redis_client(), _signer()
        bus = [nats_grant.revoke(redis, signer, args.id)] if signer is not None else []
        auth.revoke(redis, args.id)
        return "\n".join([*bus, f"revoked {args.id}"])
    if args.command == "drain":
        return _drain(args.wait)
    return "\n".join(_remove(args))


def _drain(wait_s: int) -> str:
    from scripts.hive import remove
    from scripts.swarm.commands import hive_id
    from scripts.swarm.store import RedisStore

    return remove.settle(RedisStore(redis_client()), hive_id(), wait_s, remove.Host())


def _signer() -> "Signer | None":
    from scripts.hive import nats_grant

    return nats_grant.from_environ(os.environ)


def _nats_trust(args: argparse.Namespace) -> str:
    import yaml

    from scripts import openbao_kv
    from scripts.hive import nats_trust

    bao = openbao_kv.from_env(os.environ)
    if bao is None:
        raise auth.HiveError(
            f"nats-trust writes OpenBao, so {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV} must be set"
        )
    values, lines = nats_trust.bootstrap(bao, nats_trust.load_users(args.users), args.url)
    Path(args.values).write_text(yaml.safe_dump(values, sort_keys=False))
    return "\n".join([*lines, f"wrote the public operator trust to {args.values}"])


def _serve(args: argparse.Namespace) -> int:
    from scripts.swarm.store import redis_url

    if bool(args.tls_cert) != bool(args.tls_key):
        raise auth.HiveError("--tls-cert and --tls-key go together")
    tls = (args.tls_cert, args.tls_key) if args.tls_cert else None
    redis = redis_client()
    auth.regrant(redis)
    _signer()
    httpd = server.make_server(
        redis,
        args.redis_url or redis_url(os.environ),
        args.host,
        args.port,
        tls,
        ledger_ca=ledger_ca.served(),
        signer=_signer,
    )
    scheme = "https" if tls else "http"
    print(f"hive endpoint on {scheme}://{args.host}:{httpd.server_address[1]}{server.REFRESH_PATH}", flush=True)
    httpd.serve_forever()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks hive", description="Hive credentials for remote swarm hosts")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="Save an SSH machine in herdr and deliver its member credential to it")
    add.add_argument("target", help="SSH target herdr machine add dials, such as ssh://worker@host:2222")
    add.add_argument("--url", required=True, help="this hive's endpoint as the machine reaches it, for hive refresh")
    add.add_argument("--redis-url", help="Redis URL the machine connects to; defaults to this host's")
    invited = sub.add_parser("invite", help="Mint a hive member and write its credential into an OpenBao record")
    invited.add_argument("label", help="hive member name")
    invited.add_argument("--path", help="OpenBao record under the secret mount; defaults to k8s/agentihooks-hive-LABEL")
    invited.add_argument("--like", help="OpenBao record of another hive to copy addresses and the ledger CA from")
    invited.add_argument("--url", help="this hive's endpoint as the new hive reaches it, for hive refresh")
    invited.add_argument("--redis-url", help="Redis URL the new hive connects to")
    invited.add_argument("--swarm-url", help="ledger address the new hive reaches")
    invited.add_argument("--bus-url", help="NATS address the new hive reaches; copied from --like when omitted")
    invited.add_argument(
        "--swarm", action="append", default=[], help="swarm the member's bus credential reaches; every swarm if omitted"
    )
    scoped = sub.add_parser("scope", help="List member swarms in the hub NATS signer record for every unlisted member")
    scoped.add_argument("--swarm", action="append", default=[], help="swarm to list; every swarm if omitted")
    created = sub.add_parser("create", help="Create a Kubernetes hive from nothing and add it to herdr")
    created.add_argument("--repo", help="GitOps repository of the hive; defaults to antoncore")
    created.add_argument("--base", help="branch the hive pull request merges into; defaults to dev")
    created.add_argument("--like", help="OpenBao record to copy; defaults to the workstation hive's")
    created.add_argument("--node", help="node the hive pod runs on; defaults to worker-001")
    created.add_argument("--timeout", type=int, help="seconds each wait may take; defaults to 1800")
    created.add_argument("--dry-run", action="store_true", help="Print every step and file, and write nothing")
    joined = sub.add_parser("join", help="Join this host with one invite file from hive invite")
    joined.add_argument("invite", type=Path, help="private member invite file shared with Kubernetes workers")
    sub.add_parser("refresh", help="Mint a new Redis credential for this hive through its hive endpoint")
    sub.add_parser("revoke", help="Delete a member's ledger credential and Redis user").add_argument("id")
    removed = sub.add_parser(
        "remove", help="Drain a hive, then delete it from Redis, herdr, known hosts, OpenBao and antoncore"
    )
    removed.add_argument("id")
    removed.add_argument("--label", help="herdr label and manifest name; defaults to the member's name")
    removed.add_argument("--path", help="OpenBao record under the secret mount; defaults to k8s/agentihooks-hive-LABEL")
    removed.add_argument("--antoncore", type=Path, help="antoncore checkout to remove the hive's manifests from")
    removed.add_argument("--wait", type=int, default=3600, help="seconds to wait for its agents to hand off")
    drained = sub.add_parser("drain", help="Take no new agents on this hive and wait until its agents finish")
    drained.add_argument("--wait", type=int, default=3600, help="seconds to wait for its agents to finish")
    sub.add_parser(
        "controller", help="Write a new controller service credential to a private file, retiring the previous one"
    )
    serve = sub.add_parser("serve", help="Run the hive refresh endpoint")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument("--redis-url", help="Redis URL members connect to; defaults to this host's")
    serve.add_argument("--tls-cert", help="PEM certificate; required off loopback")
    serve.add_argument("--tls-key", help="PEM private key for --tls-cert")
    settings = sub.add_parser("set", help=f"Set {', '.join(registry.SETTINGS)} as key=value")
    settings.add_argument("--keep", action="store_true", help="Leave max-agents, excluded, ui and ephemeral as stored")
    settings.add_argument("id")
    settings.add_argument("settings", nargs="*", metavar="key=value")
    trust = sub.add_parser(
        "nats-trust", help="Create the swarm NATS operator trust once and sign the service users of a users file"
    )
    trust.add_argument("--users", required=True, help="YAML file listing each service user and its OpenBao record")
    trust.add_argument("--url", required=True, help="NATS address the hive join endpoint pushes account updates to")
    trust.add_argument("--values", required=True, help="Helm values file to write the public operator trust to")
    sub.add_parser("show", help="Print a hive's record as JSON").add_argument("id")
    sub.add_parser("list", help="One line per hive with its liveness")
    sub.add_parser("run", help="Publish hive telemetry every fifteen seconds")
    sub.add_parser("install", help="Write and enable the hive user service")
    compare = sub.add_parser("compare", help="List each live hive's configuration against a base hive's")
    compare.add_argument("--base", help="The base hive id; defaults to this machine's hive")
    origin = sub.add_parser(
        "bundle-origin", help="Write the linked bundle's git address and branch into the hive settings record"
    )
    origin.add_argument("--path", default=bundle_origin.SETTINGS_PATH, help="OpenBao record under the secret mount")
    setup = sub.add_parser(
        "setup", help="Write the workstation agent settings and the bundle origin into the hive settings record"
    )
    setup.add_argument("--path", default=bundle_origin.SETTINGS_PATH, help="OpenBao record under the secret mount")
    setup.add_argument(
        "--dry-run", action="store_true", help="Print the key names it would write and leave out, and write nothing"
    )
    return parser


def _list() -> None:
    from scripts.hive import headroom, join_report

    now = now_ms()
    redis = redis_client()
    hives = registry.hives(redis)
    reported = {record["id"]: join_report.live(redis, record["id"]) for record in hives}
    local = _local_agents(redis, {(report["slug"], report["agent"]) for rows in reported.values() for report in rows})
    for record in hives:
        state = "live" if registry.live(record, now) else "stale"
        excluded = ",".join(record["excluded"]) or "none"
        room = headroom.summary(headroom.read(redis, record["id"], now))
        label = f"\tname={record['name']}" if record["name"] != record["id"] else ""
        print(
            f"{record['id']}{label}\t{state}\tui={record['ui']}"
            f"\tmax-agents={record['max_agents']}\texcluded={excluded}\t{room}"
        )
        for report in reported[record["id"]]:
            print("  " + join_report.summary(report))
        for name in local.pop(record["id"], []):
            print(f"  {name}\tlocal")
    for hive, names in local.items():
        print(f"{hive}\tunregistered")
        for name in names:
            print(f"  {name}\tlocal")


def _local_agents(redis: "Redis", reported: set[tuple[str, str]]) -> dict[str, list[str]]:
    from scripts.hive import reports
    from scripts.swarm import runtime, status
    from scripts.swarm.store import RedisStore

    store = RedisStore(redis)
    candidates = [
        agents
        for slug in store.slugs()
        if (
            agents := [
                agent
                for agent in store.agents(slug)
                if agent.runtime_backend == "local" and not reports.host(agent) and (slug, agent.name) not in reported
            ]
        )
    ]
    if not candidates:
        return {}
    herdr = runtime.HerdrRuntime()
    found: dict[str, list[str]] = {}
    for agents in candidates:
        for agent in status.live_agents(agents, herdr):
            found.setdefault(status.agent_hive(agent), []).append(agent.name)
    return found


def _registry(args: argparse.Namespace) -> None:
    if args.command == "set":
        print(json.dumps(registry.update(redis_client(), args.id, args.settings, keep=args.keep)))
    elif args.command == "show":
        record = registry.show(redis_client(), args.id)
        if record is None:
            raise auth.HiveError(f"no hive {args.id}")
        print(json.dumps(record))
    else:
        _list()


def _daemon(command: str) -> int:
    from scripts.hive import daemon

    actions = {"run": lambda: daemon.run(lambda: daemon.clients(redis_client())), "install": daemon.install}
    result = actions[command]()
    return 1 if result is False else 0


def _compare(base_id: str | None) -> int:
    from scripts.hive import config
    from scripts.swarm import commands

    redis, now = redis_client(), now_ms()
    base_id = base_id or commands.hive_id()
    found = {
        record["id"]: config.read(redis, record["id"]) for record in registry.hives(redis) if registry.live(record, now)
    }
    base = found.pop(base_id, None)
    if base is None:
        raise auth.HiveError(f"live hive {base_id} published no configuration")
    print(f"{base_id}\tbase\t{config.summary(base)}")
    differing = [hive_id for hive_id, other in sorted(found.items()) if _report(hive_id, base, other)]
    return 1 if differing else 0


def _report(hive_id: str, base: dict, other: dict | None) -> bool:
    from scripts.hive import config

    if other is None:
        print(f"{hive_id}\tno configuration published")
        return True
    lines = config.differences(base, other)
    print(f"{hive_id}\t{'differs' if lines else 'equal'}\t{config.summary(other)}")
    for line in lines:
        print(f"  {line}")
    return bool(lines)


def _setup(args: argparse.Namespace) -> str:
    import hooks.config  # noqa: F401  loads the agentihooks env files into os.environ
    from scripts.hive import setup

    return setup.publish(os.environ, args.path, args.dry_run)


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "add":
            from scripts.hive import machine

            label = machine.label(time.time())
            grant = _add(args, label)
            print(f"added {args.target} to herdr as {label}; its hive member is {grant['id']}")
        elif args.command in ("invite", "create", "scope"):
            print({"invite": _invite, "create": _create, "scope": _scope}[args.command](args))
        elif args.command in ("join", "refresh"):
            from scripts.hive import join

            with refresh_lock(os.environ):
                member_id = join.enroll(args.invite, os.environ) if args.command == "join" else refresh_env(os.environ)
            action = "joined" if args.command == "join" else "refreshed the Redis credential of"
            print(f"{action} hive member {member_id}; it is in {registry.home() / auth.ENV_FILE}")
        elif args.command in ("revoke", "remove", "drain"):
            print(_retire(args))
        elif args.command in ("run", "install"):
            return _daemon(args.command)
        elif args.command == "compare":
            return _compare(args.base)
        elif args.command == "controller":
            path = auth.write_controller_env(registry.home(), auth.issue_controller(redis_client()))
            print(f"issued a controller credential; it is in {path}")
        elif args.command in ("set", "show", "list"):
            _registry(args)
        elif args.command == "bundle-origin":
            print(bundle_origin.publish(os.environ, args.path))
        elif args.command in ("setup", "nats-trust"):
            print((_setup if args.command == "setup" else _nats_trust)(args))
        else:
            return _serve(args)
    except auth.HiveError as exc:
        print(f"hive {args.command} refused: {exc}", file=sys.stderr)
        return 1
    return 0
